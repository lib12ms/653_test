"""알라딘 TTB ItemLookUp + 상세페이지 크롤링."""
from __future__ import annotations

import asyncio
import json as _json
import logging
import re
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

# 책소개가 이 길이(문자수) 미만이면 출판사 제공 책소개를 병합해 함께 취급한다.
DESC_MERGE_THRESHOLD_CHARS = 150


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
        raw_publisher_desc = ""

        # 항상 크롤링 — 출판사 제공 책소개는 API에 없음
        crawl_used = True
        crawled = await _crawl_aladin_detail(isbn13, req_client)
        if not raw_desc.strip() and crawled.get("detail_description"):
            raw_desc = crawled["detail_description"]
            crawl_desc_filled = True
        if not raw_toc.strip() and crawled.get("toc"):
            raw_toc = crawled["toc"]
            crawl_toc_filled = True
        raw_publisher_desc = crawled.get("publisher_desc", "")

        # 알라딘 책소개(API/크롤링 대체분 포함)가 짧으면 출판사 제공 책소개를 병합해
        # 함께 취급한다 — description 단독으로는 키워드 추출에 부족한 경우 보강.
        # (예: 위스키 도감처럼 API 책소개가 2문장뿐이고 목차는 제품명 나열이라
        #  실질적으로 유용한 내용은 출판사 책소개에만 있는 책)
        desc_merged_with_publisher = False
        if len(raw_desc.strip()) < DESC_MERGE_THRESHOLD_CHARS and raw_publisher_desc.strip():
            raw_desc = "\n".join(p for p in (raw_desc.strip(), raw_publisher_desc.strip()) if p)
            desc_merged_with_publisher = True

        cleaned_category = clean_category_for_ai(raw_category, s.category_remove_words)
        cleaned_desc = clean_description_for_ai(raw_desc)
        cleaned_toc = clean_toc_for_ai(raw_toc)

        meta = AladinMetadata653(
            category=cleaned_category,
            title=str(item.get("title", "") or ""),
            authors=clean_author_str(str(raw_author or "")),
            description=cleaned_desc.strip(),
            toc=cleaned_toc.strip(),
            # 출판사 제공 책소개는 설명이 짧을 때만(위에서) description에 이미 병합됨.
            # 여기서는 항상 비워둔다 — 안 그러면 _build_input()이 별도 섹션으로
            # 한 번 더 넣어서 (a) 병합 안 된 경우에도 GPT에 전송되고 (b) 병합된
            # 경우 내용이 두 번 들어가는 중복이 생긴다.
            publisher_desc="",
        )
        if include_debug:
            dbg = {
                "category_raw": raw_category,
                "category_clean": cleaned_category,
                "description_raw": raw_desc[:1200],
                "description_clean": cleaned_desc[:1200],
                "toc_raw": raw_toc[:1200],
                "toc_clean": cleaned_toc[:1200],
                "publisher_desc": raw_publisher_desc[:1200],
                "crawl_used": str(crawl_used),
                "crawl_desc_filled": str(crawl_desc_filled),
                "crawl_toc_filled": str(crawl_toc_filled),
                "desc_merged_with_publisher": str(desc_merged_with_publisher),
            }
            return meta, dbg
        return meta
    finally:
        if owns_client:
            await req_client.aclose()


def _isbn13_to_isbn10(isbn13: str) -> str:
    """ISBN13 → 알라딘 URL용 ID 변환.
    978 접두어: ISBN-10으로 변환.
    979 접두어: ISBN-13 그대로 반환 (ISBN-10 변환 불가).
    """
    s = isbn13.strip().replace("-", "")
    if len(s) != 13:
        return s[:10] if len(s) >= 10 else s
    if s.startswith("979"):
        return s
    if not s.startswith("978"):
        return s[:10] if len(s) >= 10 else s
    core = s[3:12]
    check_val = (11 - (sum((10 - i) * int(d) for i, d in enumerate(core)) % 11)) % 11
    return core + ("X" if check_val == 10 else str(check_val))


