"""알라딘 + KPIPA 목차 병합."""
from app.config import Settings
from app.metadata_merge import merge_aladin_with_nlk
from app.models import AladinMetadata653, NlkMetadataHint


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # 테스트에서 .env 의존 제거
        category_remove_words_csv="",
    )


def test_merge_kpipa_appends_toc():
    s = _settings()
    base = AladinMetadata653(toc="section-alpha")
    hint = NlkMetadataHint(toc="section-beta")
    m = merge_aladin_with_nlk(base, hint, settings=s, secondary_source="kpipa")
    assert "section-alpha" in m.toc
    assert "section-beta" in m.toc


def test_merge_none_skips_secondary_toc():
    s = _settings()
    base = AladinMetadata653(toc="base-only")
    hint = NlkMetadataHint(toc="external-line")
    m = merge_aladin_with_nlk(base, hint, settings=s, secondary_source="none")
    assert "external-line" not in m.toc


def test_merge_no_duplicate_substring():
    s = _settings()
    base = AladinMetadata653(toc="already has beta block")
    hint = NlkMetadataHint(toc="beta")
    m = merge_aladin_with_nlk(base, hint, settings=s, secondary_source="kpipa")
    assert m.toc.count("beta") == 1
