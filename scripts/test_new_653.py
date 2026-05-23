"""분야별 신간(1개월 이내) 15권씩 653 생성 후 CSV 저장.

분야-알라딘 카테고리 대응 (사용자 정의):
  문학     : 소설, 시, 희곡, 에세이, 장르소설
  인문학   : 인문학, 역사
  종교     : 종교, 역학
  사회과학 : 사회과학, 경제경영, 자기계발
  자연과학 : 과학
  기술과학 : 컴퓨터, 모바일, 건강, 취미, 요리, 살림
  예술     : 예술, 대중문화, 대학교재
  교육     : 대학교재, 외국어
  자기계발 : 자기계발
  기타     : 여행, 전집, 좋은부모

사용 예:
  python scripts/test_new_653.py                         # 전체 10개 분야
  python scripts/test_new_653.py 문학 사회과학           # 특정 분야만
  python scripts/test_new_653.py --api https://six53-test.onrender.com  # 배포 서버

환경변수:
  ALADIN_TTB_KEY      알라딘 TTB 키
  I2M_653_API_BASE    백엔드 URL (기본 http://127.0.0.1:8000)
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

import os
ALADIN_TTB_KEY = os.environ.get("ALADIN_TTB_KEY", "")
ALADIN_ITEM_LIST_URL = "https://www.aladin.co.kr/ttb/api/ItemList.aspx"
ALADIN_ITEM_LOOKUP_URL = "https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
DEFAULT_API_BASE = "http://127.0.0.1:8000"

# ── 알라딘 CategoryId 매핑 ──────────────────────────────────────────────────
# [parent CID] 해당 대분류 전체 신간 반환 (단일 CID로 충분)
# [sub CID 리스트] parent CID 미확인 → 소분류 여러 개 합산
CATEGORY_CID_MAP: dict[str, list[int]] = {
    # 문학: 소설/시/희곡(1) + 에세이(55889) — 장르소설은 1 안에 포함
    "문학":     [1, 55889],
    # 인문학: 인문학(656) + 역사 소분류(한국사일반·세계사일반·조선사)
    "인문학":   [656, 116, 169, 94],
    # 종교: 종교/역학 소분류 (parent CID 1200은 무응답 확인됨)
    # 51568=역학, 51571/51598/51649=기독교(개신교), 51622/51625=천주교, 51640/51643=불교
    "종교":     [51568, 51571, 51598, 51622, 51625, 51640, 51649],
    # 사회과학: 사회과학(798) + 경제경영 소분류 + 자기계발(336)
    "사회과학": [798, 3103, 3060, 2330, 336],
    # 자연과학: 과학 parent
    "자연과학": [987],
    # 기술과학: 컴퓨터/모바일(351) + 건강/취미 소분류
    "기술과학": [351, 53719, 53500, 54708],
    # 예술: 예술/대중문화 소분류 + 대학교재 소분류
    "예술":     [121, 51082, 51092, 51096, 51108, 50970, 51054, 2735, 8563],
    # 교육: 대학교재 소분류 + 외국어 소분류
    "교육":     [2735, 8563, 16034, 49833, 49837, 49855, 49859],
    # 자기계발: 자기계발 parent
    "자기계발": [336],
    # 기타: 좋은부모 소분류 (여행/전집 CID 미확인 — 추후 보완)
    "기타":     [2030, 3390, 73196],
}

CATEGORY_GROUPS = list(CATEGORY_CID_MAP.keys())

# 수집된 책의 알라딘 categoryName에 아래 키워드 중 하나라도 포함돼야 수집 허용
# → 다른 분야 CID가 타 분야 신간을 반환하는 경우 방어
CATEGORY_ACCEPT_KEYWORDS: dict[str, list[str]] = {
    "문학":     ["소설", "시", "희곡", "에세이", "장르"],
    "인문학":   ["인문학", "역사", "철학"],
    "종교":     ["종교", "역학"],
    "사회과학": ["사회과학", "경제경영", "경제", "경영", "자기계발", "사회학", "정치", "통일", "북한"],
    "자연과학": ["과학"],
    "기술과학": ["컴퓨터", "모바일", "인공지능", "건강", "취미", "요리", "살림"],
    "예술":     ["예술", "대중문화", "대학교재", "전문서적"],
    "교육":     ["대학교재", "전문서적", "외국어", "수험"],
    "자기계발": ["자기계발"],
    "기타":     ["여행", "전집", "좋은부모", "임신", "출산", "육아", "교육"],
}

# ai_service.py CATEGORY_MAP과 달리 이 스크립트의 분야명은
# 수집 목적이므로 별도로 관리됨 (처리 시 ai_service 자체 분류 적용)

CUTOFF_DAYS = 30  # 신간 기준: 출판일로부터 N일 이내


# ── 알라딘 API ─────────────────────────────────────────────────────────────

def _aladin_item_list(cid: int, max_results: int = 25) -> list[dict]:
    params = (
        f"ttbkey={ALADIN_TTB_KEY}"
        f"&QueryType=ItemNewAll"
        f"&CategoryId={cid}"
        f"&MaxResults={max_results}"
        f"&SearchTarget=Book"
        f"&output=js"
        f"&Version=20131101"
    )
    url = f"{ALADIN_ITEM_LIST_URL}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read().decode("utf-8")).get("item", [])
    except Exception as e:
        print(f"    [알라딘 ItemList CID={cid}] 오류: {e}", file=sys.stderr)
        return []


def _is_recent(pub_date: str, cutoff: datetime.date) -> bool:
    """pubDate 'YYYY-MM-DD' 형식 파싱 후 cutoff 이후인지 확인.
    출판일이 없거나 파싱 불가한 경우 제외(False).
    """
    if not pub_date or not pub_date.strip():
        return False  # 출판일 미기재 → 제외
    try:
        d = datetime.date.fromisoformat(pub_date[:10])
        return d >= cutoff
    except ValueError:
        return False  # 파싱 실패 → 제외


def collect_isbns_for_group(group: str, target: int = 15, exclude: set[str] | None = None) -> list[dict]:
    """분야 그룹 → 최신순 신간 ISBN 목록 (최대 target개).
    exclude: 이전 테스트에서 이미 사용한 ISBN 집합 — 중복 제외.
    """
    cids = CATEGORY_CID_MAP[group]
    cutoff = datetime.date.today() - datetime.timedelta(days=CUTOFF_DAYS)
    exclude = exclude or set()

    seen: set[str] = set()
    books: list[dict] = []

    # exclude가 있으면 그만큼 더 가져와야 함
    exclude_per_cid = (len(exclude) // len(cids) + 5) if exclude else 0
    per_cid = (30 if len(cids) == 1 else max(10, (target * 2) // len(cids) + 2)) + exclude_per_cid
    per_cid = min(per_cid, 50)  # 알라딘 API 최대 50

    for cid in cids:
        if len(books) >= target:
            break
        items = _aladin_item_list(cid, max_results=per_cid)
        for it in items:
            if len(books) >= target:
                break
            isbn = str(it.get("isbn13") or it.get("isbn") or "").strip()
            if not isbn or isbn in seen or isbn in exclude:
                continue
            # 카테고리 키워드 필터 — 타 분야 유입 방지
            cat_name = str(it.get("categoryName") or "")
            accept_kws = CATEGORY_ACCEPT_KEYWORDS.get(group, [])
            if accept_kws and not any(kw in cat_name for kw in accept_kws):
                continue
            pub_date = str(it.get("pubDate") or "")
            if not _is_recent(pub_date, cutoff):
                continue
            seen.add(isbn)
            books.append({
                "isbn": isbn,
                "title": it.get("title", ""),
                "authors": it.get("author", ""),
                "pub_date": pub_date,
                "aladin_category": it.get("categoryName", ""),
            })
        time.sleep(0.15)

    return books[:target]


# ── 653 API 호출 ────────────────────────────────────────────────────────────

def call_653_api(isbn: str, api_base: str) -> dict:
    body = json.dumps({"isbn": isbn}).encode("utf-8")
    req = urllib.request.Request(
        f"{api_base}/api/field653",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


# ── CSV 출력 ────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "category_group", "isbn", "pub_date",
    "title", "authors", "aladin_category",
    "description",
    "success", "kw_count", "keywords", "tag_653",
    "error",
]


def _row(group: str, book: dict, result: dict | None, error: str = "") -> dict:
    # description: 성공 시 API 응답의 aladin.description 사용 (이미 정제된 값)
    description = ""
    if result and result.get("success"):
        aladin_meta = result.get("aladin") or {}
        description = str(aladin_meta.get("description") or "")[:500]

    if result and result.get("success"):
        kws = result.get("keywords") or []
        return {
            "category_group": group,
            "isbn": book["isbn"],
            "pub_date": book.get("pub_date", ""),
            "title": book["title"][:60],
            "authors": book["authors"][:40],
            "aladin_category": book.get("aladin_category", "")[:60],
            "description": description,
            "success": "TRUE",
            "kw_count": len(kws),
            "keywords": "|".join(kws),
            "tag_653": result.get("tag_653", ""),
            "error": "",
        }
    return {
        "category_group": group,
        "isbn": book["isbn"],
        "pub_date": book.get("pub_date", ""),
        "title": book["title"][:60],
        "authors": book["authors"][:40],
        "aladin_category": book.get("aladin_category", "")[:60],
        "description": description,
        "success": "FALSE",
        "kw_count": 0,
        "keywords": "",
        "tag_653": "",
        "error": error,
    }


# ── 메인 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="분야별 신간 653 배치 테스트")
    parser.add_argument("categories", nargs="*", help="대상 분야 (기본: 전체)")
    parser.add_argument("--api", default=None, help="백엔드 URL")
    parser.add_argument("--target", type=int, default=15, help="분야당 목표 권수 (기본 15)")
    parser.add_argument("--output", default=None, help="저장 CSV 경로")
    parser.add_argument("--exclude", default=None, help="제외할 ISBN이 담긴 CSV 경로 (이전 테스트 결과)")
    args = parser.parse_args()

    api_base = (args.api or os.environ.get("I2M_653_API_BASE", DEFAULT_API_BASE)).rstrip("/")
    target = args.target

    groups = [g for g in args.categories if g in CATEGORY_GROUPS] if args.categories else CATEGORY_GROUPS
    unknown = [g for g in args.categories if g not in CATEGORY_GROUPS] if args.categories else []
    if unknown:
        print(f"⚠  알 수 없는 분야 무시: {unknown}")
        print(f"   사용 가능: {CATEGORY_GROUPS}")

    # 제외 ISBN 로드
    exclude_isbns: set[str] = set()
    if args.exclude:
        excl_path = Path(args.exclude)
        if excl_path.exists():
            with open(excl_path, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    isbn = str(row.get("isbn") or "").strip()
                    if isbn:
                        exclude_isbns.add(isbn)
            print(f"  exclude: {len(exclude_isbns)}개 ISBN 로드 ({excl_path.name})")
        else:
            print(f"  WARNING: --exclude 파일 없음: {excl_path}", file=sys.stderr)

    if not ALADIN_TTB_KEY:
        print("ERROR: ALADIN_TTB_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.output) if args.output else Path(__file__).resolve().parents[1] / "backend" / f"653_신간테스트_{ts}.csv"

    print(f"[{ts}] groups: {groups}")
    print(f"  api: {api_base}")
    print(f"  target: {target}  /  cutoff: {CUTOFF_DAYS}d")
    print(f"  output: {out_path}")
    print()

    rows: list[dict] = []

    for group in groups:
        print(f"▶ [{group}] ISBN 수집 중…")
        books = collect_isbns_for_group(group, target=target, exclude=exclude_isbns)
        print(f"  collected: {len(books)}", end="")
        if len(books) < target:
            print(f"  (only {len(books)} found, continuing)", end="")
        print()

        if not books:
            print(f"  SKIP [{group}]: no new books\n")
            continue

        for i, book in enumerate(books, 1):
            isbn = book["isbn"]
            title_short = book["title"][:30]
            print(f"  [{i:02d}/{len(books)}] {isbn}  {title_short}…", end=" ", flush=True)
            try:
                result = call_653_api(isbn, api_base)
                kw_count = len(result.get("keywords") or [])
                ok = "OK" if result.get("success") else "NG"
                print(f"{ok}  kw={kw_count}")
                rows.append(_row(group, book, result))
            except urllib.error.HTTPError as e:
                msg = f"HTTP {e.code}"
                try:
                    msg += f": {e.read().decode('utf-8', errors='replace')[:120]}"
                except Exception:
                    pass
                print(f"NG  {msg}")
                rows.append(_row(group, book, None, error=msg))
            except Exception as e:
                msg = str(e)[:120]
                print(f"NG  {msg}")
                rows.append(_row(group, book, None, error=msg))
            time.sleep(0.3)

        success_count = sum(1 for r in rows if r["category_group"] == group and r["success"] == "TRUE")
        print(f"  => success {success_count}/{len(books)}\n")

    # CSV 저장
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    ok_count = sum(1 for r in rows if r["success"] == "TRUE")
    print("=" * 60)
    print(f"Done: {ok_count}/{total} OK")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
