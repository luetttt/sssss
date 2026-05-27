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
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": (
                    "다음 모임 상황에 맞는 카카오맵 장소 검색 방법을 3~5개 제안해.\n"
                    "카테고리 검색이 키워드 검색보다 정확하므로, 적합한 카테고리가 있으면 반드시 써.\n"
                    "키워드 검색은 카테고리로 표현 못 하는 틈새 장소(굿즈샵·보드게임카페 등)에만 사용해.\n\n"
                    "사용 가능한 카테고리 코드:\n"
                    "FD6=음식점(한식/고기집/레스토랑 등), CE7=카페·디저트,\n"
                    "AC5=숙박(호텔/모텔/펜션), AT4=관광명소·여행지,\n"
                    "CT1=문화시설(미술관/박물관/공연장), SW8=지하철역, MT1=대형마트\n\n"
                    "반드시 JSON 배열만 출력. 마크다운·설명 없이.\n"
                    "형식: [{\"type\":\"category\",\"code\":\"코드\"} 또는 {\"type\":\"keyword\",\"query\":\"검색어\"}]\n\n"
                    "예시1(회식): [{\"type\":\"category\",\"code\":\"FD6\"},{\"type\":\"keyword\",\"query\":\"룸 식당\"}]\n"
                    "예시2(오타쿠 모임): [{\"type\":\"keyword\",\"query\":\"굿즈샵\"},{\"type\":\"keyword\",\"query\":\"만화카페\"}]\n"
                    "예시3(커플 여행): [{\"type\":\"category\",\"code\":\"AC5\"},{\"type\":\"category\",\"code\":\"AT4\"}]\n\n"
                    f"모임 설명: {description}"
                ),
            }],
        )
        text = msg.content[0].text.strip()
        # 마크다운 코드블록 제거
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        searches = json.loads(text)
        if not isinstance(searches, list) or not searches:
            raise ValueError
        # 유효한 항목만 통과 (type/code 또는 type/query 구조 검증)
        valid = []
        for s in searches[:5]:
            if not isinstance(s, dict):
                continue
            if s.get("type") == "category" and s.get("code"):
                valid.append({"type": "category", "code": str(s["code"])})
            elif s.get("type") == "keyword" and s.get("query"):
                valid.append({"type": "keyword", "query": str(s["query"])})
        if not valid:
            raise ValueError
        return valid
    except Exception:
        return _DEFAULT_SEARCHES
