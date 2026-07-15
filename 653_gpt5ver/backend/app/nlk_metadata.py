"""NLK/search·Seoji 응답 파싱(순수 함수)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from .models import NlkMetadataHint


def to_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(to_text(x) for x in v if to_text(x))
    return str(v).strip()


def first_value(doc: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = doc.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def extract_subjects(doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k in ("subject", "subjects", "keyword", "keywords", "subjectHeading"):
        v = doc.get(k)
        if isinstance(v, str) and v.strip():
            parts = re.split(r"[,;/|·\n]+", v)
            out.extend(p.strip() for p in parts if p.strip())
        elif isinstance(v, list):
            for x in v:
                t = to_text(x)
                if t:
                    out.append(t)
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:12]


def parse_nlk_json(raw: dict[str, Any]) -> NlkMetadataHint:
    if not isinstance(raw, dict):
        return NlkMetadataHint()
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
        class_no=first_value(doc, ("kdc", "kdcCode", "classNo", "class_no", "classification")),
        kwd=first_value(doc, ("kwd", "keyword", "keywords", "subjectHeading", "subject")),
        subjects=extract_subjects(doc),
        description=first_value(doc, ("description", "contents", "abstract", "summary")),
        toc=first_value(doc, ("toc", "tableOfContents")),
        book_tb_cnt_url=first_value(doc, ("BOOK_TB_CNT_URL", "bookTbCntUrl", "book_tb_cnt_url")),
        book_intro_url=first_value(
            doc, ("BOOK_INTRODUCTION_URL", "bookIntroductionUrl", "book_introduction_url")
        ),
    )


def parse_nlk_xml(xml_text: str) -> NlkMetadataHint:
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


def hint_from_seoji_doc(doc: dict[str, Any]) -> NlkMetadataHint:
    """ISBN 서지(Seoji) API docs[] 첫 행 → NlkMetadataHint.

    실응답에서 KDC 필드는 대부분 비어 있고 EA_ADD_CODE(진짜 ISBN 부가기호)가
    채워지는 것으로 확인됨(2026-07-03 스파이크) — 둘 다 기록해둔다.
    """
    if not isinstance(doc, dict):
        return NlkMetadataHint()
    kdc = to_text(doc.get("KDC") or doc.get("kdc"))
    ea_add_code = to_text(doc.get("EA_ADD_CODE") or doc.get("ea_add_code"))
    intro = to_text(doc.get("BOOK_INTRODUCTION"))
    summary = to_text(doc.get("BOOK_SUMMARY"))
    tb_cnt = to_text(doc.get("BOOK_TB_CNT"))
    parts = [p for p in (intro, summary) if p]
    description = "\n".join(parts) if parts else ""
    return NlkMetadataHint(
        class_no=kdc,
        ea_add_code=ea_add_code,
        kwd="",
        subjects=[],
        description=description,
        toc=tb_cnt,
        book_tb_cnt_url=to_text(doc.get("BOOK_TB_CNT_URL")),
        book_intro_url=to_text(doc.get("BOOK_INTRODUCTION_URL")),
    )


def content_code_from_hint(h: NlkMetadataHint) -> str:
    """NlkMetadataHint → 3자리 내용분류코드(KDC 강목 상당).

    EA_ADD_CODE(5자리) 마지막 3자리를 우선 사용하고, 없으면 class_no(KDC)
    앞 3자리로 대체한다. 유효하지 않으면 빈 문자열.
    """
    ea = (h.ea_add_code or "").strip()
    if len(ea) >= 5 and ea.isdigit():
        return ea[-3:]
    kdc = (h.class_no or "").strip()
    if len(kdc) >= 3 and kdc[:3].isdigit():
        return kdc[:3]
    return ""


def nlk_hint_nonempty(h: NlkMetadataHint) -> bool:
    return bool(
        h.class_no
        or h.ea_add_code
        or h.kwd
        or h.subjects
        or h.description
        or h.toc
        or h.book_tb_cnt_url
        or h.book_intro_url
    )