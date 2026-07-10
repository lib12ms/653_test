"""환경·시스템 설정(OPENAI, 알라딘, 모델명 등)."""
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    aladin_ttb_key: str = Field(default="", description="알라딘 TTB API 키")
    openai_api_key: str = Field(default="", description="OpenAI API 키")
    openai_model: str = Field(default="gpt-4o", description="653 생성용 채팅 모델")
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="OpenAI 호환 API 베이스 URL",
    )
    kormarc_agent_conv_id: Optional[str] = Field(
        default=None,
        description=(
            "[미사용] 구 Conversation 방식 conv ID. "
            "턴 누적 문제로 instructions 방식으로 전환됨. 설정해도 무시됨."
        ),
    )
    aladin_item_lookup_url: str = Field(
        default="https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx",
    )
    nlk_api_key: str = Field(default="", description="국립중앙도서관 OpenAPI 인증키")
    nlk_api_url: str = Field(
        default="https://www.nl.go.kr/NL/search/openApi/search.do",
        description="국립중앙도서관 소장 검색 OpenAPI URL",
    )
    nlk_seoji_api_url: str = Field(
        default="https://www.nl.go.kr/seoji/SearchApi.do",
        description="국립중앙도서관 ISBN 서지(Seoji) OpenAPI URL",
    )
    nlk_enable: bool = Field(
        default=False,
        description="NLK OpenAPI(앱 본선 파이프라인에서는 미사용; probe_nlk_isbns 등 스크립트용)",
    )
    request_timeout_s: float = 30.0
    allow_insecure_ssl_fallback: bool = Field(
        default=False,
        description="인증서 검증 실패 시 verify=False 폴백 허용 여부(기본 비활성)",
    )
    insecure_ssl_fallback_hosts_csv: str = Field(
        default="",
        description="verify=False 폴백을 허용할 호스트 목록(CSV). 비어 있으면 전체 차단",
    )
    max_keywords_653: int = 7
    min_keywords_653: int = Field(default=5, ge=1, le=15)
    isbn_cache_ttl_s: int = Field(default=600, ge=0, description="ISBN 결과 캐시 TTL(초)")
    isbn_cache_max_entries: int = Field(default=2000, ge=1, description="ISBN 결과 캐시 최대 항목 수")
    field653_cache_bundle_version: str = Field(
        default="1",
        description=(
            "/api/field653 ISBN 캐시 키에 포함. 병합·전처리·프롬프트 변경 시 버전을 올려 기존 캐시 무효화"
        ),
    )
    category_remove_words_csv: str = Field(
        default="국내도서,외국도서,실용서,단행본,ebook,e-book,전자책,베스트셀러,신간,스테디셀러,md추천",
        description="카테고리 정제 시 제거할 유통/판매 분류어(CSV)",
    )

    @property
    def category_remove_words(self) -> list[str]:
        return [w.strip() for w in self.category_remove_words_csv.split(",") if w.strip()]

    @property
    def insecure_ssl_fallback_hosts(self) -> list[str]:
        return [
            w.strip().lower()
            for w in self.insecure_ssl_fallback_hosts_csv.split(",")
            if w.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
