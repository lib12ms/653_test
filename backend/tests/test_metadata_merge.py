"""알라딘 메타 병합 — 알라딘 단독 전처리 검증."""
from app.config import Settings
from app.metadata_merge import merge_aladin_with_nlk
from app.models import AladinMetadata653


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        category_remove_words_csv="",
    )


def test_merge_returns_base_toc():
    s = _settings()
    base = AladinMetadata653(toc="section-alpha")
    m = merge_aladin_with_nlk(base, settings=s)
    assert "section-alpha" in m.toc


def test_merge_none_source_skips_hint_toc():
    s = _settings()
    from app.models import NlkMetadataHint
    base = AladinMetadata653(toc="base-only")
    hint = NlkMetadataHint(toc="external-line")
    m = merge_aladin_with_nlk(base, hint, settings=s, secondary_source="none")
    assert "external-line" not in m.toc
