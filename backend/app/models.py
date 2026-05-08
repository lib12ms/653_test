"""653 관련 요청/응답·메타데이터 스키마."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def normalize_isbn13(raw: str) -> str:
    s = (raw or "").strip().replace("-", "").replace(" ", "")
    return s


AnalysisMode = Literal["fast", "precise"]


class AladinMetadata653(BaseModel):
    """알라딘 API에서 653에 필요한 필드만 정규화."""

    category: str = ""
    title: str = ""
    authors: str = ""
    description: str = ""
    toc: str = ""


class NlkMetadataHint(BaseModel):
    """국립중앙도서관 API에서 653 보강에 쓸 힌트."""

    class_no: str = ""
    kwd: str = ""
    subjects: list[str] = Field(default_factory=list)
    description: str = ""
    toc: str = ""
    book_tb_cnt_url: str = ""
    book_intro_url: str = ""


class Field653FromIsbnRequest(BaseModel):
    isbn: str = Field(..., min_length=10, max_length=20, description="ISBN(하이픈 있어도 됨)")
    analysis_mode: AnalysisMode = Field(default="fast", description="653 생성 모드")

    @field_validator("isbn", mode="before")
    @classmethod
    def strip_isbn(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip()


class Field653FromMetadataRequest(BaseModel):
    """알라딘 없이 직접 메타로 테스트할 때 사용."""

    category: str = ""
    title: str = ""
    authors: str = ""
    description: str = ""
    toc: str = ""
    max_keywords: int = Field(default=7, ge=1, le=15)
    analysis_mode: AnalysisMode = Field(default="fast", description="653 생성 모드")


class Field653Response(BaseModel):
    success: bool = True
    analysis_mode: AnalysisMode = "fast"
    tag_653: str | None = None
    """예: =653  \\$a키워드1$a키워드2"""
    keywords: list[str] = Field(default_factory=list)
    raw_keyword_line: str | None = None
    """$a... 형태(653 서브필드만, =653 접두 없음)"""
    error: str | None = None
    aladin: AladinMetadata653 | None = None
    nlk_hint: NlkMetadataHint | None = None
    preprocess_debug: dict[str, str] | None = None


def parse_653_keywords(tag_653: str | None) -> list[str]:
    """
    '=653  \\$a아동문학$a정서조절' → ['아동문학', '정서조절']
    """
    if not tag_653:
        return []
    s = tag_653.strip()
    s = re.sub(r"^=653\s+\\\\", "", s)
    kws: list[str] = []
    for m in re.finditer(r"\$a([^$]+)", s):
        w = (m.group(1) or "").strip()
        if w:
            kws.append(w)
    seen: set[str] = set()
    out: list[str] = []
    for w in kws:
        if w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= 7:
            break
    return out
