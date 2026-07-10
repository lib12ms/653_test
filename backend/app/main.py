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
from .sheets_service import diagnose_sheets, save_golden_data
from .fetcher import fetch_aladin_for_653, merge_aladin_with_nlk
from .nlk_client import fetch_kdc_content_code_by_isbn
from .models import (
    AladinMetadata653,
    Field653FromIsbnRequest,
    Field653FromMetadataRequest,
    Field653Response,
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
    preprocess_debug: dict[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> Field653Response:
    raw_line, err, token_usage, quality = await ai_service.generate_653_subfield_line(
        meta,
        max_keywords=max_kw,
        min_keywords=min_kw,
    )
    if err or not raw_line:
        return Field653Response(
            success=False,
            error=err or "653 생성 실패",
            token_usage=token_usage,
            aladin=meta,
            preprocess_debug=preprocess_debug,
        )
    tag = ai_service.build_marc_653_line(raw_line)
    kws = parse_653_keywords(tag, max_keywords=max_kw)
    fallback_kws = quality.fallback_keywords if quality else []
    return Field653Response(
        success=True,
        tag_653=tag,
        keywords=kws,
        raw_keyword_line=raw_line,
        token_usage=token_usage,
        aladin=meta,
        preprocess_debug=preprocess_debug,
        fallback_keywords=fallback_kws,
    )


@app.post("/api/field653", response_model=Field653Response)
async def field653_from_isbn(req: Field653FromIsbnRequest) -> Field653Response:
    """ISBN → 알라딘 → 653."""
    s = get_settings()
    http_client: httpx.AsyncClient = app.state.http_client
    cache: _TtlCache = app.state.isbn_cache
    conv_key = (s.kormarc_agent_conv_id or "").strip() or "instructions"
    cache_key = (
        f"v{str(s.field653_cache_bundle_version).strip() or '1'}|"
        f"{req.isbn.strip()}|{s.openai_model}|"
        f"{s.max_keywords_653}|{s.min_keywords_653}|"
        f"n{int(bool(s.nlk_enable and s.nlk_api_key))}|"
        f"agent:{conv_key}"
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    request_start = time.monotonic()
    try:
        (base_meta, preprocess_debug), content_code = await asyncio.gather(
            fetch_aladin_for_653(req.isbn, settings=s, include_debug=True, client=http_client),
            fetch_kdc_content_code_by_isbn(req.isbn, settings=s, client=http_client),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("알라딘 조회")
        raise HTTPException(status_code=502, detail=f"알라딘 API 오류: {e}") from e

    meta = merge_aladin_with_nlk(base_meta, settings=s, content_code=content_code)
    response = await _build_response_from_meta(
        meta,
        s.max_keywords_653,
        s.min_keywords_653,
        preprocess_debug=preprocess_debug,
        client=http_client,
    )
    response.duration_ms = round((time.monotonic() - request_start) * 1000, 1)
    if response.success:
        cache.set(cache_key, response)
    return response


@app.get("/api/sheets-check")
async def sheets_check() -> dict:
    """Google Sheets 연결 진단 (임시 — 확인 후 제거)."""
    return diagnose_sheets()


@app.post("/api/save-golden")
async def save_golden(data: dict) -> dict:
    """사서가 확정한 653 키워드를 Google Sheets에 저장합니다."""
    success, err_msg = save_golden_data(data)
    if success:
        return {"success": True}
    return {"success": False, "error": err_msg}


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
        client=http_client,
    )
