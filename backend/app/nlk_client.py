"""국립중앙도서관 OpenAPI(앱 본선 미사용, probe·진단 스크립트용)."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings, get_settings
from .fetcher_http import get_json, get_text, safe_fetch_page_text
from .models import NlkMetadataHint, normalize_isbn13
from .nlk_metadata import (
    hint_from_seoji_doc,
    nlk_hint_nonempty,
    parse_nlk_json,
    parse_nlk_xml,
)
from .preprocess import clean_description_for_ai, clean_toc_for_ai

logger = logging.getLogger(__name__)


async def _fetch_nlk_seoji_hint(
    isbn13: str,
    s: Settings,
    client: httpx.AsyncClient,
) -> NlkMetadataHint:
    params: dict[str, Any] = {
        "cert_key": s.nlk_api_key,
        "result_style": "json",
        "page_no": 1,
        "page_size": 3,
        "isbn": isbn13,
    }
    raw = await get_json(
        s.nlk_seoji_api_url,
        params,
        timeout=s.request_timeout_s,
        client=client,
        settings=s,
    )
    docs = raw.get("docs")
    if not isinstance(docs, list) or not docs or not isinstance(docs[0], dict):
        return NlkMetadataHint()
    hint = hint_from_seoji_doc(docs[0])
    if not hint.toc and hint.book_tb_cnt_url:
        hint.toc = clean_toc_for_ai(
            await safe_fetch_page_text(
                hint.book_tb_cnt_url,
                timeout=s.request_timeout_s,
                client=client,
                settings=s,
            )
        )
    else:
        hint.toc = clean_toc_for_ai(hint.toc)
    if not hint.description and hint.book_intro_url:
        hint.description = await safe_fetch_page_text(
            hint.book_intro_url,
            timeout=s.request_timeout_s,
            client=client,
            settings=s,
        )
    hint.description = clean_description_for_ai(hint.description)
    return hint


async def fetch_nlk_hint_by_isbn(
    isbn: str,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> NlkMetadataHint:
    s = get_settings() if settings is None else settings
    isbn13 = normalize_isbn13(isbn)
    if not isbn13 or not s.nlk_enable or not s.nlk_api_key:
        return NlkMetadataHint()

    params: dict[str, Any] = {
        "key": s.nlk_api_key,
        "kwd": isbn13,
        "pageNum": 1,
        "pageSize": 1,
    }
    req_client = client or httpx.AsyncClient()
    owns_client = client is None
    try:
        raw_json: dict[str, Any] | None = None
        try:
            raw_json = await get_json(
                s.nlk_api_url,
                {**params, "apiType": "json"},
                timeout=s.request_timeout_s,
                client=req_client,
                settings=s,
            )
            parsed = parse_nlk_json(raw_json)
            if not parsed.toc and parsed.book_tb_cnt_url:
                parsed.toc = clean_toc_for_ai(
                    await safe_fetch_page_text(
                        parsed.book_tb_cnt_url,
                        timeout=s.request_timeout_s,
                        client=req_client,
                        settings=s,
                    )
                )
            else:
                parsed.toc = clean_toc_for_ai(parsed.toc)
            if not parsed.description and parsed.book_intro_url:
                parsed.description = await safe_fetch_page_text(
                    parsed.book_intro_url,
                    timeout=s.request_timeout_s,
                    client=req_client,
                    settings=s,
                )
            parsed.description = clean_description_for_ai(parsed.description)
            if nlk_hint_nonempty(parsed):
                return parsed
        except Exception:
            logger.info("NLK search.do JSON 실패, XML로 재시도")

        skip_xml = False
        if isinstance(raw_json, dict):
            t0 = raw_json.get("total")
            if t0 is not None:
                try:
                    skip_xml = int(str(t0).strip()) == 0
                except ValueError:
                    pass
        if skip_xml:
            return await _fetch_nlk_seoji_hint(isbn13, s, req_client)

        try:
            raw_xml = await get_text(
                s.nlk_api_url,
                {**params, "apiType": "xml"},
                timeout=s.request_timeout_s,
                client=req_client,
                settings=s,
            )
            parsed = parse_nlk_xml(raw_xml)
            if not parsed.toc and parsed.book_tb_cnt_url:
                parsed.toc = clean_toc_for_ai(
                    await safe_fetch_page_text(
                        parsed.book_tb_cnt_url,
                        timeout=s.request_timeout_s,
                        client=req_client,
                        settings=s,
                    )
                )
            else:
                parsed.toc = clean_toc_for_ai(parsed.toc)
            if not parsed.description and parsed.book_intro_url:
                parsed.description = await safe_fetch_page_text(
                    parsed.book_intro_url,
                    timeout=s.request_timeout_s,
                    client=req_client,
                    settings=s,
                )
            parsed.description = clean_description_for_ai(parsed.description)
            if nlk_hint_nonempty(parsed):
                return parsed
        except Exception as e:
            logger.warning("NLK search.do XML 실패: %s", e)

        seoji = await _fetch_nlk_seoji_hint(isbn13, s, req_client)
        return seoji
    finally:
        if owns_client:
            await req_client.aclose()
