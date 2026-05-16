"""ISBN 정규화."""
from app.models import normalize_isbn13


def test_normalize_strips_hyphens_and_spaces():
    assert normalize_isbn13("978-89-3643-359-8") == "9788936433598"
    assert normalize_isbn13(" 9791199691407 ") == "9791199691407"


def test_normalize_empty():
    assert normalize_isbn13("") == ""
    assert normalize_isbn13("   ") == ""
