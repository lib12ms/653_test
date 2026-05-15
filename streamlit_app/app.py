"""
I2M 653 필드 UI — 단건(ISBN) 조회 + 배치 처리
"""
from __future__ import annotations

import csv
import io
import os
from typing import Any

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

BACKEND_URL = st.secrets["BACKEND_URL"].rstrip("/")
DEFAULT_TIMEOUT = int(os.getenv("I2M_653_HTTP_TIMEOUT", "60"))
ANALYSIS_MODE_OPTIONS = {
    "fast": "⚡ 빠른 모드",
    "precise": "🎯 정밀 모드",
}

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


def _render_nlk_info(data: dict[str, Any]) -> None:
    nlk = data.get("nlk_hint") or {}
    class_no = nlk.get("class_no", "")
    kwd = nlk.get("kwd", "")
    subjects = nlk.get("subjects") or []
    if not (class_no or kwd or subjects):
        return
    parts = []
    if class_no:
        parts.append(f"KDC **{class_no}**")
    if kwd:
        parts.append(f"키워드: {kwd}")
    if subjects:
        parts.append(f"주제어: {', '.join(subjects[:5])}")
    st.caption("NLK | " + " · ".join(parts))


def _render_editable_653(data: dict[str, Any], key_prefix: str) -> None:
    st.subheader("653 결과")
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

    edited_text = st.text_area(
        "키워드 (한 줄에 하나씩, 직접 수정 가능)",
        height=180,
        key=state_key,
    )
    edited_keywords = [line.strip() for line in edited_text.splitlines() if line.strip()]
    edited_tag = "=653  \\\\" + "".join(f"$a{kw.replace(' ', '')}" for kw in edited_keywords)

    st.markdown("**653 (MRK)**")
    st.code(edited_tag, language="text")

    _render_nlk_info(data)

    with st.expander("원본 API 응답"):
        st.json(data)

    dbg = data.get("preprocess_debug") or {}
    if dbg:
        with st.expander("전처리 전/후 비교"):
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
    analysis_mode = st.radio(
        "분석 모드",
        options=list(ANALYSIS_MODE_OPTIONS.keys()),
        format_func=lambda m: ANALYSIS_MODE_OPTIONS[m],
        horizontal=True,
    )
    if analysis_mode == "fast":
        st.caption("⚡ 빠른 모드: 핵심 규칙 중심으로 빠르게 생성합니다.")
    else:
        st.caption("🎯 정밀 모드: 5단계 CoT 분석 적용. 품질 우선, 응답 시간이 다소 길 수 있습니다.")

    isbn = st.text_input("ISBN", placeholder="9788936434267")
    if st.button("653 생성", type="primary", key="btn_isbn"):
        if not (isbn or "").strip():
            st.warning("ISBN을 입력하세요.")
        else:
            with st.spinner("알라딘·NLK 수집 → GPT 분석…"):
                data, err = post_json(
                    "/api/field653",
                    {"isbn": isbn.strip(), "analysis_mode": analysis_mode},
                )
            if err:
                st.error(err)
            elif data:
                st.session_state["isbn_result_data"] = data

    result_data = st.session_state.get("isbn_result_data")
    if result_data:
        if result_data.get("success") and result_data.get("tag_653"):
            _render_editable_653(result_data, "isbn")
        if result_data.get("error"):
            st.warning(result_data["error"])

# ── 탭 2: 배치 ───────────────────────────────────────────────────────────────
with tab_batch:
    st.markdown("ISBN을 한 줄에 하나씩 입력하세요. 처리 후 CSV를 다운로드할 수 있습니다.")

    batch_mode = st.radio(
        "분석 모드",
        options=list(ANALYSIS_MODE_OPTIONS.keys()),
        format_func=lambda m: ANALYSIS_MODE_OPTIONS[m],
        horizontal=True,
        key="batch_mode",
    )

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
                    {"isbn": isbn_item, "analysis_mode": batch_mode},
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


st.divider()
st.markdown(
    "**실행:** `.streamlit/secrets.toml` 에 `BACKEND_URL` 설정 "
    "(로컬: `http://127.0.0.1:8000` / 배포: `https://six53-test.onrender.com`). "
    "백엔드 실행: `cd backend && uvicorn app.main:app --reload`"
)
