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

        need_crawl = not raw_desc.strip() or not raw_toc.strip()
        if need_crawl:
            crawled = await _crawl_aladin_detail(isbn13, req_client)
            if not raw_desc.strip() and crawled.get("detail_description"):
                raw_desc = crawled["detail_description"]
            if not raw_toc.strip() and crawled.get("toc"):
                raw_toc = crawled["toc"]

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
    finally:
        if owns_client:
            await req_client.aclose()


async def _crawl_aladin_detail(
    isbn: str,
    client: httpx.AsyncClient,
) -> dict[str, str]:
    """
    알라딘 상세페이지 크롤링.
    fulldescription/toc가 API에서 비어있을 때 보완용.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {}

    url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={isbn}"
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception:
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")

    detail_desc = ""
    desc_div = soup.select_one("div.Ere_prod_mconts_R")
    if desc_div:
        detail_desc = desc_div.get_text(separator=" ", strip=True)

    toc = ""
    toc_div = soup.select_one("div#div_TOC_All")
    if toc_div:
        toc = toc_div.get_text(separator=" ", strip=True)

    return {
        "detail_description": detail_desc[:800],
        "toc": toc[:400],
    }
