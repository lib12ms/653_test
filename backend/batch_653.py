"""
ISBN 배치 653 생성 스크립트
============================
ISBN 목록 전체를 대상으로 653 필드를 생성하고 CSV로 저장합니다.

실행:
    cd e:/653_test/backend
    python batch_653.py
"""
from __future__ import annotations

import asyncio
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ["ALLOW_INSECURE_SSL_FALLBACK"] = "true"
os.environ["INSECURE_SSL_FALLBACK_HOSTS_CSV"] = "www.aladin.co.kr"

sys.path.insert(0, str(Path(__file__).parent))

import httpx

from app import ai_service
from app.ai_service import build_marc_653_line
from app.config import Settings
from app.fetcher import fetch_aladin_for_653, fetch_secondary_metadata_hint, merge_aladin_with_nlk
from app.models import parse_653_keywords

# ── ISBN 목록 ─────────────────────────────────────────────────────────────
ISBNS: list[str] = [
    "9791159056710",
    "9791185136660",
    "9791189898700",
    "9791169100885",
    "9791168150140",
    "9791187705246",
    "9788963194448",
    "9788966272136",
    "9788960907195",
    "9791188071326",
    "9788990944740",
    "9788932039985",
    "9791170401308",
]

CONCURRENCY = 1  # 순차 처리 (rate limit 방지)
REQUEST_DELAY_S = 3  # 요청 간 대기 시간(초)

# ── 검토 필요 판단 기준 ────────────────────────────────────────────────────
REVIEW_SCORE_THRESHOLD = 0.7   # 품질점수 미만이면 검토 대상
REVIEW_MIN_KEYWORDS = 5        # 최종 키워드 수 미달이면 검토 대상

# ── 처리 함수 ─────────────────────────────────────────────────────────────

async def process_isbn(
    isbn: str,
    idx: int,
    total: int,
    settings: Settings,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> dict:
    result = {
        "순번": idx,
        "ISBN": isbn,
        "제목": "",
        "저자": "",
        "카테고리": "",
        "653필드": "",
        "키워드목록": "",
        "AI생성수": "",
        "차단수": "",
        "최종수": "",
        "품질점수": "",
        "경고플래그": "",
        "검토필요": "",
        "오류": "",
    }
    async with sem:
        if idx > 1:
            await asyncio.sleep(REQUEST_DELAY_S)
        try:
            base_meta, _ = await fetch_aladin_for_653(
                isbn, settings=settings, include_debug=True, client=client
            )
            result["제목"] = base_meta.title
            result["저자"] = base_meta.authors
            result["카테고리"] = base_meta.category

            nlk, hint_src = await fetch_secondary_metadata_hint(isbn, settings=settings, client=client)
            merge_src = "kpipa" if hint_src == "kpipa" else "none"
            meta = merge_aladin_with_nlk(base_meta, nlk, settings=settings, secondary_source=merge_src)

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

            review_mark = " ★검토" if result["검토필요"] == "Y" else ""
            print(f"  [{idx:>2}/{total}] OK  {base_meta.title[:30]}  "
                  f"품질={quality.quality_score if quality else '?'}{review_mark}")
        except Exception as e:
            result["오류"] = str(e)
            print(f"  [{idx:>2}/{total}] ERR {isbn}: {e}")

    return result


# ── 저장 ─────────────────────────────────────────────────────────────────

CSV_COLUMNS = [
    "순번", "ISBN", "제목", "저자", "카테고리",
    "653필드", "키워드목록",
    "AI생성수", "차단수", "최종수", "품질점수", "경고플래그", "검토필요",
    "오류",
]

REVIEW_COLUMNS = [
    "순번", "ISBN", "제목", "카테고리",
    "키워드목록", "품질점수", "경고플래그", "오류",
]


def save_csv(results: list[dict], out_dir: Path) -> tuple[Path, Path | None]:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 전체 결과
    full_path = out_dir / f"653_배치_{ts}.csv"
    with open(full_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(results)

    # 검토 필요 항목만 별도 저장
    review_rows = [r for r in results if r.get("검토필요") == "Y" or r.get("오류")]
    review_path: Path | None = None
    if review_rows:
        review_path = out_dir / f"653_검토대기_{ts}.csv"
        with open(review_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=REVIEW_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(review_rows)

    return full_path, review_path


# ── 진입점 ────────────────────────────────────────────────────────────────

async def main() -> None:
    settings = Settings(
        allow_insecure_ssl_fallback=True,
        insecure_ssl_fallback_hosts_csv="www.aladin.co.kr",
    )
    total = len(ISBNS)
    print(f"배치 653 생성 시작: {total}권 | 동시처리: {CONCURRENCY}건")
    print("(OpenAI API 호출 포함 - 약 3~5분 소요 예상)\n")

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        tasks = [
            process_isbn(isbn, idx + 1, total, settings, client, sem)
            for idx, isbn in enumerate(ISBNS)
        ]
        results = await asyncio.gather(*tasks)

    results = sorted(results, key=lambda r: r["순번"])
    full_path, review_path = save_csv(list(results), out_dir=Path(__file__).parent)

    ok = sum(1 for r in results if not r["오류"])
    err = sum(1 for r in results if r["오류"])
    review = sum(1 for r in results if r.get("검토필요") == "Y")
    scores = [r["품질점수"] for r in results if isinstance(r.get("품질점수"), float)]
    avg_score = round(sum(scores) / len(scores), 3) if scores else "-"

    print(f"\n{'='*50}")
    print(f"완료: 성공 {ok}권 / 오류 {err}권 / 총 {total}권")
    print(f"품질: 평균점수 {avg_score}  |  검토필요 {review}권")
    print(f"{'='*50}")
    print(f"전체 결과: {full_path.name}")
    if review_path:
        print(f"검토 대기: {review_path.name}  ({review}건)")


if __name__ == "__main__":
    asyncio.run(main())
