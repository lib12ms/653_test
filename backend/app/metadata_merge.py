"""알라딘 메타 전처리 및 보조 목차 병합."""
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
    nlk: NlkMetadataHint | None = None,
    settings: Settings | None = None,
    secondary_source: str = "none",
    content_code: str = "",
) -> AladinMetadata653:
    """
    알라딘을 주 정보원으로 두고 전처리한다.
    nlk, secondary_source 파라미터는 하위 호환을 위해 유지하나 목차 병합은 수행하지 않는다.
    content_code: ISBN 부가기호 기반 3자리 내용분류코드(분야 라우팅 보조 신호).
    """
    s = get_settings() if settings is None else settings
    merged_category = clean_category_for_ai((base.category or "").strip(), s.category_remove_words)
    merged_desc = clean_description_for_ai((base.description or "").strip())
    merged_toc = clean_toc_for_ai((base.toc or "").strip())

    return AladinMetadata653(
        category=merged_category,
        title=base.title,
        authors=base.authors,
        description=merged_desc,
        toc=clean_toc_for_ai(merged_toc),
        publisher_desc=base.publisher_desc,
        content_code=content_code,
    )
