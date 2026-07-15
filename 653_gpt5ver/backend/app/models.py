"""653 관련 요청/응답·메타데이터 스키마."""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator


def normalize_isbn13(raw: str) -> str:
    s = (raw or "").strip().replace("-", "").replace(" ", "")
    return s


class AladinMetadata653(BaseModel):
    """알라딘 API에서 653에 필요한 필드만 정규화."""

    category: str = ""
    title: str = ""
    authors: str = ""
    description: str = ""
    toc: str = ""
    publisher_desc: str = ""  # 출판사 제공 책소개 (getContents.aspx 크롤링)
    content_code: str = ""  # ISBN 부가기호(EA_ADD_CODE) 마지막 3자리 — 분야 라우팅 보조 신호


class NlkMetadataHint(BaseModel):
    """보조 메타 힌트 구조체 — toc 필드만 병합 파이프라인에서 참조."""

    class_no: str = ""
    ea_add_code: str = ""  # ISBN 부가기호(5자리, Seoji EA_ADD_CODE) — KDC보다 실제로 채워지는 필드
    kwd: str = ""
    subjects: list[str] = Field(default_factory=list)
    description: str = ""
    toc: str = ""
    book_tb_cnt_url: str = ""
    book_intro_url: str = ""


class Field653FromIsbnRequest(BaseModel):
    isbn: str = Field(..., min_length=10, max_length=20, description="ISBN(하이픈 있어도 됨)")

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


class TokenUsage(BaseModel):
    """OpenAI chat/completions usage (해당 653 생성 호출 1회)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    breakdown: dict[str, int] = Field(default_factory=dict)
    """입력 프롬프트를 구성 섹션별 글자수 비율로 나눈 추정 토큰수(합계=prompt_tokens).
    예: {"지침 프롬프트": 812, "도서 정보(API·크롤링)": 214}"""


class Field653Quality(BaseModel):
    """ISBN 처리마다 자동 산출되는 키워드 품질 지표."""

    ai_raw_count: int = 0
    """AI가 출력한 키워드 수 (후처리 전)"""
    filtered_count: int = 0
    """후처리에서 차단된 키워드 수"""
    final_count: int = 0
    """최종 키워드 수"""
    backup_used: bool = False
    """AI 유효 키워드가 0개인지 여부 (텍스트 fallback 실행 여부와 무관 — 문학은 텍스트 fallback 미사용)"""
    category_fallback_used: bool = False
    """min_keywords 미달로 카테고리 fallback을 사용했는지"""
    quality_score: float = 0.0
    """0.0~1.0 종합 품질 점수"""
    flags: list[str] = Field(default_factory=list)
    """경고 플래그 목록 (예: ['과다차단', 'fallback사용'])"""
    fallback_keywords: list[str] = Field(default_factory=list)
    """텍스트/카테고리 fallback으로 보충된 키워드 목록 (AI 유효 키워드 제외)"""
    raw_keywords: list[str] = Field(default_factory=list)
    """finalize_653()에 입력된 AI 생성 키워드 목록 (should_keep_keyword 통과 후)"""
    blocked_keywords: list[str] = Field(default_factory=list)
    """finalize_653()에서 차단된 키워드 목록 (저효용어·과잉AI·중복 포함)"""

    @property
    def filter_rate(self) -> float:
        if self.ai_raw_count == 0:
            return 0.0
        return round(self.filtered_count / self.ai_raw_count, 3)


class Field653Response(BaseModel):
    success: bool = True
    tag_653: str | None = None
    """예: =653  \\$a키워드1$a키워드2"""
    keywords: list[str] = Field(default_factory=list)
    raw_keyword_line: str | None = None
    """$a... 형태(653 서브필드만, =653 접두 없음)"""
    error: str | None = None
    token_usage: TokenUsage | None = None
    aladin: AladinMetadata653 | None = None
    preprocess_debug: dict[str, str] | None = None
    duration_ms: float | None = None
    fallback_keywords: list[str] = Field(default_factory=list)
    """갯수 부족으로 fallback(텍스트/카테고리)에서 보충된 키워드 목록"""


def parse_653_keywords(tag_653: str | None, *, max_keywords: int = 15) -> list[str]:
    """
    '=653  \\$a아동문학$a정서조절' → ['아동문학', '정서조절']

    max_keywords: 응답·표시용 키워드 상한. API는 `Settings.max_keywords_653` 또는
    `Field653FromMetadataRequest.max_keywords`를 넘겨야 설정과 일치한다.
    """
    cap = max(1, min(int(max_keywords), 50))
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
        if len(out) >= cap:
            break
    return out
