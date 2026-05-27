"""
중간지점 찾기 - 카카오 API 프록시 서버.
환경변수: KAKAO_API_KEY (필수), ANTHROPIC_API_KEY (AI 기능용), APP_TOKEN (선택)
"""
import os
import json
import concurrent.futures
import requests
from fastapi import FastAPI, Query, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_KAKAO_KEY      = os.environ.get("KAKAO_API_KEY", "")
_ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
_APP_TOKEN      = os.environ.get("APP_TOKEN", "")
_BASE           = "https://dapi.kakao.com/v2/local"

_DEFAULT_SEARCHES = [
    {"type": "category", "code": "AT4"},
    {"type": "category", "code": "CT1"},
    {"type": "keyword",  "query": "광장"},
    {"type": "keyword",  "query": "거리"},
    {"type": "keyword",  "query": "명소"},
    {"type": "keyword",  "query": "공원"},
    {"type": "category", "code": "CE7"},
    {"type": "category", "code": "FD6"},
]


def _check_token(token):
    if _APP_TOKEN and token != _APP_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")


def _kakao_headers():
    return {"Authorization": f"KakaoAK {_KAKAO_KEY}"}


def _get(path, params):
    r = requests.get(f"{_BASE}{path}", headers=_kakao_headers(), params=params, timeout=6)
    r.raise_for_status()
    return r.json()


# ── 기존 엔드포인트 ───────────────────────────────────────────

@app.get("/geocode")
def geocode(query: str = Query(...), x_app_token: str | None = Header(None)):
    _check_token(x_app_token)
    data = _get("/search/address.json", {"query": query, "size": 1})
    docs = data.get("documents", [])
    if docs:
        return {"lat": float(docs[0]["y"]), "lon": float(docs[0]["x"])}
    data = _get("/search/keyword.json", {"query": query, "size": 1})
    docs = data.get("documents", [])
    if docs:
        return {"lat": float(docs[0]["y"]), "lon": float(docs[0]["x"])}
    return {"lat": None, "lon": None}


@app.get("/suggest")
def suggest(keyword: str = Query(...), x_app_token: str | None = Header(None)):
    _check_token(x_app_token)
    if not keyword.strip():
        return []

    def _addr():
        return _get("/search/address.json", {"query": keyword, "size": 5}).get("documents", [])
    def _kw():
        return _get("/search/keyword.json", {"query": keyword, "size": 8}).get("documents", [])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        addr_docs = ex.submit(_addr).result()
        kw_docs   = ex.submit(_kw).result()

    results, seen = [], set()
    for doc in addr_docs:
        key = f"addr:{doc.get('address_name')}:{doc.get('x')}:{doc.get('y')}"
        if key in seen: continue
        seen.add(key)
        results.append({
            "id": key,
            "place_name": doc.get("address_name") or keyword,
            "category_name": "주소",
            "road_address_name": doc.get("road_address", {}).get("address_name", ""),
            "address_name": doc.get("address_name", ""),
            "x": doc["x"], "y": doc["y"],
        })
    for doc in kw_docs:
        key = doc.get("id") or f"place:{doc.get('place_name')}:{doc.get('x')}:{doc.get('y')}"
        if key in seen: continue
        seen.add(key)
        results.append(doc)
    return results[:8]


@app.get("/search/keyword")
def search_keyword(
    query: str = Query(...),
    lat: float = Query(...), lon: float = Query(...),
    radius: int = Query(5000),
    x_app_token: str | None = Header(None),
):
    _check_token(x_app_token)
    results = []
    for page in range(1, 4):
        data = _get("/search/keyword.json", {
            "query": query, "x": lon, "y": lat,
            "radius": radius, "sort": "distance", "page": page, "size": 15,
        })
        docs = data.get("documents", [])
        results.extend(docs)
        if not docs or data.get("meta", {}).get("is_end"):
            break
    return results


@app.get("/search/category")
def search_category(
    code: str = Query(...),
    lat: float = Query(...), lon: float = Query(...),
    radius: int = Query(5000),
    x_app_token: str | None = Header(None),
):
    _check_token(x_app_token)
    results = []
    for page in range(1, 4):
        data = _get("/search/category.json", {
            "category_group_code": code, "x": lon, "y": lat,
            "radius": radius, "sort": "distance", "page": page, "size": 15,
        })
        docs = data.get("documents", [])
        results.extend(docs)
        if not docs or data.get("meta", {}).get("is_end"):
            break
    return results


# ── AI 검색어 추출 ────────────────────────────────────────────

@app.get("/ai-searches")
def ai_searches(description: str = Query(...), x_app_token: str | None = Header(None)):
    _check_token(x_app_token)

    if not _ANTHROPIC_KEY:
        return _DEFAULT_SEARCHES

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=_ANTHROPIC_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    "다음 모임 상황에 맞는 카카오 지도 검색 키워드를 3~5개 뽑아줘. "
                    "JSON 문자열 배열로만 답해. 설명 없이 배열만 출력.\n\n"
                    f"모임 설명: {description}"
                ),
            }],
        )
        keywords = json.loads(msg.content[0].text.strip())
        if not isinstance(keywords, list):
            raise ValueError
        return [{"type": "keyword", "query": str(kw)} for kw in keywords[:5]]
    except Exception:
        return _DEFAULT_SEARCHES
