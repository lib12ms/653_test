"""
653 편차 진단 스크립트
=====================
다양한 분야의 ISBN을 대상으로 전체 파이프라인을 실행하고,
입력 데이터 품질과 출력 품질을 비교해 편차 원인을 파악합니다.

실행:
    cd e:/653_test/backend
    python diagnose_653.py

ISBN 목록은 하단 TEST_BOOKS 리스트에서 자유롭게 수정하세요.
"""
from __future__ import annotations

import asyncio
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

# 알라딘 SSL 우회 (진단 전용 — 프로덕션에서는 .env에서 관리)
os.environ["ALLOW_INSECURE_SSL_FALLBACK"] = "true"
os.environ["INSECURE_SSL_FALLBACK_HOSTS_CSV"] = "www.aladin.co.kr"

sys.path.insert(0, str(Path(__file__).parent))

import httpx

from app import ai_service
from app.ai_service import (
    build_marc_653_line,
    get_category_group,
    parse_keyword_line,
)
from app.config import Settings
from app.fetcher import fetch_aladin_for_653, fetch_secondary_metadata_hint, merge_aladin_with_nlk
from app.models import parse_653_keywords
from app.preprocess import build_forbidden_set, should_keep_keyword

# ── 테스트 ISBN 목록 (자유롭게 추가/수정) ─────────────────────────────────
# (ISBN13, 라벨)  — 라벨은 식별용으로만 사용
TEST_BOOKS: list[tuple[str, str]] = [
    ("9788936433598", "채식주의자 [문학/소설]"),
    ("9788954641326", "82년생 김지영 [사회과학]"),
    ("9788935213610", "돈의 속성 [경제경영]"),
    ("9791198363510", "아몬드 [문학/소설]"),
    ("9788901224756", "원씽 [자기계발]"),
    ("9788983712691", "코스모스 [자연과학]"),
    ("9788974744618", "총균쇠 [인문학]"),
    ("9791164050871", "미라클 모닝 [자기계발]"),
]


# ── 진단 함수 ─────────────────────────────────────────────────────────────

async def diagnose_isbn(
    isbn: str,
    label: str,
    settings: Settings,
    client: httpx.AsyncClient,
) -> dict:
    result: dict = {
        "isbn": isbn,
        "label": label,
        "error": None,
        "title": "",
        "category": "",
        "cat_group": "",
        # 입력 데이터 품질
        "toc_raw_len": 0,      # 알라딘 원본 목차 길이
        "toc_merged_len": 0,   # KPIPA 목차 병합 후 목차 길이
        "desc_raw_len": 0,     # 알라딘 원본 설명 길이
        "desc_merged_len": 0,  # 병합 후 설명 길이(알라딘만; KPIPA는 설명 미병합)
        # KPIPA(목차만)
        "hint_src": "",
        "kpipa_toc_hint_len": 0,
        "kpipa_toc_added": False,
        "kpipa_configured": False,
        # 출력 품질
        "ai_kw_count": 0,      # AI 응답에서 유효한 키워드 수
        "final_kw_count": 0,   # 후처리 후 최종 키워드 수
        "backup_used": False,  # backup 후보가 보충에 사용됐는지
        "keywords": [],
        "tag_653": "",
        # 편차 지표
        "data_richness": 0,    # 목차+설명 합산 글자 수 (풍부도 지표)
        "flag": "",            # 주의 플래그
    }

    try:
        result["kpipa_configured"] = bool(settings.kpipa_enable and settings.kpipa_api_key)

        # 1. 알라딘 수집
        base_meta, debug = await fetch_aladin_for_653(
            isbn, settings=settings, include_debug=True, client=client
        )
        result["title"] = base_meta.title
        result["category"] = base_meta.category
        result["cat_group"] = get_category_group(base_meta.category)
        result["toc_raw_len"] = len(base_meta.toc)
        result["desc_raw_len"] = len(base_meta.description)

        # 2. KPIPA 목차 힌트(앱 본선에서 NLK 미사용)
        hint, hint_src = await fetch_secondary_metadata_hint(isbn, settings=settings, client=client)
        result["hint_src"] = hint_src
        result["kpipa_toc_hint_len"] = len((hint.toc or "").strip())

        # 3. 병합(알라딘 + KPIPA 목차만)
        merge_src = "kpipa" if hint_src == "kpipa" else "none"
        meta = merge_aladin_with_nlk(base_meta, hint, settings=settings, secondary_source=merge_src)
        result["toc_merged_len"] = len(meta.toc)
        result["desc_merged_len"] = len(meta.description)
        result["kpipa_toc_added"] = result["toc_merged_len"] > result["toc_raw_len"]
        result["data_richness"] = result["toc_merged_len"] + result["desc_merged_len"]

        # 4. 653 생성
        raw_line, err, _usage = await ai_service.generate_653_subfield_line(
            meta,
            max_keywords=settings.max_keywords_653,
            min_keywords=settings.min_keywords_653,
            settings=settings,
            client=client,
        )
        if err:
            result["error"] = f"AI 오류: {err}"
            return result

        # AI 원본 유효 키워드 수 측정
        forbidden = build_forbidden_set(meta.title, meta.authors)
        raw_kws = parse_keyword_line(raw_line or "")
        ai_valid = [kw for kw in raw_kws if should_keep_keyword(kw, forbidden)]
        result["ai_kw_count"] = len(ai_valid)

        # 최종 결과
        tag = build_marc_653_line(raw_line)
        kws = parse_653_keywords(tag, max_keywords=settings.max_keywords_653)
        result["final_kw_count"] = len(kws)
        result["backup_used"] = result["final_kw_count"] > result["ai_kw_count"]
        result["keywords"] = kws
        result["tag_653"] = tag

        # 편차 플래그
        flags = []
        if result["toc_merged_len"] == 0:
            flags.append("목차없음")
        if result["desc_merged_len"] < 100:
            flags.append("설명빈약")
        if result["backup_used"]:
            flags.append("backup사용")
        if result["final_kw_count"] < settings.min_keywords_653:
            flags.append(f"키워드부족({result['final_kw_count']}개)")
        if settings.kpipa_enable and settings.kpipa_api_key and not result["kpipa_toc_hint_len"]:
            flags.append("KPIPA목차없음")
        result["flag"] = " | ".join(flags)

    except Exception as e:
        result["error"] = str(e)

    return result


