"""
I2M 653 필드 테스트 UI — 백엔드(FastAPI)에 ISBN 또는 직접 메타로 요청.
배포: `streamlit run streamlit_app/app.py` (루트에서)
"""
from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

DEFAULT_API = os.getenv("I2M_653_API_BASE", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_TIMEOUT = int(os.getenv("I2M_653_HTTP_TIMEOUT", "60"))


st.set_page_config(page_title="I2M — 653 테스트", page_icon="📚", layout="wide")
st.title("I2M 653 필드 (자유주제어) 테스트")
st.caption("데이터 수집(알라딘) → 전처리 → GPT 의미분석 → 키워드/ MRK 653 — 백엔드 API 연동")

def post_json(path: str, body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    url = f"{DEFAULT_API}{path}"
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            r = client.post(url, json=body)
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}: {r.text[:500]}"
        return r.json(), None
    except httpx.RequestError as e:
        return None, str(e)


def _render_editable_653(data: dict[str, Any], key_prefix: str) -> None:
    st.subheader("653 결과 편집")
    keywords = [str(x).strip() for x in (data.get("keywords") or []) if str(x).strip()]
    default_text = "\n".join(keywords)
    state_key = f"{key_prefix}_kw_edit"
    source_key = f"{key_prefix}_kw_source"
    current_source = str(
        data.get("raw_keyword_line")
        or data.get("tag_653")
        or default_text
    )

    # 첫 렌더이거나, 새 결과가 들어온 경우에만 편집창 초기값 갱신
    if state_key not in st.session_state or st.session_state.get(source_key) != current_source:
        st.session_state[state_key] = default_text
        st.session_state[source_key] = current_source

    edited_text = st.text_area(
        "키워드(한 줄에 하나씩, 직접 추가/수정 가능)",
        height=180,
        key=state_key,
    )
    edited_keywords = [line.strip() for line in edited_text.splitlines() if line.strip()]
    edited_tag = "=653  \\\\" + "".join(f"$a{kw.replace(' ', '')}" for kw in edited_keywords)

    st.markdown("**편집된 653(MRK)**")
    st.code(edited_tag, language="text")
    st.markdown("**편집된 키워드 목록**")
    st.write(edited_keywords)

    with st.expander("원본 API 응답 보기"):
        st.json(data)

    dbg = data.get("preprocess_debug") or {}
    if dbg:
        with st.expander("전처리 전/후 비교 보기"):
            st.markdown("**Category (raw -> clean)**")
            st.code(f"{dbg.get('category_raw','')}\n=>\n{dbg.get('category_clean','')}", language="text")
            st.markdown("**Description (raw -> clean)**")
            st.code(f"{dbg.get('description_raw','')}\n=>\n{dbg.get('description_clean','')}", language="text")
            st.markdown("**TOC (raw -> clean)**")
            st.code(f"{dbg.get('toc_raw','')}\n=>\n{dbg.get('toc_clean','')}", language="text")


isbn = st.text_input("ISBN", placeholder="9788936434267")
if st.button("653 생성 (ISBN)", type="primary", key="btn_isbn"):
    if not (isbn or "").strip():
        st.warning("ISBN을 입력하세요.")
    else:
        with st.spinner("알라딘 + GPT…"):
            data, err = post_json("/api/field653", {"isbn": isbn.strip()})
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

st.divider()
st.markdown(
    "**로컬 실행 예:** 터미널1 `cd backend` → `uvicorn app.main:app --reload`  "
    " / 터미널2 `streamlit run streamlit_app/app.py`  "
    " (프로젝트 루트 `653_test`에서)"
)
