"""KPIPA getBookDetail — 앱 본선에서는 ONIX 목차(TextType 04)만 사용."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings, get_settings
from .fetcher_http import get_json, strip_html
from .models import NlkMetadataHint, normalize_isbn13
from .preprocess import clean_toc_for_ai

logger = logging.getLogger(__name__)


def extract_kpipa_book_payload(raw: dict[str, Any]) -> dict[str, Any] | None:
    """
    KPIPA getBookDetail ONIX JSON에서 Product 1건 dict 추출.
    스키마: response.body.items.Product (단일 dict 또는 배열).
    """
    if not isinstance(raw, dict):
        return None
    resp = raw.get("response")
    if not isinstance(resp, dict):
        return None
    body = resp.get("body")
    if not isinstance(body, dict):
        return None
    items = body.get("items")
    if not isinstance(items, dict):
        return None
    prod = items.get("Product")
    if isinstance(prod, list):
        for p in prod:
            if isinstance(p, dict):
                return p
        return None
    if isinstance(prod, dict):
        return prod
    return None


def kpipa_collateral_text(product: dict[str, Any], text_type: str | int) -> str:
    """ONIX CollateralDetail.TextContent에서 TextType별 본문(앱에서는 04=목차만 사용)."""
    cd = product.get("CollateralDetail")
    if not isinstance(cd, dict):
        return ""
    blocks = cd.get("TextContent")
    if not isinstance(blocks, list):
        return ""
    want = str(text_type)
    plain: list[str] = []
    fallback: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if str(b.get("TextType")) != want:
            continue
        aud = b.get("ContentAudience")
        aud0 = str(aud[0]) if isinstance(aud, list) and aud else ""
        texts = b.get("Text")
        if isinstance(texts, str):
            text_list = [texts]
        elif isinstance(texts, list):
            text_list = texts
        else:
            text_list = []
        merged = "\n".join(
            t.strip() for t in text_list if isinstance(t, str) and t.strip()
        )
        if not merged:
            continue
        cleaned = strip_html(merged) if "<" in merged else merged
        (plain if aud0 == "02" else fallback).append(cleaned)
    if plain:
        return max(plain, key=len)
    if fallback:
        return max(fallback, key=len)
    return ""


def parse_kpipa_toc_only(raw: dict[str, Any]) -> NlkMetadataHint:
    """KPIPA ONIX Product에서 목차(TextType 04)만 추출 → 힌트의 toc만 채움."""
    product = extract_kpipa_book_payload(raw)
    if not product:
        return NlkMetadataHint()
    toc_raw = kpipa_collateral_text(product, "04")
    return NlkMetadataHint(toc=clean_toc_for_ai(toc_raw))


async def fetch_kpipa_hint_by_isbn(
    isbn: str,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[NlkMetadataHint, dict[str, Any] | None]:
    """KPIPA getBookDetail — 응답 중 ONIX 목차(TextContent 04)만 사용. raw 응답도 함께 반환."""
    s = get_settings() if settings is None else settings
    isbn13 = normalize_isbn13(isbn)
    if not isbn13 or not s.kpipa_enable or not s.kpipa_api_key:
        return NlkMetadataHint(), None

    base = s.kpipa_api_base_url.rstrip("/")
    url = f"{base}/api/openApi/metaInfoSvc/getBookDetail"
    params: dict[str, Any] = {"apiKey": s.kpipa_api_key, "isbn": isbn13}
    req_client = client or httpx.AsyncClient()
    owns_client = client is None
    try:
        raw = await get_json(
            url,
            params,
            timeout=s.request_timeout_s,
            client=req_client,
            settings=s,
        )
        if not isinstance(raw, dict):
            return NlkMetadataHint(), None
        resp = raw.get("response")
        if isinstance(resp, dict):
            res = resp.get("result")
            if isinstance(res, dict):
                code = str(res.get("resultCode", "")).upper()
                if code and code != "INFO-000":
                    return NlkMetadataHint(), raw
        return parse_kpipa_toc_only(raw), raw
    except Exception as e:
        logger.warning("KPIPA getBookDetail 실패: %s", e)
        return NlkMetadataHint(), None
    finally:
        if owns_client:
            await req_client.aclose()


async def fetch_secondary_metadata_hint(
    isbn: str,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[NlkMetadataHint, str, dict[str, Any] | None]:
    """
    알라딘 외 보강: KPIPA에서 목차만 조회(앱 본선에서 NLK 미사용).
    반환: (힌트, 출처, kpipa_raw) — 출처: 'kpipa'(목차 있음) | 'none'.
    """
    s = get_settings() if settings is None else settings
    req = client or httpx.AsyncClient()
    owns = client is None
    try:
        if not (s.kpipa_enable and s.kpipa_api_key):
            return NlkMetadataHint(), "none", None
        hint, kpipa_raw = await fetch_kpipa_hint_by_isbn(isbn, settings=s, client=req)
        if (hint.toc or "").strip():
            return hint, "kpipa", kpipa_raw
        return NlkMetadataHint(), "none", kpipa_raw
    finally:
        if owns:
            await req.aclose()