# ── 출력 ─────────────────────────────────────────────────────────────────

def _bar(length: int, max_len: int = 1000, width: int = 10) -> str:
    filled = min(width, round(length / max_len * width))
    return "#" * filled + "." * (width - filled)


def print_report(results: list[dict], min_kw: int) -> None:
    sep = "=" * 110

    print(f"\n{sep}")
    print("  653 편차 진단 보고서")
    print(sep)
    print(
        f"  {'라벨':<30} {'분야':^8} {'목차':>5} {'설명':>5} {'풍부도':^12}"
        f" {'KPIPA목차':^10} {'AI→최종':^7} {'B':^3} {'주의플래그'}"
    )
    print("-" * 110)

    for r in results:
        if r["error"]:
            print(f"  {r['label']:<30}  ERROR: {r['error']}")
            continue

        bar = _bar(r["data_richness"])
        kpipa_toc = (
            f"{r['kpipa_toc_hint_len']}자"
            if r["kpipa_toc_hint_len"]
            else "-"
        )
        kw_flow = f"{r['ai_kw_count']}→{r['final_kw_count']}"
        backup_mark = "Y" if r["backup_used"] else "-"
        flag = r["flag"] or "OK"

        print(
            f"  {r['label']:<30} {r['cat_group']:^8}"
            f" {r['toc_merged_len']:>5} {r['desc_merged_len']:>5}"
            f" {bar} {r['data_richness']:>4}자"
            f"  {kpipa_toc:^10} {kw_flow:^7} {backup_mark:^3}  {flag}"
        )
        print(f"    제목: {r['title']}")
        print(f"    카테고리: {r['category']}")
        if r["keywords"]:
            print(f"    → {r['tag_653']}")
        print()

    # ── 패턴 요약 ────────────────────────────────────────────────────────
    ok = [r for r in results if not r["error"]]
    print(f"\n{sep}")
    print("  [편차 패턴 요약]")
    print(sep)

    no_toc = [r for r in ok if r["toc_merged_len"] == 0]
    short_desc = [r for r in ok if r["desc_merged_len"] < 100]
    no_kpipa_toc = [
        r for r in ok if r.get("kpipa_configured") and not r["kpipa_toc_hint_len"]
    ]
    backups = [r for r in ok if r["backup_used"]]
    low_kw = [r for r in ok if r["final_kw_count"] < min_kw]

    def _labels(lst: list[dict]) -> str:
        return ", ".join(r["label"].split(" [")[0] for r in lst) or "(없음)"

    print(f"  목차 없음           ({len(no_toc):>2}권): {_labels(no_toc)}")
    print(f"  설명 빈약(<100자)   ({len(short_desc):>2}권): {_labels(short_desc)}")
    print(f"  KPIPA 목차 힌트 없음 ({len(no_kpipa_toc):>2}권): {_labels(no_kpipa_toc)}")
    print(f"  backup 사용(AI부족) ({len(backups):>2}권): {_labels(backups)}")
    print(f"  키워드 {min_kw}개 미만      ({len(low_kw):>2}권): {_labels(low_kw)}")

    if ok:
        avg_richness = sum(r["data_richness"] for r in ok) / len(ok)
        avg_kw = sum(r["final_kw_count"] for r in ok) / len(ok)
        print(f"\n  평균 입력 풍부도: {avg_richness:.0f}자  |  평균 최종 키워드: {avg_kw:.1f}개")

    print(sep)
    print(
        "\n  [해석 가이드]\n"
        "  목차없음 + 설명빈약 → 입력 데이터 부족이 편차 원인일 가능성 높음\n"
        "  backup=Y           → AI 결과가 금지어/저가치 필터에 걸려 보충됨 → 프롬프트 조정 여지\n"
        "  KPIPA목차없음      → KPIPA에 해당 ISBN 목차가 없거나 비활성\n"
        "  키워드부족          → 최종 품질 저하 구간\n"
    )


