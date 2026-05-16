"""KPIPA ONIX JSON에서 Product·목차 추출(네트워크 없음)."""
from app.kpipa_client import extract_kpipa_book_payload, kpipa_collateral_text, parse_kpipa_toc_only


def _minimal_response(product: dict) -> dict:
    return {"response": {"body": {"items": {"Product": product}}}}


def test_extract_single_product_dict():
    p = {"RecordReference": "x", "DescriptiveDetail": {}}
    assert extract_kpipa_book_payload(_minimal_response(p)) == p


def test_extract_first_of_list():
    a = {"id": "first"}
    b = {"id": "second"}
    assert extract_kpipa_book_payload(_minimal_response([a, b])) == a


def test_extract_missing_returns_none():
    assert extract_kpipa_book_payload({}) is None
    assert extract_kpipa_book_payload({"response": {}}) is None
    assert extract_kpipa_book_payload({"response": {"body": {}}}) is None


def test_collateral_prefers_content_audience_02():
    product = {
        "CollateralDetail": {
            "TextContent": [
                {"TextType": "04", "ContentAudience": ["00"], "Text": ["짧음"]},
                {
                    "TextType": "04",
                    "ContentAudience": ["02"],
                    "Text": ["긴 목차 본문 여기 더김"],
                },
            ]
        }
    }
    assert kpipa_collateral_text(product, "04") == "긴 목차 본문 여기 더김"


def test_parse_kpipa_toc_only_minimal():
    raw = _minimal_response(
        {
            "CollateralDetail": {
                "TextContent": [
                    {
                        "TextType": "04",
                        "ContentAudience": ["02"],
                        "Text": ["장1 장2 장3"],
                    }
                ]
            }
        }
    )
    hint = parse_kpipa_toc_only(raw)
    assert "장1" in hint.toc
    assert "장3" in hint.toc


def test_parse_kpipa_toc_only_no_product():
    hint = parse_kpipa_toc_only({"response": {"result": {"resultCode": "INFO-105"}}})
    assert hint.toc == ""
