"""
I2M 653 필드 UI — 단건(ISBN) 조회 + 배치 처리 + 품질 평가
"""
from __future__ import annotations

import csv
import io
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

_root = Path(__file__).resolve().parents[1]
_here = Path(__file__).resolve().parent
_backend = _root / "backend"
for _p in (_here, _backend):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quality_rubric import EVAL_COLUMNS, RUBRIC_GUIDE_KO, empty_eval_fields

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

    # ── 메타 정보 배지 (크롤링 보완 / KPIPA 목차 병합) ──────────────────────
    dbg = data.get("preprocess_debug") or {}
    if dbg.get("crawl_used") == "True":
        crawled_fields = []
        if dbg.get("crawl_desc_filled") == "True":
            crawled_fields.append("설명")
        if dbg.get("crawl_toc_filled") == "True":
            crawled_fields.append("목차")
        if crawled_fields:
            st.info(f"알라딘 상세페이지 크롤링으로 **{', '.join(crawled_fields)}** 보완됨")
        else:
            st.warning("알라딘 상세페이지 크롤링 시도했으나 내용을 찾지 못했습니다.")

    if data.get("hint_source") == "kpipa":
        st.info("KPIPA 목차가 병합되었습니다.")

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

    edited_text = st.text_area(
        "키워드 (한 줄에 하나씩, 직접 수정 가능)",
        height=180,
        key=state_key,
    )
    edited_keywords = [line.strip() for line in edited_text.splitlines() if line.strip()]
    edited_tag = "=653  \\\\" + "".join(f"$a{kw.replace(' ', '')}" for kw in edited_keywords)

    st.markdown("**653 (MRK)**")
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


_EVAL_MACHINE_COLS = [
    "ISBN",
    "제목",
    "카테고리",
    "KPIPA_목차_글자수",
    "키워드목록",
    "653필드",
    "오류",
]


def _make_eval_labeling_csv_bytes(df: pd.DataFrame) -> bytes:
    cols = _EVAL_MACHINE_COLS + list(EVAL_COLUMNS)
    buf = io.BytesIO()
    df[cols].to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue()


def _build_eval_column_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for c in _EVAL_MACHINE_COLS:
        cfg[c] = st.column_config.TextColumn(c, disabled=True)
    cfg["평가_종합"] = st.column_config.SelectboxColumn(
        "평가_종합",
        options=["", "양호", "보통", "불량"],
        help="사서 종합 판정",
    )
    score_opts = ["", "1", "2", "3", "4", "5"]
    cfg["평가_검색효용_1to5"] = st.column_config.SelectboxColumn(
        "검색효용(1~5)",
        options=score_opts,
        help="1=낮음, 5=높음",
    )
    cfg["평가_주제부합_1to5"] = st.column_config.SelectboxColumn(
        "주제부합(1~5)",
        options=score_opts,
        help="1=낮음, 5=높음",
    )
    for c in ("평가_불량태그", "평가_메모", "평가자", "평가일"):
        cfg[c] = st.column_config.TextColumn(c)
    return cfg


# ── 탭 ───────────────────────────────────────────────────────────────────────
tab_single, tab_batch, tab_eval = st.tabs(["단건 조회", "배치 처리", "품질 평가"])

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

# ── 탭 3: 품질 평가 (사서 라벨링) ───────────────────────────────────────────
with tab_eval:
    st.markdown(
        "백엔드 API로 653을 생성한 뒤, 표에서 **평가_** 열만 채우고 CSV로 내려받습니다. "
        "오프라인으로 `python 0516test/summarize_eval_csv.py <파일.csv>` 로 집계할 수 있습니다."
    )
    with st.expander("평가 기준 안내", expanded=False):
        st.markdown(RUBRIC_GUIDE_KO)

    isbn_eval = st.text_area(
        "ISBN 목록 (한 줄에 하나)",
        height=160,
        placeholder="9788936433598\n9788954641326",
        key="eval_isbn_area",
    )

    if st.button("653 불러와 평가 시트 만들기", type="primary", key="btn_eval_load"):
        lines = [ln.strip() for ln in isbn_eval.splitlines() if ln.strip()]
        if not lines:
            st.warning("ISBN을 입력하세요.")
        else:
            eval_rows: list[dict[str, Any]] = []
            prog = st.progress(0, text="653 생성 중…")
            for i, isbn_item in enumerate(lines):
                data, err = post_json("/api/field653", {"isbn": isbn_item})
                aladin = (data or {}).get("aladin") or {}
                kpipa_hint = (data or {}).get("nlk_hint") or {}
                toc_s = (kpipa_hint.get("toc") or "").strip()
                base = {
                    "ISBN": isbn_item,
                    "제목": aladin.get("title", ""),
                    "카테고리": aladin.get("category", ""),
                    "KPIPA_목차_글자수": len(toc_s) if toc_s else "",
                    "키워드목록": " / ".join((data or {}).get("keywords") or []),
                    "653필드": (data or {}).get("tag_653", ""),
                    "오류": err or ((data or {}).get("error") or ""),
                }
                base.update(empty_eval_fields())
                eval_rows.append(base)
                prog.progress((i + 1) / len(lines), text=f"{i + 1}/{len(lines)} 완료")
            st.session_state["eval_label_df"] = pd.DataFrame(eval_rows)

    eval_df = st.session_state.get("eval_label_df")
    if eval_df is not None and not eval_df.empty:
        st.subheader("라벨링 표")
        edited = st.data_editor(
            eval_df,
            column_config=_build_eval_column_config(),
            hide_index=True,
            use_container_width=True,
            num_rows="fixed",
            key="eval_data_editor",
        )
        st.session_state["eval_label_df"] = edited
        st.download_button(
            "평가 시트 CSV 다운로드",
            data=_make_eval_labeling_csv_bytes(edited),
            file_name="653_평가시트.csv",
            mime="text/csv",
            key="dl_eval_csv",
        )

st.divider()
st.markdown(
    "**실행:** `.streamlit/secrets.toml` 에 `BACKEND_URL` 설정 "
    "(로컬: `http://127.0.0.1:8000` / 배포: `https://six53-test.onrender.com`). "
    "백엔드: `cd backend && uvicorn app.main:app --reload`. "
    "**품질 평가(오프라인):** `python 0516test/export_eval_sheet.py` 로 시트 생성 후 사서가 채우고, "
    "`python 0516test/summarize_eval_csv.py 0516test/eval_sheet_….csv` 로 요약. 기준·컬럼 정의는 `0516test/quality_rubric.py`."
)
