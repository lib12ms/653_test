"""FastAPI 엔트리: 653 필드 메타 수집 → GPT → MRK."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import ai_service
from .config import get_settings
from .fetcher import fetch_aladin_for_653, fetch_nlk_hint_by_isbn, merge_aladin_with_nlk
from .models import (
    AnalysisMode,
    AladinMetadata653,
    Field653FromIsbnRequest,
    Field653FromMetadataRequest,
    Field653Response,
    NlkMetadataHint,
    parse_653_keywords,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class _TtlCache:
    def __init__(self, ttl_s: int, max_entries: int) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._store: OrderedDict[str, tuple[float, Field653Response]] = OrderedDict()

    def get(self, key: str) -> Field653Response | None:
        if self.ttl_s <= 0:
            return None
        now = time.monotonic()
        hit = self._store.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if expires_at < now:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return value.model_copy(deep=True)

    def set(self, key: str, value: Field653Response) -> None:
        if self.ttl_s <= 0:
            return
        expires_at = time.monotonic() + self.ttl_s
        self._store[key] = (expires_at, value.model_copy(deep=True))
        self._store.move_to_end(key)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.http_client = httpx.AsyncClient(timeout=settings.request_timeout_s)
    app.state.isbn_cache = _TtlCache(
        ttl_s=settings.isbn_cache_ttl_s,
        max_entries=settings.isbn_cache_max_entries,
    )
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(title="I2M 653", version="0.1.0", lifespan=lifespan)
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


async def _build_response_from_meta(
    meta: AladinMetadata653,
    max_kw: int,
    min_kw: int,
    analysis_mode: AnalysisMode,
    nlk_hint: NlkMetadataHint | None = None,
    preprocess_debug: dict[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> Field653Response:
    raw_line, err, token_usage = await ai_service.generate_653_subfield_line(
        meta,
        max_keywords=max_kw,
        min_keywords=min_kw,
        analysis_mode=analysis_mode,
        client=client,
    )
    if err or not raw_line:
        return Field653Response(
            success=False,
            analysis_mode=analysis_mode,
            error=err or "653 생성 실패",
            token_usage=token_usage,
            aladin=meta,
            nlk_hint=nlk_hint,
            preprocess_debug=preprocess_debug,
        )
    tag = ai_service.build_marc_653_line(raw_line)
    kws = parse_653_keywords(tag)
    return Field653Response(
        success=True,
        analysis_mode=analysis_mode,
        tag_653=tag,
        keywords=kws,
        raw_keyword_line=raw_line,
        token_usage=token_usage,
        aladin=meta,
        nlk_hint=nlk_hint,
        preprocess_debug=preprocess_debug,
    )


@app.post("/api/field653", response_model=Field653Response)
async def field653_from_isbn(req: Field653FromIsbnRequest) -> Field653Response:
    """ISBN → 알라딘(+NLK 보강) 메타 수집 → 653."""
    s = get_settings()
    http_client: httpx.AsyncClient = app.state.http_client
    cache: _TtlCache = app.state.isbn_cache
    cache_key = (
        f"{req.isbn.strip()}|{req.analysis_mode}|{s.openai_model}|"
        f"{s.max_keywords_653}|{s.min_keywords_653}"
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        base_meta_task = fetch_aladin_for_653(
            req.isbn, settings=s, include_debug=True, client=http_client
        )
        nlk_task = fetch_nlk_hint_by_isbn(req.isbn, settings=s, client=http_client)
        (base_meta, preprocess_debug), nlk_hint = await asyncio.gather(base_meta_task, nlk_task)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("알라딘 조회")
        raise HTTPException(status_code=502, detail=f"알라딘 API 오류: {e}") from e

    meta = merge_aladin_with_nlk(base_meta, nlk_hint, settings=s)
    response = await _build_response_from_meta(
        meta,
        s.max_keywords_653,
        s.min_keywords_653,
        req.analysis_mode,
        nlk_hint=nlk_hint,
        preprocess_debug=preprocess_debug,
        client=http_client,
    )
    if response.success:
        cache.set(cache_key, response)
    return response


@app.post("/api/field653/preview", response_model=Field653Response)
async def field653_from_metadata(req: Field653FromMetadataRequest) -> Field653Response:
    """메타데이터만으로 653(팀 점검·프롬프트 테스트용, 알라딘 없음)."""
    s = get_settings()
    http_client: httpx.AsyncClient = app.state.http_client
    meta = AladinMetadata653(
        category=req.category,
        title=req.title,
        authors=req.authors,
        description=req.description,
        toc=req.toc,
    )
    return await _build_response_from_meta(
        meta,
        req.max_keywords,
        min(s.min_keywords_653, req.max_keywords),
        req.analysis_mode,
        client=http_client,
    )
