"""
외부 메타데이터 수집 — 하위 호환 `app.fetcher` 진입점.

구현은 분리 모듈에 있습니다:
- `fetcher_http` — 재시도·SSL 폴백 HTTP
- `aladin_client` — 알라딘 ItemLookUp
- `nlk_client` — NLK(probe 스크립트용)
- `metadata_merge` — 알라딘 병합
"""
from __future__ import annotations

import httpx

from .aladin_client import fetch_aladin_for_653
from .metadata_merge import merge_aladin_with_nlk
from .models import NlkMetadataHint
from .nlk_client import fetch_nlk_hint_by_isbn

__all__ = [
    "fetch_aladin_for_653",
    "fetch_nlk_hint_by_isbn",
    "fetch_secondary_metadata_hint",
    "merge_aladin_with_nlk",
]


async def fetch_secondary_metadata_hint(
    isbn: str,
    settings=None,
    client: httpx.AsyncClient | None = None,
) -> tuple[NlkMetadataHint, str, None]:
    """보조 목차 조회 — KPIPA 비활성화로 항상 빈 힌트 반환."""
    return NlkMetadataHint(), "none", None