def _parse_section_html(html: str) -> str:
    """알라딘 getContents.aspx HTML 응답 → 정제된 텍스트."""
    if not html or len(html) < 10:
        return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "link"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return re.sub(r"\n{3,}", "\n\n", text)
    except Exception:
        return ""


def _extract_introduce_body(text: str) -> tuple[str, str]:
    """
    Introduce 섹션 텍스트에서 (책소개 본문, 목차 텍스트) 분리.
    알라딘 Introduce 섹션은 책소개 + 목차가 함께 포함됨.
    """
    if not text:
        return "", ""

    lines = text.splitlines()
    HEADER_SKIP = {"책소개", "목차"}
    desc_lines: list[str] = []
    toc_lines: list[str] = []
    in_toc = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in HEADER_SKIP:
            if stripped == "목차":
                in_toc = True
            continue
        if in_toc:
            toc_lines.append(stripped)
        else:
            desc_lines.append(stripped)

    return "\n".join(desc_lines), "\n".join(toc_lines)


def _extract_publisher_book_intro(text: str) -> str:
    """
    PublisherDesc 섹션에서 출판사 메타(이름·최근작·분야 순위) 제거 후
    '출판사 제공 책소개' 본문만 추출.
    """
    if not text:
        return ""

    lines = text.splitlines()
    MARKER = "출판사 제공 책소개"
    SKIP_AFTER = {"출판사 제공", "책소개", "더보기", MARKER}

    start_idx = None
    for i, line in enumerate(lines):
        if line.strip() == MARKER:
            start_idx = i + 1
            break

    if start_idx is None:
        return ""

    content_lines: list[str] = []
    seen: set[str] = set()
    for line in lines[start_idx:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in SKIP_AFTER:
            continue
        if stripped in seen:      # 더보기 앞뒤로 같은 내용이 반복되므로 중복 제거
            continue
        seen.add(stripped)
        content_lines.append(stripped)

    return "\n".join(content_lines)


async def _crawl_aladin_detail(
    isbn: str,
    client: httpx.AsyncClient,
) -> dict[str, str]:
    """
    알라딘 상세페이지 3개 섹션을 getContents.aspx 직접 호출로 수집.
    - Introduce   : 책소개 + 내장 목차
    - Toc         : 목차 전문 (없으면 Introduce 내장 목차 활용)
    - PublisherDesc: 출판사 제공 책소개
    Playwright 불필요, 쿠키·세션 불필요.
    """
    result: dict[str, str] = {}
    isbn10 = _isbn13_to_isbn10(isbn)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={isbn10}",
        "X-Requested-With": "XMLHttpRequest",
    }

    async def _get(name: str) -> str:
        url = (
            f"https://www.aladin.co.kr/shop/product/getContents.aspx"
            f"?ISBN={isbn10}&name={name}&type=0"
        )
        try:
            resp = await client.get(url, headers=headers, timeout=10.0, follow_redirects=True)
            resp.raise_for_status()
            return _parse_section_html(resp.text)
        except Exception as e:
            logger.debug("알라딘 섹션 %s 추출 실패: %s", name, e)
            return ""

    introduce_text, toc_text, publisher_text = await asyncio.gather(
        _get("Introduce"), _get("Toc"), _get("PublisherDesc")
    )

    # ── 책소개 + 내장목차 분리 ─────────────────────────────────────────────────
    intro_desc, intro_toc = _extract_introduce_body(introduce_text)
    if intro_desc:
        result["detail_description"] = intro_desc[:1500]

    # ── 목차: 전용 섹션 우선, 없으면 Introduce 내장 목차 ──────────────────────
    if toc_text.strip():
        result["toc"] = toc_text[:800]
    elif intro_toc:
        result["toc"] = intro_toc[:400]

    # ── 출판사 제공 책소개 ─────────────────────────────────────────────────────
    pub_desc = _extract_publisher_book_intro(publisher_text)
    if pub_desc:
        result["publisher_desc"] = pub_desc[:1500]

    return result
