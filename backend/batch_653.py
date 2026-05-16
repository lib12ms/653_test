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
    "9791168224506",
    "9791194630456",
    "9791175910676",
    "9791167853080",
    "9791173325878",
    "9791167701565",
    "9791194285274",
    "9791187135418",
    "9791167376565",
    "9791141603182",
    "9791141603243",
    "9788976048059",
    "9791194127369",
    "9791173630457",
    "9788936425371",
    "9788932044682",
    "9791190365901",
    "9791159334757",
    "9791155819269",
    "9788972972068",
    "9788932324913",
    "9791139730937",
    "9791160871579",
    "9791155644331",
    "9791193000953",
    "9791192742632",
    "9791199489530",
    "9791172612566",
    "9791189697679",
    "9791199350144",
    "9791193027615",
    "9791124516157",
    "9788990530967",
    "9788931505238",
]

CONCURRENCY = 1  # 순차 처리 (rate limit 방지)
REQUEST_DELAY_S = 3  # 요청 간 대기 시간(초)

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

            raw_line, err, _usage = await ai_service.generate_653_subfield_line(
                meta,
                max_keywords=settings.max_keywords_653,
                min_keywords=settings.min_keywords_653,
                settings=settings,
                client=client,
            )
            if err:
                result["오류"] = f"AI 오류: {err}"
                return result

            tag = build_marc_653_line(raw_line)
            kws = parse_653_keywords(tag, max_keywords=settings.max_keywords_653)
            result["653필드"] = tag
            result["키워드목록"] = " / ".join(kws)

            print(f"  [{idx:>2}/{total}] OK  {base_meta.title[:30]}")
        except Exception as e:
            result["오류"] = str(e)
            print(f"  [{idx:>2}/{total}] ERR {isbn}: {e}")

    return result


# ── 저장 ─────────────────────────────────────────────────────────────────

CSV_COLUMNS = ["순번", "ISBN", "제목", "저자", "카테고리", "653필드", "키워드목록", "오류"]


def save_csv(results: list[dict], out_dir: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"653_배치_{ts}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(results)
    return path


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
    csv_path = save_csv(list(results), out_dir=Path(__file__).parent)

    ok = sum(1 for r in results if not r["오류"])
    err = sum(1 for r in results if r["오류"])
    print(f"\n완료: 성공 {ok}권 / 오류 {err}권")
    print(f"CSV 저장: {csv_path.name}")


if __name__ == "__main__":
    asyncio.run(main())
