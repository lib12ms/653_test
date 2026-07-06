"""알라딘 메타와 보조 출처(KPIPA 목차) 병합."""
from __future__ import annotations

from .config import Settings, get_settings
from .models import AladinMetadata653, NlkMetadataHint
from .preprocess import (
    clean_category_for_ai,
    clean_description_for_ai,
    clean_toc_for_ai,
)


def merge_aladin_with_nlk(
    base: AladinMetadata653,
    nlk: NlkMetadataHint,
    settings: Settings | None = None,
    secondary_source: str = "none",
    content_code: str = "",
) -> AladinMetadata653:
    """
    알라딘을 주 정보원으로 두고 보강한다.
    - secondary_source == 'kpipa': KPIPA에서 가져온 목차(nlk.toc)만 알라딘 목차에 덧붙임.
    - 'none': 알라딘만(전처리만).
    - content_code: ISBN 부가기호 기반 3자리 내용분류코드(있으면 분야 라우팅 보조 신호로 함께 보관).
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
        publisher_desc=base.publisher_desc,
        content_code=content_code,
    )
