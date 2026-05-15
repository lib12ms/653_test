"""알라딘 + 국립중앙도서관 외부 API 수집(httpx + tenacity)."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings, get_settings
from .models import AladinMetadata653, NlkMetadataHint, normalize_isbn13
from .preprocess import (
    clean_author_str,
    clean_category_for_ai,
    clean_description_for_ai,
    clean_toc_for_ai,
)

logger = logging.getLogger(__name__)


def _can_use_insecure_fallback(url: str, settings: Settings) -> bool:
    if not settings.allow_insecure_ssl_fallback:
        return False
    allowed_hosts = settings.insecure_ssl_fallback_hosts
    if not allowed_hosts:
        return False
    host = (urlparse(url).hostname or "").lower()
    return host in allowed_hosts


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(
        exc,
        (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout),
    )


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.7, min=0.7, max=8),
    retry=retry_if_exception(_is_retryable),
)
async def _get_json(
    url: str,
    params: dict[str, Any],
    timeout: float,
    client: httpx.AsyncClient,
    settings: Settings,
) -> dict[str, Any]:
    headers = {
        "User-Agent": "I2M-653/1.0 (library metadata)",
        "Accept": "application/json",
    }
    try:
        r = await client.get(url, params=params, timeout=timeout, headers=headers)
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError as e:
        emsg = str(e).lower()
        if "certificate verify failed" not in emsg and "self-signed" not in emsg:
            raise
        if not _can_use_insecure_fallback(url, settings):
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
    retry=retry_if_exception(_is_retryable),
)
async def _get_text(
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
        if not _can_use_insecure_fallback(url, settings):
            raise
        logger.warning("SSL 검증 실패로 제한적 verify=False 폴백: %s", url)
        async with httpx.AsyncClient(verify=False) as insecure_client:
            r = await insecure_client.get(url, params=params, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r.text


def _to_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(_to_text(x) for x in v if _to_text(x))
    return str(v).strip()


def _first_value(doc: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = doc.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def _strip_html(text: str) -> str:
    s = text or ""
    s = re.sub(r"(?is)<script.*?>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style.*?>.*?</style>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


async def _safe_fetch_page_text(
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
        return _strip_html(r.text)[:5000]
    except Exception:
        pass
    if not _can_use_insecure_fallback(url, settings):
        return ""
    try:
        async with httpx.AsyncClient(verify=False) as insecure_client:
            r = await insecure_client.get(url, timeout=timeout, follow_redirects=True)
            r.raise_for_status()
            return _strip_html(r.text)[:5000]
    except Exception:
        return ""


def _extract_subjects(doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k in ("subject", "subjects", "keyword", "keywords", "subjectHeading"):
        v = doc.get(k)
        if isinstance(v, str) and v.strip():
            parts = re.split(r"[,;/|·\n]+", v)
            out.extend(p.strip() for p in parts if p.strip())
        elif isinstance(v, list):
            for x in v:
                t = _to_text(x)
                if t:
                    out.append(t)
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:12]


def _parse_nlk_json(raw: dict[str, Any]) -> NlkMetadataHint:
    docs = raw.get("result") or raw.get("docs") or raw.get("doc") or raw.get("item")
    if isinstance(docs, list) and docs:
        doc = docs[0] if isinstance(docs[0], dict) else {}
    elif isinstance(docs, dict):
        doc = docs
    else:
        doc = raw if isinstance(raw, dict) else {}

    return NlkMetadataHint(
        class_no=_first_value(doc, ("kdc", "kdcCode", "classNo", "class_no", "classification")),
        kwd=_first_value(doc, ("kwd", "keyword", "keywords", "subjectHeading", "subject")),
        subjects=_extract_subjects(doc),
        description=_first_value(doc, ("description", "contents", "abstract", "summary")),
        toc=_first_value(doc, ("toc", "tableOfContents")),
        book_tb_cnt_url=_first_value(doc, ("BOOK_TB_CNT_URL", "bookTbCntUrl", "book_tb_cnt_url")),
        book_intro_url=_first_value(doc, ("BOOK_INTRODUCTION_URL", "bookIntroductionUrl", "book_introduction_url")),
    )


def _parse_nlk_xml(xml_text: str) -> NlkMetadataHint:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return NlkMetadataHint()

    def pick(*tags: str) -> str:
        for tag in tags:
            node = root.find(f".//{tag}")
            if node is not None and (node.text or "").strip():
                return node.text.strip()
        return ""

    subj_nodes = root.findall(".//subject") + root.findall(".//keyword")
    subjects = [(n.text or "").strip() for n in subj_nodes if (n.text or "").strip()]

    seen: set[str] = set()
    uniq: list[str] = []
    for sub in subjects:
        if sub not in seen:
            seen.add(sub)
            uniq.append(sub)

    return NlkMetadataHint(
        class_no=pick("kdc", "classNo", "classification"),
        kwd=pick("kwd", "keyword", "keywords", "subject"),
        subjects=uniq[:12],
        description=pick("description", "contents", "summary", "abstract"),
        toc=pick("toc", "tableOfContents"),
        book_tb_cnt_url=pick("BOOK_TB_CNT_URL", "bookTbCntUrl", "book_tb_cnt_url"),
        book_intro_url=pick("BOOK_INTRODUCTION_URL", "bookIntroductionUrl", "book_introduction_url"),
    )


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
        try:
            raw_json = await _get_json(
                s.nlk_api_url,
                {**params, "apiType": "json"},
                timeout=s.request_timeout_s,
                client=req_client,
                settings=s,
            )
            parsed = _parse_nlk_json(raw_json)
            if not parsed.toc and parsed.book_tb_cnt_url:
                parsed.toc = clean_toc_for_ai(
                    await _safe_fetch_page_text(
                        parsed.book_tb_cnt_url,
                        timeout=s.request_timeout_s,
                        client=req_client,
                        settings=s,
                    )
                )
            else:
                parsed.toc = clean_toc_for_ai(parsed.toc)
            if not parsed.description and parsed.book_intro_url:
                parsed.description = await _safe_fetch_page_text(
                    parsed.book_intro_url,
                    timeout=s.request_timeout_s,
                    client=req_client,
                    settings=s,
                )
            parsed.description = clean_description_for_ai(parsed.description)
            if parsed.class_no or parsed.kwd or parsed.subjects or parsed.description or parsed.toc:
                return parsed
        except Exception:
            logger.info("NLK JSON 파싱 실패, XML로 재시도")

        try:
            raw_xml = await _get_text(
                s.nlk_api_url,
                {**params, "apiType": "xml"},
                timeout=s.request_timeout_s,
                client=req_client,
                settings=s,
            )
            parsed = _parse_nlk_xml(raw_xml)
            if not parsed.toc and parsed.book_tb_cnt_url:
                parsed.toc = clean_toc_for_ai(
                    await _safe_fetch_page_text(
                        parsed.book_tb_cnt_url,
                        timeout=s.request_timeout_s,
                        client=req_client,
                        settings=s,
                    )
                )
            else:
                parsed.toc = clean_toc_for_ai(parsed.toc)
            if not parsed.description and parsed.book_intro_url:
                parsed.description = await _safe_fetch_page_text(
                    parsed.book_intro_url,
                    timeout=s.request_timeout_s,
                    client=req_client,
                    settings=s,
                )
            parsed.description = clean_description_for_ai(parsed.description)
            return parsed
        except Exception as e:
            logger.warning("NLK 조회 실패: %s", e)
            return NlkMetadataHint()
    finally:
        if owns_client:
            await req_client.aclose()


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
        data = await _get_json(
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


def merge_aladin_with_nlk(
    base: AladinMetadata653,
    nlk: NlkMetadataHint,
    settings: Settings | None = None,
) -> AladinMetadata653:
    """
    653 생성에 필요한 텍스트를 NLK 힌트로 보강한다.
    - 알라딘 기본값을 유지하고, 비어 있거나 약한 필드만 보수적으로 덧붙인다.
    """
    merged_desc = (base.description or "").strip()
    merged_desc = clean_description_for_ai(merged_desc)
    if nlk.description and nlk.description not in merged_desc:
        merged_desc = (
            f"{merged_desc}\n{clean_description_for_ai(nlk.description)}".strip()
            if merged_desc
            else clean_description_for_ai(nlk.description)
        )

    merged_toc = (base.toc or "").strip()
    merged_toc = clean_toc_for_ai(merged_toc)
    if nlk.toc and nlk.toc not in merged_toc:
        merged_toc = (
            f"{merged_toc}\n{clean_toc_for_ai(nlk.toc)}".strip()
            if merged_toc
            else clean_toc_for_ai(nlk.toc)
        )

    s = get_settings() if settings is None else settings
    merged_category = (base.category or "").strip()
    merged_category = clean_category_for_ai(merged_category, s.category_remove_words)
    if nlk.class_no:
        class_hint = f"국립중앙도서관KDC:{nlk.class_no}"
        if class_hint not in merged_category:
            merged_category = f"{merged_category} > {class_hint}".strip(" >")

    if nlk.subjects:
        subj_hint = " ".join(nlk.subjects[:8])
        if subj_hint and subj_hint not in merged_toc:
            merged_toc = f"{merged_toc}\n주제어힌트:{subj_hint}".strip() if merged_toc else f"주제어힌트:{subj_hint}"
    kwd_norm = re.sub(r"[-\s]", "", nlk.kwd or "")
    if nlk.kwd and not kwd_norm.isdigit() and nlk.kwd not in merged_toc:
        merged_toc = f"{merged_toc}\nNLK키워드:{nlk.kwd}".strip() if merged_toc else f"NLK키워드:{nlk.kwd}"
    if nlk.book_tb_cnt_url and nlk.book_tb_cnt_url not in merged_toc:
        merged_toc = (
            f"{merged_toc}\nNLK목차URL:{nlk.book_tb_cnt_url}".strip()
            if merged_toc
            else f"NLK목차URL:{nlk.book_tb_cnt_url}"
        )
    if nlk.book_intro_url and nlk.book_intro_url not in merged_desc:
        merged_desc = (
            f"{merged_desc}\nNLK소개URL:{nlk.book_intro_url}".strip()
            if merged_desc
            else f"NLK소개URL:{nlk.book_intro_url}"
        )

    return AladinMetadata653(
        category=merged_category,
        title=base.title,
        authors=base.authors,
        description=merged_desc,
        toc=clean_toc_for_ai(merged_toc),
    )
