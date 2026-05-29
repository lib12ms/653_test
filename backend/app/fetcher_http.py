"""공용 HTTP: 재시도, SSL 호스트 제한 폴백, HTML 정제."""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings

logger = logging.getLogger(__name__)


def can_use_insecure_fallback(url: str, settings: Settings) -> bool:
    if not settings.allow_insecure_ssl_fallback:
        return False
    allowed_hosts = settings.insecure_ssl_fallback_hosts
    if not allowed_hosts:
        return False
    host = (urlparse(url).hostname or "").lower()
    return host in allowed_hosts


def is_retryable_http(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(
        exc,
        (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout),
    )


def strip_html(text: str) -> str:
    s = text or ""
    s = re.sub(r"(?is)<script.*?>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style.*?>.*?</style>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.7, min=0.7, max=8),
    retry=retry_if_exception(is_retryable_http),
)
async def get_json(
    url: str,
    params: dict[str, Any],
    timeout: float,
    client: httpx.AsyncClient,
    settings: Settings,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = {
        "User-Agent": "I2M-653/1.0 (library metadata)",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    try:
        r = await client.get(url, params=params, timeout=timeout, headers=headers)
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError as e:
        emsg = str(e).lower()
        if "certificate verify failed" not in emsg and "self-signed" not in emsg:
            raise
        if not can_use_insecure_fallback(url, settings):
            raise
        logger.warning("SSL 검증 실패로 제한적 verify=False 폴백: %s", url)
        async with httpx.AsyncClient(verify=False) as insecure_client:
            r = await insecure_client.get(url, params=params, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r.json()


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.7, min=0.7, max=8),
    retry=retry_if_exception(is_retryable_http),
)
async def get_text(
    url: str,
    params: dict[str, Any],
    timeout: float,
    client: httpx.AsyncClient,
    settings: Settings,
) -> str:
    headers = {
        "User-Agent": "I2M-653/1.0 (library metadata)",
        "Accept": "*/*",
    }
    try:
        r = await client.get(url, params=params, timeout=timeout, headers=headers)
        r.raise_for_status()
        return r.text
    except httpx.ConnectError as e:
        emsg = str(e).lower()
        if "certificate verify failed" not in emsg and "self-signed" not in emsg:
            raise
        if not can_use_insecure_fallback(url, settings):
            raise
        logger.warning("SSL 검증 실패로 제한적 verify=False 폴백: %s", url)
        async with httpx.AsyncClient(verify=False) as insecure_client:
            r = await insecure_client.get(url, params=params, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r.text


async def safe_fetch_page_text(
    url: str,
    timeout: float,
    client: httpx.AsyncClient,
    settings: Settings,
) -> str:
    if not url:
        return ""
    try:
        r = await client.get(url, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        return strip_html(r.text)[:5000]
    except Exception:
        pass
    if not can_use_insecure_fallback(url, settings):
        return ""
    try:
        async with httpx.AsyncClient(verify=False) as insecure_client:
            r = await insecure_client.get(url, timeout=timeout, follow_redirects=True)
            r.raise_for_status()
            return strip_html(r.text)[:5000]
    except Exception:
        return ""
