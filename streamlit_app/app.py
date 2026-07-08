"""
I2M 653 필드 UI — 단건(ISBN) 조회 + 배치 처리
"""
from __future__ import annotations

import csv
import io
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

_root = Path(__file__).resolve().parents[1]
_here = Path(__file__).resolve().parent
_backend = _root / "backend"
for _p in (_here, _backend):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

BACKEND_URL = st.secrets["BACKEND_URL"].rstrip("/")
DEFAULT_TIMEOUT = int(os.getenv("I2M_653_HTTP_TIMEOUT", "60"))
st.set_page_config(page_title="I2M — 653", page_icon="📚", layout="wide")
st.title("I2M 653 필드 (자유주제어)")


# ── 공통 유틸 ─────────────────────────────────────────────────────────────────

def post_json(path: str, body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    url = f"{BACKEND_URL}{path}"
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            r = client.post(url, json=body)
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}: {r.text[:500]}"
        return r.json(), None
    except httpx.RequestError as e:
        return None, str(e)


def _render_editable_653(data: dict[str, Any], key_prefix: str, isbn: str = "") -> None:
    st.subheader("653 결과")

    # ── 653 생성에 실제로 사용된 정보원 (API + 크롤링 통합 표시) ────────────
    dbg = data.get("preprocess_debug") or {}
    sources: list[str] = []

    if dbg.get("crawl_desc_filled") == "True":
        sources.append("크롤링 상세설명")
    elif (dbg.get("description_clean") or "").strip():
        sources.append("알라딘 API 상세설명")

    if (dbg.get("publisher_desc") or "").strip():
        if dbg.get("desc_merged_with_publisher") == "True":
            sources.append("크롤링 출판사 책소개(상세설명에 병합)")
        else:
            sources.append("크롤링 출판사 책소개")

    if dbg.get("crawl_toc_filled") == "True":
        sources.append("크롤링 목차")
    elif (dbg.get("toc_clean") or "").strip():
        sources.append("알라딘 API 목차")

    if data.get("hint_source") == "kpipa":
        sources.append("KPIPA 목차")

    # ── 정보원 / 소요시간 / 토큰 사용량 3단 표시 ─────────────────────────────
    col_src, col_time, col_tokens = st.columns(3)

    with col_src:
        st.caption("📚 정보원")
        st.markdown(", ".join(sources) if sources else "_정보 없음_")

    with col_time:
        st.caption("⏱ 소요시간")
        duration_ms = data.get("duration_ms")
        if duration_ms is not None:
            st.markdown(f"{duration_ms / 1000:.1f}초")
        else:
            st.markdown("_—_")

    with col_tokens:
        st.caption("🔢 토큰 사용량")
        usage = data.get("token_usage") or {}
        total_tokens = usage.get("total_tokens")
        if total_tokens:
            st.markdown(f"총 {total_tokens:,}")
            breakdown = usage.get("breakdown") or {}
            for label, count in breakdown.items():
                st.caption(f"· {label}: {count:,}")
            completion_tokens = usage.get("completion_tokens")
            if completion_tokens:
                st.caption(f"· 생성 결과: {completion_tokens:,}")
        else:
            st.markdown("_—_")

    if (
        dbg.get("crawl_used") == "True"
        and dbg.get("crawl_toc_filled") != "True"
        and not (dbg.get("toc_clean") or "").strip()
    ):
        if dbg.get("playwright_used") != "True":
            st.warning("알라딘 목차 크롤링: Playwright 미설치 — `pip install playwright && playwright install chromium`")
        else:
            st.warning("알라딘 상세페이지 크롤링 시도했으나 목차를 찾지 못했습니다.")

    # ── 키워드 편집 ──────────────────────────────────────────────────────────
    keywords = [str(x).strip() for x in (data.get("keywords") or []) if str(x).strip()]
    default_text = "\n".join(keywords)
    state_key = f"{key_prefix}_kw_edit"
    source_key = f"{key_prefix}_kw_source"
    current_source = str(
        data.get("raw_keyword_line") or data.get("tag_653") or default_text
    )

    if state_key not in st.session_state or st.session_state.get(source_key) != current_source:
        st.session_state[state_key] = default_text
        st.session_state[source_key] = current_source

    st.markdown("#### 키워드 편집")
    edited_text = st.text_area(
        "키워드",
        height=180,
        key=state_key,
        label_visibility="collapsed",
        placeholder="키워드를 한 줄에 하나씩 입력하세요",
        help="한 줄에 키워드 하나씩. 수정·삭제·추가 후 Ctrl+Enter로 반영됩니다.",
    )
    edited_keywords = [line.strip() for line in edited_text.splitlines() if line.strip()]
    edited_tag = "=653  \\\\" + "".join(f"$a{kw.replace(' ', '')}" for kw in edited_keywords)

    st.caption(f"{len(edited_keywords)}개 키워드")
    st.markdown("###### 653 (MRK 포맷)")
    st.code(edited_tag, language="text")

    token_usage = data.get("token_usage") or {}
    if token_usage:
        prompt_t = token_usage.get("prompt_tokens", 0)
        completion_t = token_usage.get("completion_tokens", 0)
        total_t = token_usage.get("total_tokens", 0)
        st.caption(
            f"토큰 사용량 — 프롬프트: {prompt_t:,} / 완성: {completion_t:,} / 합계: {total_t:,}"
        )

    with st.expander("원본 API 응답"):
        st.json(data)

    st.divider()
    if st.button("골든 데이터로 확정 저장", type="primary", key=f"{key_prefix}_save_golden"):
        aladin = data.get("aladin") or {}
        save_data = {
            "isbn": isbn,
            "title": aladin.get("title", ""),
            "authors": aladin.get("authors", ""),
            "category": aladin.get("category", ""),
            "category_group": "",
            "gpt_result": data.get("tag_653", ""),
            "golden_result": edited_tag,
            "is_modified": edited_tag != data.get("tag_653", ""),
            "mode": "단건조회",
        }
        save_result, err = post_json("/api/save-golden", save_data)
        if err or not (save_result or {}).get("success"):
            st.error(f"저장 실패: {err or save_result}")
        else:
            st.success("골든 데이터셋에 저장되었습니다!")

    if dbg:
        with st.expander("전처리 전/후 비교"):
            if dbg.get("crawl_used") == "True":
                crawled_labels = []
                if dbg.get("crawl_desc_filled") == "True":
                    crawled_labels.append("설명(크롤링)")
                if dbg.get("crawl_toc_filled") == "True":
                    crawled_labels.append("목차(크롤링)")
                if crawled_labels:
                    st.caption(f"크롤링으로 채워진 필드: {', '.join(crawled_labels)}")
            st.markdown("**Category (raw → clean)**")
            st.code(f"{dbg.get('category_raw','')}\n→\n{dbg.get('category_clean','')}", language="text")
            st.markdown("**Description (raw → clean)**")
            st.code(f"{dbg.get('description_raw','')}\n→\n{dbg.get('description_clean','')}", language="text")
            st.markdown("**TOC (raw → clean)**")
            st.code(f"{dbg.get('toc_raw','')}\n→\n{dbg.get('toc_clean','')}", language="text")


