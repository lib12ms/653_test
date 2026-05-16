"""
구(정밀 모드) vs 신(통합 모드) 프롬프트 비교 스크립트
======================================================
동일 ISBN·메타데이터로 두 프롬프트를 각각 실행해
키워드 수·토큰·backup 발동을 비교합니다.

실행:
    cd e:/653_test/backend
    python compare_modes.py
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

from app.ai_service import (
    _openai_chat_completions,
    _system_and_user_messages,
    build_marc_653_line,
    finalize_653,
    get_category_group,
    get_category_prompt,
    parse_keyword_line,
)
from app.config import Settings
from app.fetcher import fetch_aladin_for_653, fetch_secondary_metadata_hint, merge_aladin_with_nlk
from app.models import AladinMetadata653, parse_653_keywords
from app.preprocess import build_forbidden_set, clean_author_str, should_keep_keyword

# ── 비교 대상 ISBN (분야별 샘플) ────────────────────────────────────────────
TEST_BOOKS: list[tuple[str, str]] = [
    ("9791194322276", ""),
    ("9791167903594", ""),
    ("9791155819104", ""),
    ("9791192519883", ""),
    ("9791141603373", ""),
    ("9788960909878", ""),
    ("9788936481285", ""),
    ("9791189074906", ""),
    ("9788962627008", ""),
    ("9791199304901", ""),
    ("9791194084334", ""),
    ("9791194630753", ""),
]

DELAY_S = 4  # 호출 간 딜레이 (rate limit 대응)


# ── 구 정밀 모드 메시지 빌더 (5단계 CoT) ────────────────────────────────────

def _build_old_precise_messages(
    meta: AladinMetadata653,
    max_keywords: int,
) -> tuple[dict, dict]:
    """백업에서 복원한 구(정밀) 모드 프롬프트를 그대로 재현합니다."""
    category = meta.category
    title = meta.title
    authors = clean_author_str(meta.authors)
    description = meta.description
    toc = meta.toc

    parts = [p.strip() for p in (category or "").split(">") if p.strip()]
    cat_tail = " ".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "")

    forbidden = build_forbidden_set(title, authors)
    forbidden_list = ", ".join(sorted(forbidden)) or "(없음)"
    category_group = get_category_group(category)
    category_prompt = get_category_prompt(category)

    mode_prompt = (
        "정밀 모드: 아래 5단계를 내부적으로만 수행하고 최종 결과만 출력하세요.\n\n"
        "[1단계: 입력 분석] 분류 체인·꼬리, 제목, 저자, 설명, 목차를 종합해 핵심 주제 후보를 도출합니다. 정보 부족 시 분류 꼬리를 기반으로 삼습니다.\n\n"
        "[2단계: 필터링] 제외: 제목·저자 유래어(단, 주제 필수 시 구체 하위개념 치환으로 최대 1~2개 허용), 출판·유통 표현(베스트셀러·신간·단행본 등), 기능 약한 일반어(연구·개론·방법·이론 등), 한 글자·숫자·특수문자 토큰, 국가명+문학장르 복합어(한국문학·한국소설·한국시·한국에세이·일본소설·영미소설 등 — 이런 분류어는 독자 검색 주제어가 아님)\n\n"
        "[3단계: 분야 특화 치환] 추상·메타 표현(의의·현황·동향·배경·개요 등)은 실제 내용의 구체 하위개념으로 반드시 치환합니다. 카테고리별 지침 우선 적용, 인접 분야 확산 금지. 문학 분야에서 국가+장르 대신 구체 하위장르·주제를 사용하세요 (예: '한국소설' → '성장소설', '장편소설', '심리소설'; '한국시' → '현대시', '서정시', '시적언어').\n\n"
        "[4단계: 형식 최적화] 2~6글자 복합명사 우선, 붙여쓰기(공백 없음), 형용사+명사 자연어 문구 금지, 의미 중복은 대표어 1개로 정리.\n\n"
        f"[5단계: 최종 확정] 관련성·구체성·비중복성·균형 기준으로 최대 {max_keywords}개 선정.\n"
    )

    system_msg = {
        "role": "system",
        "content": (
            "당신은 KORMARC 작성 경험이 풍부한 도서관 메타데이터 전문가입니다.\n"
            "주어진 도서 정보를 바탕으로 MARC 653 자유주제어를 생성하세요.\n\n"
            f"{mode_prompt}\n"
            f"카테고리 그룹: {category_group}\n"
            f"[카테고리별 지침]\n{category_prompt}\n"
            "출력: `$a키워드1 $a키워드2 ...` 한 줄만, 사고 과정 없이 최종 결과만\n"
            "- 상위 분류명(건강·취미 등)은 구체 하위개념으로 치환\n"
            "- 유효 키워드 부족 시 분류 꼬리 기반 명사를 1~3개라도 출력\n\n"
            "출력 예시: '$a정서조절 $a성장소설' (쉼표·번호·설명문 금지)"
        ),
    }
    user_msg = {
        "role": "user",
        "content": (
            f"### 분석 대상 도서\n"
            f"- 분류(전체 체인): \"{category}\"\n"
            f"- 분류(핵심 꼬리): \"{cat_tail}\"\n"
            f"- 제목(245): \"{title}\"\n"
            f"- 저자(100/700): \"{authors}\"\n"
            f"- 설명: \"{description}\"\n"
            f"- 목차: \"{toc}\"\n"
            f"- 제외어 목록: {forbidden_list}\n\n"
            f"### 작업 지시\n"
            f"precise 모드와 카테고리별 지침을 적용해 653 주제어를 생성하세요.\n"
            f"- 목표 개수: 최소 5개, 최대 {max_keywords}개\n"
            f"- 결과: `$a키워드1 $a키워드2 ...` 한 줄"
        ),
    }
    return system_msg, user_msg


# ── 공통 후처리 ──────────────────────────────────────────────────────────────

def _postprocess(
    raw: str,
    meta: AladinMetadata653,
    max_keywords: int,
    min_keywords: int,
) -> dict:
    category = meta.category
    title = meta.title
    authors = clean_author_str(meta.authors)
    forbidden = build_forbidden_set(title, authors)
    raw_kws = parse_keyword_line(raw or "")
    ai_valid = [kw for kw in raw_kws if should_keep_keyword(kw, forbidden)]
    ai_output = "".join(f"$a{kw}" for kw in ai_valid)
    subfield = finalize_653(
        ai_output, forbidden,
        max_keywords=max_keywords, min_keywords=min_keywords,
        category=category, toc=meta.toc, description=meta.description,
    )
    tag = build_marc_653_line(subfield) if subfield else ""
    kws = parse_653_keywords(tag)
    return {
        "kw_count": len(kws),
        "ai_kw_count": len(ai_valid),
        "backup": len(kws) > len(ai_valid),
        "keywords": kws,
        "tag": tag,
        "error": "",
    }


# ── 프롬프트별 실행 ──────────────────────────────────────────────────────────

async def run_old_precise(
    meta: AladinMetadata653,
    settings: Settings,
    client: httpx.AsyncClient,
) -> dict:
    sys_m, user_m = _build_old_precise_messages(meta, settings.max_keywords_653)
    try:
        raw, usage = await _openai_chat_completions(
            settings.openai_api_key,
            settings.openai_base_url,
            settings.openai_model,
            [sys_m, user_m],
            settings=settings,
            client=client,
            temperature=0.2,
            max_tokens=220,
        )
    except Exception as e:
        return {"error": str(e), "kw_count": 0, "ai_kw_count": 0,
                "tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "backup": False, "keywords": [], "tag": ""}
    result = _postprocess(raw, meta, settings.max_keywords_653, settings.min_keywords_653)
    result["tokens"] = usage.total_tokens if usage else 0
    result["prompt_tokens"] = usage.prompt_tokens if usage else 0
    result["completion_tokens"] = usage.completion_tokens if usage else 0
    return result


async def run_new_unified(
    meta: AladinMetadata653,
    settings: Settings,
    client: httpx.AsyncClient,
) -> dict:
    sys_m, user_m = _system_and_user_messages(
        meta.category,
        meta.title,
        clean_author_str(meta.authors),
        meta.description,
        meta.toc,
        settings.max_keywords_653,
    )
    try:
        raw, usage = await _openai_chat_completions(
            settings.openai_api_key,
            settings.openai_base_url,
            settings.openai_model,
            [sys_m, user_m],
            settings=settings,
            client=client,
            temperature=0.2,
            max_tokens=200,
        )
    except Exception as e:
        return {"error": str(e), "kw_count": 0, "ai_kw_count": 0,
                "tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "backup": False, "keywords": [], "tag": ""}
    result = _postprocess(raw, meta, settings.max_keywords_653, settings.min_keywords_653)
    result["tokens"] = usage.total_tokens if usage else 0
    result["prompt_tokens"] = usage.prompt_tokens if usage else 0
    result["completion_tokens"] = usage.completion_tokens if usage else 0
    return result


# ── ISBN 하나 비교 ────────────────────────────────────────────────────────────

async def compare_isbn(
    isbn: str,
    label: str,
    idx: int,
    total: int,
    settings: Settings,
    client: httpx.AsyncClient,
) -> dict:
    row: dict = {
        "isbn": isbn, "label": label,
        "title": "", "category": "", "cat_group": "",
        "data_richness": 0,
        "old": {}, "new": {},
        "error": "",
    }
    try:
        base_meta, _ = await fetch_aladin_for_653(isbn, settings=settings, include_debug=True, client=client)
        row["title"] = base_meta.title
        if not row["label"]:
            row["label"] = base_meta.title[:28]
        row["category"] = base_meta.category
        row["cat_group"] = get_category_group(base_meta.category)

        nlk, hint_src = await fetch_secondary_metadata_hint(isbn, settings=settings, client=client)
        merge_src = "kpipa" if hint_src == "kpipa" else "none"
        meta = merge_aladin_with_nlk(base_meta, nlk, settings=settings, secondary_source=merge_src)
        row["data_richness"] = len(meta.toc) + len(meta.description)

        print(f"  [{idx:>2}/{total}] {label}")

        print(f"         [구] 정밀 5단계 CoT…", end="", flush=True)
        row["old"] = await run_old_precise(meta, settings, client)
        print(f" {row['old']['kw_count']}개 / {row['old']['tokens']}tok")

        await asyncio.sleep(DELAY_S)

        print(f"         [신] 통합 단일 프롬프트…", end="", flush=True)
        row["new"] = await run_new_unified(meta, settings, client)
        print(f" {row['new']['kw_count']}개 / {row['new']['tokens']}tok")

    except Exception as e:
        row["error"] = str(e)
        print(f"  [{idx:>2}/{total}] ERR {isbn}: {e}")

    return row


# ── 보고서 출력 ───────────────────────────────────────────────────────────────

def print_report(rows: list[dict]) -> None:
    sep = "=" * 115
    print(f"\n{sep}")
    print("  구(정밀 5단계 CoT) vs 신(통합 단일 프롬프트) 비교")
    print(sep)
    print(
        f"  {'라벨':<26} {'분야':^8}"
        f"  {'[구]키워드':^9} {'[구]토큰':^7} {'[구]B':^4}"
        f"  {'[신]키워드':^9} {'[신]토큰':^7} {'[신]B':^4}"
        f"  {'키워드차':^6} {'토큰절감':^7}"
    )
    print("-" * 115)

    ok = [r for r in rows if not r["error"]]
    for r in rows:
        if r["error"]:
            print(f"  {r['label']:<26}  ERROR: {r['error']}")
            continue
        o = r["old"]
        n = r["new"]
        kw_diff = n["kw_count"] - o["kw_count"]   # 신 - 구 (양수면 신이 더 많음)
        tok_save = o["tokens"] - n["tokens"]        # 구 - 신 (양수면 신이 절감)
        kw_diff_s = (f"+{kw_diff}" if kw_diff > 0 else str(kw_diff))
        tok_save_s = (f"-{tok_save}" if tok_save > 0 else f"+{abs(tok_save)}")
        o_b = "Y" if o.get("backup") else "-"
        n_b = "Y" if n.get("backup") else "-"
        print(
            f"  {r['label']:<26} {r['cat_group']:^8}"
            f"  {o['kw_count']:^9} {o['tokens']:^7} {o_b:^4}"
            f"  {n['kw_count']:^9} {n['tokens']:^7} {n_b:^4}"
            f"  {kw_diff_s:^6} {tok_save_s:^7}"
        )
        print(f"    [구] {' / '.join(o.get('keywords', []))}")
        print(f"    [신] {' / '.join(n.get('keywords', []))}")
        print()

    if not ok:
        return

    avg_o_kw  = sum(r["old"]["kw_count"] for r in ok) / len(ok)
    avg_n_kw  = sum(r["new"]["kw_count"] for r in ok) / len(ok)
    avg_o_tok = sum(r["old"]["tokens"]   for r in ok) / len(ok)
    avg_n_tok = sum(r["new"]["tokens"]   for r in ok) / len(ok)
    o_backups = sum(1 for r in ok if r["old"].get("backup"))
    n_backups = sum(1 for r in ok if r["new"].get("backup"))

    print(sep)
    print("  [요약]")
    print(f"  평균 키워드: [구] {avg_o_kw:.1f}개  /  [신] {avg_n_kw:.1f}개  (신-구 {avg_n_kw - avg_o_kw:+.1f})")
    print(f"  평균 토큰:   [구] {avg_o_tok:.0f}   /  [신] {avg_n_tok:.0f}   (절감 {avg_o_tok - avg_n_tok:.0f}tok)")
    print(f"  backup 발동: [구] {o_backups}건  /  [신] {n_backups}건")
    print(sep)


# ── CSV 저장 ──────────────────────────────────────────────────────────────────

CSV_COLUMNS = [
    "ISBN", "라벨", "제목", "분야그룹", "카테고리", "입력풍부도",
    "구_키워드수", "구_AI키워드수", "구_토큰", "구_프롬프트토큰", "구_완성토큰", "구_backup", "구_키워드목록", "구_오류",
    "신_키워드수", "신_AI키워드수", "신_토큰", "신_프롬프트토큰", "신_완성토큰", "신_backup", "신_키워드목록", "신_오류",
    "키워드수차이(신-구)", "토큰절감(구-신)",
]


def save_csv(rows: list[dict], out_dir: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"구신비교_{ts}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in rows:
            o = r.get("old") or {}
            n = r.get("new") or {}
            writer.writerow({
                "ISBN": r["isbn"],
                "라벨": r["label"],
                "제목": r.get("title", ""),
                "분야그룹": r.get("cat_group", ""),
                "카테고리": r.get("category", ""),
                "입력풍부도": r.get("data_richness", ""),
                "구_키워드수": o.get("kw_count", ""),
                "구_AI키워드수": o.get("ai_kw_count", ""),
                "구_토큰": o.get("tokens", ""),
                "구_프롬프트토큰": o.get("prompt_tokens", ""),
                "구_완성토큰": o.get("completion_tokens", ""),
                "구_backup": "Y" if o.get("backup") else "-",
                "구_키워드목록": " / ".join(o.get("keywords") or []),
                "구_오류": o.get("error", ""),
                "신_키워드수": n.get("kw_count", ""),
                "신_AI키워드수": n.get("ai_kw_count", ""),
                "신_토큰": n.get("tokens", ""),
                "신_프롬프트토큰": n.get("prompt_tokens", ""),
                "신_완성토큰": n.get("completion_tokens", ""),
                "신_backup": "Y" if n.get("backup") else "-",
                "신_키워드목록": " / ".join(n.get("keywords") or []),
                "신_오류": n.get("error", ""),
                "키워드수차이(신-구)": (n.get("kw_count") or 0) - (o.get("kw_count") or 0),
                "토큰절감(구-신)": (o.get("tokens") or 0) - (n.get("tokens") or 0),
            })
    return path


# ── 진입점 ───────────────────────────────────────────────────────────────────

async def main() -> None:
    settings = Settings(
        allow_insecure_ssl_fallback=True,
        insecure_ssl_fallback_hosts_csv="www.aladin.co.kr",
    )
    total = len(TEST_BOOKS)
    print(f"구/신 프롬프트 비교: {total}권 x 2회 = {total * 2}회 OpenAI 호출")
    print(f"호출 간 딜레이: {DELAY_S}초 | 예상 소요: 약 {total * DELAY_S * 2 // 60 + 2}분\n")

    rows: list[dict] = []
    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        for idx, (isbn, label) in enumerate(TEST_BOOKS, start=1):
            row = await compare_isbn(isbn, label, idx, total, settings, client)
            rows.append(row)
            if idx < total:
                await asyncio.sleep(DELAY_S)

    print_report(rows)
    csv_path = save_csv(rows, out_dir=Path(__file__).parent)
    print(f"\nCSV 저장: {csv_path.name}")


if __name__ == "__main__":
    asyncio.run(main())
