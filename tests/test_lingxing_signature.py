import pytest
from jx_alert.lingxing.client import LingxingError, build_signature, signature_source


def test_signature_source_sorts_and_omits_empty_values():
    assert signature_source({"offset": 0, "length": 100, "blank": "", "nil": None}) == (
        "length=100&nil=null&offset=0"
    )


def test_post_body_business_parameters_are_part_of_signature():
    first = build_signature({"offset": 0, "length": 100, "timestamp": 1700000000},
                            access_token="token", app_key="app", app_id="0123456789abcdef")
    second = build_signature({"offset": 1, "length": 100, "timestamp": 1700000000},
                             access_token="token", app_key="app", app_id="0123456789abcdef")
    assert first != second


def test_nested_signature_values_fail_closed():
    with pytest.raises(LingxingError):
        signature_source({"filters": {"asin": ["B000000001"]}})
