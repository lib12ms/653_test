"""신간 300권 대규모 배치 653 생성 스크립트
=========================================
알라딘 신간 목록(ItemNewAll)에서 ISBN을 자동 수집한 뒤
653 필드를 생성하고 설명·목차를 포함한 CSV를 저장합니다.

실행:
    cd backend
    python batch_300.py                 # 기본 300권
    python batch_300.py --count 100     # 수량 조절
    python batch_300.py --days 60       # 신간 기준 기간(기본 30일)
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import os
import sys
import time
from pathlib import Path

os.environ["ALLOW_INSECURE_SSL_FALLBACK"] = "true"
os.environ["INSECURE_SSL_FALLBACK_HOSTS_CSV"] = "www.aladin.co.kr"

sys.path.insert(0, str(Path(__file__).parent))

import httpx

from app import ai_service
from app.ai_service import build_marc_653_line, get_category_group
from app.config import Settings
from app.fetcher import fetch_aladin_for_653, fetch_secondary_metadata_hint, merge_aladin_with_nlk
from app.models import parse_653_keywords
from app.nlk_client import fetch_kdc_content_code_by_isbn

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

ALADIN_ITEM_LIST_URL = "https://www.aladin.co.kr/ttb/api/ItemList.aspx"
ALADIN_MAX_PER_PAGE = 50  # Aladin API 페이지당 최대
CONCURRENCY = 1            # 순차 처리(rate limit·서버 부하 방지)
REQUEST_DELAY_S = 3        # AI API 호출 간 대기 시간(초)

# 분야 다양성을 위한 알라딘 CategoryId 목록
# CID 출처: ItemList API + ItemSearch API 역추적으로 실측 검증 (2026-07-07)
# 검증 방법: probe 스크립트로 CID별 결과 건수·카테고리명 직접 확인
CATEGORY_CID_MAP: dict[str, int] = {
    "소설/시/희곡": 1,        # 검증됨
    "에세이": 55889,          # 검증됨
    "인문학": 656,            # 검증됨
    "역사": 169,              # 역사>세계사 일반 (구 CID=74는 경제경영 하위)
    "종교/역학": 51573,       # 종교/역학>기독교 일반 (구 CID=1637은 0건)
    "사회과학": 51090,        # 사회과학>사회문제 일반 (구 CID=51은 0건)
    "경제경영": 170,          # 검증됨
    "자기계발": 336,          # 검증됨
    "자연과학": 987,          # 검증됨
    "IT컴퓨터": 351,          # 검증됨
    "예술/대중문화": 50950,   # 예술/대중문화>미학/예술이론 (구 CID=517은 악보 하위)
    "생활실용": 1230,         # 검증됨
    "교육": 51543,            # 사회과학>교육학>교육 일반 (구 CID=1297은 0건)
    "외국어": 1322,           # 검증됨
    "여행": 1196,             # 검증됨
}

# 카테고리 수집이 target에 미달할 때 전체 신간(CategoryId=0)으로 보충
FALLBACK_CID = 0

REVIEW_SCORE_THRESHOLD = 0.55
REVIEW_MIN_KEYWORDS = 4


# ── Phase 1: 알라딘 신간 ISBN 수집 ──────────────────────────────────────────

def _aladin_item_list_page(
    ttb_key: str,
    cid: int,
    page_no: int,
    http: httpx.Client,
    max_results: int = ALADIN_MAX_PER_PAGE,
) -> list[dict]:
    """알라딘 ItemList API — 한 페이지 조회."""
    params = {
        "ttbkey": ttb_key,
        "QueryType": "ItemNewAll",
        "CategoryId": cid,
        "page": page_no,
        "MaxResults": max_results,
        "SearchTarget": "Book",
        "output": "js",
        "Version": "20131101",
    }
    try:
        r = http.get(ALADIN_ITEM_LIST_URL, params=params, timeout=20)
        r.raise_for_status()
        return r.json().get("item", [])
    except Exception as e:
        print(f"    [알라딘 CID={cid} p={page_no}] 오류: {e}", file=sys.stderr)
        return []


def _is_recent(pub_date: str, cutoff: datetime.date) -> bool:
    if not pub_date or not pub_date.strip():
        return False
    try:
        return datetime.date.fromisoformat(pub_date[:10]) >= cutoff
    except ValueError:
        return False


_TITLE_EXCLUDE = (
    "[북토크]", "[세트]", "[전집]", "[박스]", "[큰글자]", "(큰글자)",
    "큰글자판", "큰글자본", "[합본]", "[스페셜에디션]",
)
_CATEGORY_EXCLUDE = (
    "어린이", "청소년", "유아", "아동", "잡지", "만화", "학습만화",
)

def _is_general_adult_book(item: dict) -> bool:
    """성인 일반단행본 여부 판별 — False이면 수집에서 제외."""
    title = (item.get("title") or "")
    category = (item.get("categoryName") or "")
    if any(p in title for p in _TITLE_EXCLUDE):
        return False
    if any(k in category for k in _CATEGORY_EXCLUDE):
        return False
    return True


def collect_isbns(ttb_key: str, target: int, cutoff_days: int) -> list[dict]:
    """알라딘 신간 목록에서 target 권만큼 ISBN+기초정보를 수집.

    카테고리별로 균등하게 수집하되, 중복 ISBN은 제거.
    cutoff_days 이내 출판된 도서만 포함.
    성인 일반단행본이 아닌 도서(어린이·청소년·세트·잡지 등)는 제외.
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=cutoff_days)
    per_cat = max(10, (target * 2) // len(CATEGORY_CID_MAP) + 5)

    seen: set[str] = set()
    books: list[dict] = []

    with httpx.Client(verify=False, timeout=20) as http:
        for cat_name, cid in CATEGORY_CID_MAP.items():
            cat_books: list[dict] = []
            print(f"  [{cat_name}] CID={cid} ...", end=" ", flush=True)

            for page in range(1, 6):
                if len(cat_books) >= per_cat:
                    break
                items = _aladin_item_list_page(ttb_key, cid, page, http)
                if not items:
                    break
                for it in items:
                    isbn = str(it.get("isbn13") or it.get("isbn") or "").strip()
                    if not isbn or isbn in seen:
                        continue
                    pub_date = str(it.get("pubDate") or "")
                    if not _is_recent(pub_date, cutoff):
                        continue
                    if not _is_general_adult_book(it):
                        continue
                    seen.add(isbn)
                    cat_books.append({
                        "isbn": isbn,
                        "title": (it.get("title") or "")[:80],
                        "authors": (it.get("author") or "")[:50],
                        "pub_date": pub_date,
                        "aladin_category": (it.get("categoryName") or "")[:80],
                    })
                    if len(cat_books) >= per_cat:
                        break
                time.sleep(0.2)

            books.extend(cat_books)
            print(f"{len(cat_books)}권")

            if len(books) >= target * 2:
                break

        # 카테고리 수집이 target에 미달하면 전체 신간으로 보충
        if len(books) < target:
            shortage = target - len(books)
            print(f"  [보충] 카테고리 수집 부족 -> 전체 신간(CID=0)에서 {shortage}권 추가 수집")
            for page in range(1, 10):
                if len(books) >= target:
                    break
                items = _aladin_item_list_page(ttb_key, FALLBACK_CID, page, http)
                if not items:
                    break
                for it in items:
                    isbn = str(it.get("isbn13") or it.get("isbn") or "").strip()
                    if not isbn or isbn in seen:
                        continue
                    pub_date = str(it.get("pubDate") or "")
                    if not _is_recent(pub_date, cutoff):
                        continue
                    if not _is_general_adult_book(it):
                        continue
                    seen.add(isbn)
                    books.append({
                        "isbn": isbn,
                        "title": (it.get("title") or "")[:80],
                        "authors": (it.get("author") or "")[:50],
                        "pub_date": pub_date,
                        "aladin_category": (it.get("categoryName") or "")[:80],
                    })
                time.sleep(0.2)

    return books[:target]


# ── Phase 2: 653 생성 ────────────────────────────────────────────────────────

async def process_isbn(
    book: dict,
    idx: int,
    total: int,
    settings: Settings,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> dict:
    isbn = book["isbn"]
    result: dict = {
        "순번": idx,
        "ISBN": isbn,
        "제목": book.get("title", ""),
        "저자": book.get("authors", ""),
        "출판일": book.get("pub_date", ""),
        "알라딘카테고리": book.get("aladin_category", ""),
        "카테고리(정제)": "",
        "부가기호": "",
        "분야그룹": "",
        "설명": "",
        "목차": "",
        "653필드": "",
        "키워드목록": "",
        "AI생성수": "",
        "차단수": "",
        "최종수": "",
        "품질점수": "",
        "경고플래그": "",
        "검토필요": "",
        "원생성키워드": "",
        "차단키워드": "",
        "fallback키워드": "",
        "fallback출처": "",
        "오류": "",
    }

    async with sem:
        if idx > 1:
            await asyncio.sleep(REQUEST_DELAY_S)
        try:
            base_meta, _ = await fetch_aladin_for_653(
                isbn, settings=settings, include_debug=True, client=client
            )
            result["제목"] = base_meta.title or result["제목"]
            result["저자"] = base_meta.authors or result["저자"]
            result["카테고리(정제)"] = base_meta.category
            result["설명"] = base_meta.description[:400] if base_meta.description else ""
            result["목차"] = base_meta.toc[:400] if base_meta.toc else ""

            nlk, hint_src, _ = await fetch_secondary_metadata_hint(isbn, settings=settings, client=client)
            content_code = await fetch_kdc_content_code_by_isbn(isbn, settings=settings, client=client)
            merge_src = "kpipa" if hint_src == "kpipa" else "none"
            meta = merge_aladin_with_nlk(
                base_meta, nlk, settings=settings, secondary_source=merge_src, content_code=content_code
            )
            result["부가기호"] = content_code
            result["분야그룹"] = get_category_group(meta.category, meta.content_code)

            raw_line, err, _usage, quality = await ai_service.generate_653_subfield_line(
                meta,
                max_keywords=settings.max_keywords_653,
                min_keywords=settings.min_keywords_653,
                settings=settings,
            )
            if err:
                result["오류"] = f"AI 오류: {err}"
                return result

            tag = build_marc_653_line(raw_line)
            kws = parse_653_keywords(tag, max_keywords=settings.max_keywords_653)
            result["653필드"] = tag
            result["키워드목록"] = " / ".join(kws)

            if quality:
                result["AI생성수"] = quality.ai_raw_count
                result["차단수"] = quality.filtered_count
                result["최종수"] = quality.final_count
                result["품질점수"] = quality.quality_score
                result["경고플래그"] = " | ".join(quality.flags)
                needs_review = (
                    quality.quality_score < REVIEW_SCORE_THRESHOLD
                    or quality.final_count < REVIEW_MIN_KEYWORDS
                    or bool(quality.flags)
                )
                result["검토필요"] = "Y" if needs_review else "N"
                result["원생성키워드"] = " / ".join(quality.raw_keywords)
                result["차단키워드"] = " / ".join(quality.blocked_keywords)
                result["fallback키워드"] = " / ".join(quality.fallback_keywords)
                fb_sources = []
                if "텍스트fallback사용" in quality.flags:
                    fb_sources.append("텍스트")
                if "카테고리fallback사용" in quality.flags:
                    fb_sources.append("카테고리")
                result["fallback출처"] = "+".join(fb_sources) if fb_sources else ""

            review_mark = " ★" if result["검토필요"] == "Y" else ""
            print(
                f"  [{idx:>3}/{total}] {result['분야그룹']:<8}  "
                f"Q={quality.quality_score if quality else '?':.3f}{review_mark}  "
                f"{base_meta.title[:28]}"
            )

        except Exception as e:
            result["오류"] = str(e)[:200]
            print(f"  [{idx:>3}/{total}] ERR  {isbn}: {str(e)[:60]}")

    return result


# ── Phase 3: CSV 저장 ────────────────────────────────────────────────────────

CSV_COLUMNS = [
    "순번", "ISBN", "제목", "저자", "출판일",
    "알라딘카테고리", "카테고리(정제)", "부가기호", "분야그룹",
    "설명", "목차",
    "653필드", "키워드목록",
    "AI생성수", "차단수", "최종수", "품질점수", "경고플래그", "검토필요",
    "원생성키워드", "차단키워드", "fallback키워드", "fallback출처",
    "오류",
]

REVIEW_COLUMNS = [
    "순번", "ISBN", "제목", "카테고리(정제)", "분야그룹",
    "설명", "목차", "키워드목록", "품질점수", "경고플래그", "오류",
]


def save_csv(results: list[dict], out_dir: Path, count: int = 0) -> tuple[Path, Path | None]:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    n = count or len(results)
    full_path = out_dir / f"653_신간{n}_{ts}.csv"

    with open(full_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    review_rows = [r for r in results if r.get("검토필요") == "Y" or r.get("오류")]
    review_path: Path | None = None
    if review_rows:
        review_path = out_dir / f"653_신간{n}_검토_{ts}.csv"
        with open(review_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=REVIEW_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(review_rows)

    return full_path, review_path


# ── 진입점 ───────────────────────────────────────────────────────────────────

async def main(target: int, cutoff_days: int) -> None:
    settings = Settings(
        allow_insecure_ssl_fallback=True,
        insecure_ssl_fallback_hosts_csv="www.aladin.co.kr",
    )
    if not settings.aladin_ttb_key:
        print("오류: ALADIN_TTB_KEY가 .env에 설정되지 않았습니다.", file=sys.stderr)
        sys.exit(1)
    if not settings.openai_api_key:
        print("오류: OPENAI_API_KEY가 .env에 설정되지 않았습니다.", file=sys.stderr)
        sys.exit(1)

    # ── 1단계: ISBN 수집 ─────────────────────────────────────────────────────
    print(f"\n[1단계] 알라딘 신간 ISBN 수집 - 목표: {target}권 (최근 {cutoff_days}일 이내)")
    books = collect_isbns(settings.aladin_ttb_key, target, cutoff_days)
    print(f"  → 수집 완료: {len(books)}권\n")

    if not books:
        print("수집된 ISBN이 없습니다. 종료.")
        sys.exit(1)

    # ── 2단계: 653 생성 ──────────────────────────────────────────────────────
    total = len(books)
    print(f"[2단계] 653 생성 시작: {total}권 (순차 처리, 약 {total * REQUEST_DELAY_S // 60}분 소요 예상)\n")

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        tasks = [
            process_isbn(book, idx + 1, total, settings, client, sem)
            for idx, book in enumerate(books)
        ]
        results = await asyncio.gather(*tasks)

    results_sorted = sorted(results, key=lambda r: r["순번"])

    # ── 3단계: CSV 저장 ──────────────────────────────────────────────────────
    out_dir = Path(__file__).parent
    full_path, review_path = save_csv(list(results_sorted), out_dir, count=target)

    ok = sum(1 for r in results_sorted if not r.get("오류"))
    err = sum(1 for r in results_sorted if r.get("오류"))
    review = sum(1 for r in results_sorted if r.get("검토필요") == "Y")
    scores = [r["품질점수"] for r in results_sorted if isinstance(r.get("품질점수"), float)]
    avg_score = round(sum(scores) / len(scores), 3) if scores else "-"

    # 분야그룹별 통계
    from collections import Counter
    group_counter = Counter(r["분야그룹"] for r in results_sorted if r.get("분야그룹"))

    print(f"\n{'='*56}")
    print(f"완료: 성공 {ok}권 / 오류 {err}권 / 총 {total}권")
    print(f"품질: 평균점수 {avg_score}  |  검토필요 {review}권")
    print(f"{'='*56}")
    print("분야그룹별 처리 수:")
    for group, cnt in sorted(group_counter.items(), key=lambda x: -x[1]):
        print(f"  {group:<12} {cnt:>3}권")
    print(f"{'='*56}")
    print(f"전체 결과: {full_path.name}")
    if review_path:
        print(f"검토 대기: {review_path.name}  ({review}건)")


def cli() -> None:
    parser = argparse.ArgumentParser(description="알라딘 신간 대규모 배치 653 생성")
    parser.add_argument("--count", type=int, default=500, help="수집 목표 권수 (기본 500)")
    parser.add_argument("--days", type=int, default=30, help="신간 기준 일수 (기본 30)")
    args = parser.parse_args()
    asyncio.run(main(args.count, args.days))


if __name__ == "__main__":
    cli()