# ── CSV 저장 ──────────────────────────────────────────────────────────────

CSV_COLUMNS = [
    "실행일시", "ISBN", "도서명", "제목(알라딘)",
    "분야그룹", "카테고리",
    "목차길이", "설명길이", "입력풍부도",
    "보강출처", "KPIPA_목차힌트_글자수", "KPIPA_목차병합",
    "AI키워드수", "최종키워드수", "backup사용",
    "키워드목록", "653필드", "주의플래그", "오류",
]


def save_csv(results: list[dict], out_dir: Path = Path(".")) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"653_진단_{ts}.csv"

    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: Excel 한글 호환
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        run_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in results:
            writer.writerow({
                "실행일시":     run_ts,
                "ISBN":         r["isbn"],
                "도서명":       r["label"],
                "제목(알라딘)": r.get("title", ""),
                "분야그룹":     r.get("cat_group", ""),
                "카테고리":     r.get("category", ""),
                "목차길이":     r.get("toc_merged_len", ""),
                "설명길이":     r.get("desc_merged_len", ""),
                "입력풍부도":   r.get("data_richness", ""),
                "보강출처":     r.get("hint_src", ""),
                "KPIPA_목차힌트_글자수": r.get("kpipa_toc_hint_len", ""),
                "KPIPA_목차병합": "Y" if r.get("kpipa_toc_added") else "-",
                "AI키워드수":   r.get("ai_kw_count", ""),
                "최종키워드수": r.get("final_kw_count", ""),
                "backup사용":   "Y" if r.get("backup_used") else "-",
                "키워드목록":   " / ".join(r.get("keywords", [])),
                "653필드":      r.get("tag_653", ""),
                "주의플래그":   r.get("flag", ""),
                "오류":         r.get("error", ""),
            })

    return path


# ── 진입점 ────────────────────────────────────────────────────────────────

async def main() -> None:
    settings = Settings(
        allow_insecure_ssl_fallback=True,
        insecure_ssl_fallback_hosts_csv="www.aladin.co.kr",
    )

    print(f"진단 시작: {len(TEST_BOOKS)}권 | 최소 키워드: {settings.min_keywords_653}개")
    print("(OpenAI API 호출이 포함되어 약 1~2분 소요됩니다)\n")

    # 로컬 진단 전용: 네트워크 SSL 인터셉션 환경에서 전체 우회
    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        tasks = [
            diagnose_isbn(isbn, label, settings, client)
            for isbn, label in TEST_BOOKS
        ]
        results = await asyncio.gather(*tasks)

    result_list = list(results)
    print_report(result_list, settings.min_keywords_653)

    csv_path = save_csv(result_list, out_dir=Path(__file__).parent)
    print(f"\nCSV 저장 완료: {csv_path.name}")


if __name__ == "__main__":
    asyncio.run(main())
