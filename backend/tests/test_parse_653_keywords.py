"""653 MRK 태그에서 $a 키워드 추출."""
from app.ai_service import build_marc_653_line
from app.models import parse_653_keywords


def test_parse_respects_max_keywords():
    tag = build_marc_653_line("$a가$a나$a다")
    assert parse_653_keywords(tag, max_keywords=2) == ["가", "나"]
    assert parse_653_keywords(tag, max_keywords=10) == ["가", "나", "다"]


def test_parse_empty_and_dup():
    assert parse_653_keywords(None, max_keywords=5) == []
    assert parse_653_keywords("", max_keywords=5) == []
    tag = build_marc_653_line("$a반복$a반복$a끝")
    assert parse_653_keywords(tag, max_keywords=5) == ["반복", "끝"]


def test_max_keywords_cap_sanity():
    """내부 상한(50) — 과도한 값도 예외 없이 잘림."""
    sub = "".join(f"$a{i}" for i in range(60))
    tag = build_marc_653_line(sub)
    assert len(parse_653_keywords(tag, max_keywords=99)) == 50