def _make_csv_bytes(rows: list[dict]) -> bytes:
    columns = ["순번", "ISBN", "제목", "카테고리", "653필드", "키워드목록", "오류"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


# ── 탭 ───────────────────────────────────────────────────────────────────────
tab_single, tab_batch = st.tabs(["단건 조회", "배치 처리"])

# ── 탭 1: 단건 ───────────────────────────────────────────────────────────────
with tab_single:
    isbn = st.text_input("ISBN", placeholder="9788936434267")
    if st.button("653 생성", type="primary", key="btn_isbn"):
        if not (isbn or "").strip():
            st.warning("ISBN을 입력하세요.")
        else:
            with st.spinner("알라딘 수집 → KPIPA 목차(선택) → GPT 분석…"):
                data, err = post_json(
                    "/api/field653",
                    {"isbn": isbn.strip()},
                )
            if err:
                st.error(err)
            elif data:
                st.session_state["isbn_result_data"] = data
                st.session_state["isbn_queried"] = isbn.strip()

    result_data = st.session_state.get("isbn_result_data")
    if result_data:
        if result_data.get("success") and result_data.get("tag_653"):
            queried_isbn = st.session_state.get("isbn_queried", isbn.strip())
            _render_editable_653(result_data, "isbn", isbn=queried_isbn)
        if result_data.get("error"):
            st.warning(result_data["error"])

# ── 탭 2: 배치 ───────────────────────────────────────────────────────────────
with tab_batch:
    st.markdown("ISBN을 한 줄에 하나씩 입력하세요. 처리 후 CSV를 다운로드할 수 있습니다.")

    isbn_text = st.text_area(
        "ISBN 목록",
        height=200,
        placeholder="9788936434267\n9791168224506\n9791175910676",
    )

    if st.button("배치 653 생성", type="primary", key="btn_batch"):
        isbn_list = [line.strip() for line in isbn_text.splitlines() if line.strip()]
        if not isbn_list:
            st.warning("ISBN을 입력하세요.")
        else:
            results: list[dict] = []
            progress = st.progress(0, text="처리 중…")
            status_area = st.empty()

            for i, isbn_item in enumerate(isbn_list):
                status_area.text(f"[{i + 1}/{len(isbn_list)}] {isbn_item} 처리 중…")
                data, err = post_json(
                    "/api/field653",
                    {"isbn": isbn_item},
                )
                aladin = (data or {}).get("aladin") or {}
                results.append({
                    "순번": i + 1,
                    "ISBN": isbn_item,
                    "제목": aladin.get("title", ""),
                    "카테고리": aladin.get("category", ""),
                    "653필드": (data or {}).get("tag_653", ""),
                    "키워드목록": " / ".join((data or {}).get("keywords") or []),
                    "오류": err or ((data or {}).get("error") or ""),
                })
                progress.progress((i + 1) / len(isbn_list), text=f"{i + 1}/{len(isbn_list)} 완료")

            status_area.empty()
            st.session_state["batch_results"] = results

    batch_results = st.session_state.get("batch_results")
    if batch_results:
        ok = sum(1 for r in batch_results if not r["오류"])
        st.success(f"완료: {ok}/{len(batch_results)}권 성공")
        st.dataframe(batch_results, use_container_width=True)
        st.download_button(
            "CSV 다운로드",
            data=_make_csv_bytes(batch_results),
            file_name="653_배치결과.csv",
            mime="text/csv",
        )
