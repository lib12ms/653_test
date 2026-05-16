"""
외부 메타데이터 수집 — 하위 호환 `app.fetcher` 진입점.

구현은 분리 모듈에 있습니다:
- `fetcher_http` — 재시도·SSL 폴백 HTTP
- `aladin_client` — 알라딘 ItemLookUp
- `kpipa_client` — KPIPA 목차
- `nlk_client` — NLK(probe 스크립트용)
- `metadata_merge` — 알라딘 + 보조 출처 병합
"""
from __future__ import annotations

from .aladin_client import fetch_aladin_for_653
from .kpipa_client import (
    extract_kpipa_book_payload,
    fetch_kpipa_hint_by_isbn,
    fetch_secondary_metadata_hint,
)
from .metadata_merge import merge_aladin_with_nlk
from .nlk_client import fetch_nlk_hint_by_isbn

__all__ = [
    "extract_kpipa_book_payload",
    "fetch_aladin_for_653",
    "fetch_kpipa_hint_by_isbn",
    "fetch_nlk_hint_by_isbn",
    "fetch_secondary_metadata_hint",
    "merge_aladin_with_nlk",
]
