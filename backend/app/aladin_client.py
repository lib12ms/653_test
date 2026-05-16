"""알라딘 TTB ItemLookUp."""
from __future__ import annotations

from typing import Any

import httpx

from .config import Settings, get_settings
from .fetcher_http import get_json
from .models import AladinMetadata653, normalize_isbn13
from .preprocess import (
    clean_author_str,
    clean_category_for_ai,
    clean_description_for_ai,
    clean_toc_for_ai,
)


async def fetch_aladin_for_653(
    isbn: str,
    settings: Settings | None = None,
    include_debug: bool = False,
    client: httpx.AsyncClient | None = None,
) -> AladinMetadata653 | tuple[AladinMetadata653, dict[str, str]]:
    """
    ItemLookUp으로 분류/서명/저자/설명/목차를 가져온 뒤 AladinMetadata653로 반환.
    """
    s = get_settings() if settings is None else settings
    isbn13 = normalize_isbn13(isbn)
    if not isbn13:
        raise ValueError("ISBN이 비어 있습니다.")
    if not s.aladin_ttb_key:
        raise ValueError("ALADIN_TTB_KEY가 설정되지 않았습니다.")

    params: dict[str, Any] = {
        "ttbkey": s.aladin_ttb_key,
        "ItemIdType": "ISBN",
        "ItemId": isbn13,
        "output": "js",
        "Version": "20131101",
        "OptResult": "Toc,authors,fulldescription",
    }
    req_client = client or httpx.AsyncClient()
    owns_client = client is None
    try:
        data = await get_json(
            s.aladin_item_lookup_url,
            params,
            timeout=s.request_timeout_s,
            client=req_client,
            settings=s,
        )
    finally:
        if owns_client:
            await req_client.aclose()

    item_list = data.get("item")
    if not item_list or not isinstance(item_list, list):
        raise ValueError("알라딘 API에서 도서를 찾지 못했습니다.")
    item: dict[str, Any] = item_list[0]

    raw_author = (
        item.get("author")
        or item.get("authors")
        or item.get("author_t")
        or ""
    )
    if isinstance(raw_author, list):
        raw_author = " ".join(str(x) for x in raw_author)

    sub: dict[str, Any] = (item.get("subInfo") or {}) or {}
    raw_category = str(item.get("categoryName", "") or "")
    raw_desc = str((item.get("fulldescription") or item.get("description") or "") or "")
    raw_toc = str((item.get("toc") or sub.get("toc") or "") or "")

    cleaned_category = clean_category_for_ai(raw_category, s.category_remove_words)
    cleaned_desc = clean_description_for_ai(raw_desc)
    cleaned_toc = clean_toc_for_ai(raw_toc)

    meta = AladinMetadata653(
        category=cleaned_category,
        title=str(item.get("title", "") or ""),
        authors=clean_author_str(str(raw_author or "")),
        description=cleaned_desc.strip(),
        toc=cleaned_toc.strip(),
    )
    if include_debug:
        dbg = {
            "category_raw": raw_category,
            "category_clean": cleaned_category,
            "description_raw": raw_desc[:1200],
            "description_clean": cleaned_desc[:1200],
            "toc_raw": raw_toc[:1200],
            "toc_clean": cleaned_toc[:1200],
        }
        return meta, dbg
    return meta
