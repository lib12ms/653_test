"""FastAPI 엔트리: 653 필드 메타 수집 → GPT → MRK."""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import ai_service
from .config import get_settings
from .fetcher import fetch_aladin_for_653, fetch_nlk_hint_by_isbn, merge_aladin_with_nlk
from .models import (
    AladinMetadata653,
    Field653FromIsbnRequest,
    Field653FromMetadataRequest,
    Field653Response,
    NlkMetadataHint,
    parse_653_keywords,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="I2M 653", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _build_response_from_meta(
    meta: AladinMetadata653,
    max_kw: int,
    min_kw: int,
    nlk_hint: NlkMetadataHint | None = None,
    preprocess_debug: dict[str, str] | None = None,
) -> Field653Response:
    raw_line, err = ai_service.generate_653_subfield_line(
        meta,
        max_keywords=max_kw,
        min_keywords=min_kw,
    )
    if err or not raw_line:
        return Field653Response(
            success=False,
            error=err or "653 생성 실패",
            aladin=meta,
            nlk_hint=nlk_hint,
            preprocess_debug=preprocess_debug,
        )
    tag = ai_service.build_marc_653_line(raw_line)
    kws = parse_653_keywords(tag)
    return Field653Response(
        success=True,
        tag_653=tag,
        keywords=kws,
        raw_keyword_line=raw_line,
        aladin=meta,
        nlk_hint=nlk_hint,
        preprocess_debug=preprocess_debug,
    )


@app.post("/api/field653", response_model=Field653Response)
def field653_from_isbn(req: Field653FromIsbnRequest) -> Field653Response:
    """ISBN → 알라딘(+NLK 보강) 메타 수집 → 653."""
    s = get_settings()
    try:
        base_meta, preprocess_debug = fetch_aladin_for_653(
            req.isbn,
            settings=s,
            include_debug=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("알라딘 조회")
        raise HTTPException(status_code=502, detail=f"알라딘 API 오류: {e}") from e

    nlk_hint = fetch_nlk_hint_by_isbn(req.isbn, settings=s)
    meta = merge_aladin_with_nlk(base_meta, nlk_hint, settings=s)
    return _build_response_from_meta(
        meta,
        s.max_keywords_653,
        s.min_keywords_653,
        nlk_hint=nlk_hint,
        preprocess_debug=preprocess_debug,
    )


@app.post("/api/field653/preview", response_model=Field653Response)
def field653_from_metadata(req: Field653FromMetadataRequest) -> Field653Response:
    """메타데이터만으로 653(팀 점검·프롬프트 테스트용, 알라딘 없음)."""
    s = get_settings()
    meta = AladinMetadata653(
        category=req.category,
        title=req.title,
        authors=req.authors,
        description=req.description,
        toc=req.toc,
    )
    return _build_response_from_meta(
        meta,
        req.max_keywords,
        min(s.min_keywords_653, req.max_keywords),
    )
