"""네이버 책검색 API — 설명문 보강."""
from __future__ import annotations

import html
import logging
import re
from typing import Any

import httpx

from .config import Settings, get_settings
from .fetcher_http import get_json
from .models import normalize_isbn13

logger = logging.getLogger(__name__)

_NAVER_BOOK_URL = "https://openapi.naver.com/v1/search/book_adv.json"


def _clean_naver_text(text: str) -> str:
    """HTML 태그·엔티티 제거 및 공백 정규화."""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_naver_book_description(raw: dict[str, Any]) -> str:
    """네이버 책검색 응답에서 description 추출·정제."""
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        return ""
    return _clean_naver_text(items[0].get("description") or "")


async def fetch_naver_book(
    isbn: str,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """네이버 책검색 advanced API — ISBN으로 단건 조회. 비활성화·키 미설정 시 None 반환."""
    s = get_settings() if settings is None else settings
    isbn13 = normalize_isbn13(isbn)
    if not isbn13 or not s.naver_enable or not (s.naver_client_id and s.naver_client_secret):
        return None

    req_client = client or httpx.AsyncClient()
    owns_client = client is None
    try:
        raw = await get_json(
            _NAVER_BOOK_URL,
            params={"d_isbn": isbn13},
            timeout=s.request_timeout_s,
            client=req_client,
            settings=s,
            extra_headers={
                "X-Naver-Client-Id": s.naver_client_id,
                "X-Naver-Client-Secret": s.naver_client_secret,
            },
        )
        if not isinstance(raw, dict):
            return None
        return raw
    except Exception as e:
        logger.warning("네이버 책검색 실패: %s", e)
        return None
    finally:
        if owns_client:
            await req_client.aclose()
