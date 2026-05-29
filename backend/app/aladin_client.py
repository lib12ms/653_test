"""알라딘 TTB ItemLookUp + 상세페이지 크롤링."""
from __future__ import annotations

import json as _json
import logging
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

logger = logging.getLogger(__name__)


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

        crawl_used = False
        crawl_desc_filled = False
        crawl_toc_filled = False
        playwright_used = False

        need_crawl = not raw_desc.strip() or not raw_toc.strip()
        if need_crawl:
            crawl_used = True
            crawled = await _crawl_aladin_detail(isbn13, req_client)
            if not raw_desc.strip() and crawled.get("detail_description"):
                raw_desc = crawled["detail_description"]
                crawl_desc_filled = True
            if not raw_toc.strip() and crawled.get("toc"):
                raw_toc = crawled["toc"]
                crawl_toc_filled = True
            if crawled.get("playwright_used") == "true":
                playwright_used = True

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
                "crawl_used": str(crawl_used),
                "crawl_desc_filled": str(crawl_desc_filled),
                "crawl_toc_filled": str(crawl_toc_filled),
                "playwright_used": str(playwright_used),
            }
            return meta, dbg
        return meta
    finally:
        if owns_client:
            await req_client.aclose()


def _isbn13_to_isbn10(isbn13: str) -> str:
    """ISBN13(978 접두) → ISBN10 변환. 알라딘 페이지 요소 ID에 사용."""
    s = isbn13.strip().replace("-", "")
    if len(s) != 13 or not s.startswith("978"):
        return s[:10] if len(s) >= 10 else s
    core = s[3:12]
    check_val = (11 - (sum((10 - i) * int(d) for i, d in enumerate(core)) % 11)) % 11
    return core + ("X" if check_val == 10 else str(check_val))


async def _playwright_fetch_toc(isbn10: str) -> str:
    """
    Playwright로 알라딘 상세페이지를 JS 렌더링 후 목차 텍스트 추출.
    playwright 패키지가 설치되어 있지 않으면 즉시 빈 문자열 반환.

    설치 방법:
        pip install playwright
        playwright install chromium
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ""

    # ID가 숫자로 시작하므로 CSS attribute selector 사용
    intro_attr = f'[id="{isbn10}_Introduce"]'
    url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={isbn10}"
    toc = ""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            try:
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="ko-KR",
                    viewport={"width": 1280, "height": 800},
                )
                page = await ctx.new_page()
                await page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                # loadContent() AJAX 완료 대기 (최대 8초)
                try:
                    await page.wait_for_function(
                        f"(function(){{ var el = document.querySelector('{intro_attr}');"
                        f" return el && el.innerText.trim().length > 20; }})()",
                        timeout=8_000,
                    )
                except Exception:
                    pass
                el = await page.query_selector(intro_attr)
                if el:
                    toc = (await el.inner_text() or "").strip()
            finally:
                await browser.close()
    except Exception as e:
        logger.debug("Playwright 목차 추출 실패: %s", e)
    return toc


async def _crawl_aladin_detail(
    isbn: str,
    client: httpx.AsyncClient,
) -> dict[str, str]:
    """
    알라딘 상세페이지 크롤링.
    - 설명: 정적 HTML의 JSON-LD / og:description 에서 추출 (안정적)
    - 목차: Playwright로 JS 렌더링 후 추출 (playwright 패키지 필요)
    """
    result: dict[str, str] = {}

    # ── 설명: 정적 HTML ────────────────────────────────────────────────────────
    url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={isbn}"
    try:
        from bs4 import BeautifulSoup

        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=10.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # JSON-LD description (가장 신뢰)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld = _json.loads(script.string or "")
                desc = str(ld.get("description") or "").strip()
                if desc:
                    result["detail_description"] = desc[:1500]
                    break
            except Exception:
                continue

        # fallback: og:description
        if not result.get("detail_description"):
            og = soup.find("meta", property="og:description")
            if og:
                desc = str(og.get("content") or "").strip()
                if desc:
                    result["detail_description"] = desc[:1500]

    except Exception as e:
        logger.debug("알라딘 정적 페이지 설명 추출 실패: %s", e)

    # ── 목차: Playwright (JS 렌더링 필요) ──────────────────────────────────────
    isbn10 = _isbn13_to_isbn10(isbn)
    toc = await _playwright_fetch_toc(isbn10)
    if toc:
        result["toc"] = toc[:800]
        result["playwright_used"] = "true"

    return result
