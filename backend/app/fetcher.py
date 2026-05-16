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
    if not isinstance(raw, dict):
        return NlkMetadataHint()
    # search.do 는 검색 결과가 없을 때 total=0 과 요청어 에코(kwd)만 주는 경우가 많음.
    # 이때 루트 dict 를 doc 으로 쓰면 kwd=ISBN 이 키워드로 오인됨.
    tot = raw.get("total")
    if tot is not None:
        try:
            if int(str(tot).strip()) == 0:
                return NlkMetadataHint()
        except ValueError:
            pass

    docs = raw.get("result") or raw.get("docs") or raw.get("doc") or raw.get("item")
    if isinstance(docs, list) and docs:
        doc = docs[0] if isinstance(docs[0], dict) else {}
    elif isinstance(docs, dict):
        doc = docs
    else:
        doc = {}

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


def _hint_from_seoji_doc(doc: dict[str, Any]) -> NlkMetadataHint:
    """ISBN 서지(Seoji) API docs[] 첫 행 → NlkMetadataHint."""
    if not isinstance(doc, dict):
        return NlkMetadataHint()
    kdc = _to_text(doc.get("KDC") or doc.get("kdc"))
    intro = _to_text(doc.get("BOOK_INTRODUCTION"))
    summary = _to_text(doc.get("BOOK_SUMMARY"))
    tb_cnt = _to_text(doc.get("BOOK_TB_CNT"))
    parts = [p for p in (intro, summary) if p]
    description = "\n".join(parts) if parts else ""
    return NlkMetadataHint(
        class_no=kdc,
        kwd="",
        subjects=[],
        description=description,
        toc=tb_cnt,
        book_tb_cnt_url=_to_text(doc.get("BOOK_TB_CNT_URL")),
        book_intro_url=_to_text(doc.get("BOOK_INTRODUCTION_URL")),
    )


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
    raw = await _get_json(
        s.nlk_seoji_api_url,
        params,
        timeout=s.request_timeout_s,
        client=client,
        settings=s,
    )
    docs = raw.get("docs")
    if not isinstance(docs, list) or not docs or not isinstance(docs[0], dict):
        return NlkMetadataHint()
    hint = _hint_from_seoji_doc(docs[0])
    if not hint.toc and hint.book_tb_cnt_url:
        hint.toc = clean_toc_for_ai(
            await _safe_fetch_page_text(
                hint.book_tb_cnt_url,
                timeout=s.request_timeout_s,
                client=client,
                settings=s,
            )
        )
    else:
        hint.toc = clean_toc_for_ai(hint.toc)
    if not hint.description and hint.book_intro_url:
        hint.description = await _safe_fetch_page_text(
            hint.book_intro_url,
            timeout=s.request_timeout_s,
            client=client,
            settings=s,
        )
    hint.description = clean_description_for_ai(hint.description)
    return hint


def _nlk_hint_nonempty(h: NlkMetadataHint) -> bool:
    return bool(
        h.class_no
        or h.kwd
        or h.subjects
        or h.description
        or h.toc
        or h.book_tb_cnt_url
        or h.book_intro_url
    )


def _extract_kpipa_book_payload(raw: dict[str, Any]) -> dict[str, Any] | None:
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


def _kpipa_collateral_text(product: dict[str, Any], text_type: str | int) -> str:
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
        cleaned = _strip_html(merged) if "<" in merged else merged
        (plain if aud0 == "02" else fallback).append(cleaned)
    if plain:
        return max(plain, key=len)
    if fallback:
        return max(fallback, key=len)
    return ""


def _parse_kpipa_toc_only(raw: dict[str, Any]) -> NlkMetadataHint:
    """KPIPA ONIX Product에서 목차(TextType 04)만 추출 → 힌트의 toc만 채움."""
    product = _extract_kpipa_book_payload(raw)
    if not product:
        return NlkMetadataHint()
    toc_raw = _kpipa_collateral_text(product, "04")
    return NlkMetadataHint(toc=clean_toc_for_ai(toc_raw))


async def fetch_kpipa_hint_by_isbn(
    isbn: str,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> NlkMetadataHint:
    """KPIPA getBookDetail — 응답 중 ONIX 목차(TextContent 04)만 사용."""
    s = get_settings() if settings is None else settings
    isbn13 = normalize_isbn13(isbn)
    if not isbn13 or not s.kpipa_enable or not s.kpipa_api_key:
        return NlkMetadataHint()

    base = s.kpipa_api_base_url.rstrip("/")
    url = f"{base}/api/openApi/metaInfoSvc/getBookDetail"
    params: dict[str, Any] = {"apiKey": s.kpipa_api_key, "isbn": isbn13}
    req_client = client or httpx.AsyncClient()
    owns_client = client is None
    try:
        raw = await _get_json(
            url,
            params,
            timeout=s.request_timeout_s,
            client=req_client,
            settings=s,
        )
        if not isinstance(raw, dict):
            return NlkMetadataHint()
        resp = raw.get("response")
        if isinstance(resp, dict):
            res = resp.get("result")
            if isinstance(res, dict):
                code = str(res.get("resultCode", "")).upper()
                if code and code != "INFO-000":
                    return NlkMetadataHint()
        return _parse_kpipa_toc_only(raw)
    except Exception as e:
        logger.warning("KPIPA getBookDetail 실패: %s", e)
        return NlkMetadataHint()
    finally:
        if owns_client:
            await req_client.aclose()


async def fetch_secondary_metadata_hint(
    isbn: str,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[NlkMetadataHint, str]:
    """
    알라딘 외 보강: KPIPA에서 목차만 조회(앱 본선에서 NLK 미사용).
    반환: (힌트, 출처) — 'kpipa'(목차 있음) | 'none'.
    """
    s = get_settings() if settings is None else settings
    req = client or httpx.AsyncClient()
    owns = client is None
    try:
        if not (s.kpipa_enable and s.kpipa_api_key):
            return NlkMetadataHint(), "none"
        hint = await fetch_kpipa_hint_by_isbn(isbn, settings=s, client=req)
        if (hint.toc or "").strip():
            return hint, "kpipa"
        return NlkMetadataHint(), "none"
    finally:
        if owns:
            await req.aclose()


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
            if _nlk_hint_nonempty(parsed):
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
            if _nlk_hint_nonempty(parsed):
                return parsed
        except Exception as e:
            logger.warning("NLK search.do XML 실패: %s", e)

        seoji = await _fetch_nlk_seoji_hint(isbn13, s, req_client)
        return seoji
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
    secondary_source: str = "none",
) -> AladinMetadata653:
    """
    알라딘을 주 정보원으로 두고 보강한다.
    - secondary_source == 'kpipa': KPIPA에서 가져온 목차(nlk.toc)만 알라딘 목차에 덧붙임.
    - 'none': 알라딘만(전처리만).
    """
    s = get_settings() if settings is None else settings
    merged_category = clean_category_for_ai((base.category or "").strip(), s.category_remove_words)
    merged_desc = clean_description_for_ai((base.description or "").strip())
    merged_toc = clean_toc_for_ai((base.toc or "").strip())

    if secondary_source == "kpipa" and (nlk.toc or "").strip():
        kt = clean_toc_for_ai(nlk.toc)
        if kt and kt not in merged_toc:
            merged_toc = f"{merged_toc}\n{kt}".strip() if merged_toc else kt

    return AladinMetadata653(
        category=merged_category,
        title=base.title,
        authors=base.authors,
        description=merged_desc,
        toc=clean_toc_for_ai(merged_toc),
    )
