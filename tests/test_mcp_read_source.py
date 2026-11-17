from types import SimpleNamespace

import pytest

from jx_alert.lingxing import mcp_read_source as source


DAY = "2026-11-17"


def product_response(**updates):
    row = {"sids": [101], "rdate": DAY, "row_type": "asin", "currency_code": "USD",
           "asin": "B000001", "amount": "100.00", "volume": 2, "order_items": 2}
    row.update(updates)
    return {"code": 1, "data": {"code": 1, "data": {"all_visible": True, "total": 1, "list": [row]}}}


def reader(monkeypatch, response):
    monkeypatch.setattr(source, "mcp_credentials", lambda settings: None)
    value = source.OfficialReadSource(SimpleNamespace())
    monkeypatch.setattr(value, "check_store", lambda sid: None)
    monkeypatch.setattr(value, "call", lambda tool, params: response)
    return value


@pytest.mark.parametrize("updates", [{"sids": [999]}, {"sids": [101, 102]},
    {"rdate": "2026-11-16"}, {"currency_code": None}, {"row_type": "msku"},
    {"amount": None}, {"volume": -1}, {"order_items": 1.5}])
def test_product_fallback_requires_exact_observed_grain_date_and_values(monkeypatch, updates):
    with pytest.raises(source.MCPReadError):
        reader(monkeypatch, product_response(**updates)).sales("101", DAY, "asin")


def test_complete_product_observation_keeps_purchase_values_and_source_pages(monkeypatch):
    rows, pages = reader(monkeypatch, product_response()).sales("101", DAY, "asin")
    assert rows == [{"entity_id": "B000001", "sales": 100, "units": 2, "orders": 2, "currency": "USD"}]
    assert pages[0]["request"]["date_type"] == "purchase"
    assert pages[0]["request"]["start_date"] == DAY


def test_partial_visibility_and_incomplete_pagination_are_never_complete(monkeypatch):
    response = product_response()
    response["data"]["data"]["all_visible"] = False
    with pytest.raises(source.MCPReadError):
        reader(monkeypatch, response).sales("101", DAY, "asin")
    response = product_response()
    response["data"]["data"]["total"] = 2
    value = reader(monkeypatch, response)
    responses = iter([response, {"code": 1, "data": {"code": 1, "data": {
        "all_visible": True, "total": 2, "list": []}}}])
    monkeypatch.setattr(value, "call", lambda tool, params: next(responses))
    with pytest.raises(source.MCPReadError):
        value.sales("101", DAY, "asin")


def test_one_reader_pins_identity_and_next_collection_uses_rotated_configuration(monkeypatch):
    current = ["https://official.invalid", "key-identity-one"]
    monkeypatch.setattr(source, "mcp_credentials", lambda settings: tuple(current))
    old = source.OfficialReadSource(SimpleNamespace())
    current[1] = "key-identity-two"
    new = source.OfficialReadSource(SimpleNamespace())
    assert old._credentials[1] == "key-identity-one"
    assert new._credentials[1] == "key-identity-two"


def test_unapproved_tool_is_rejected_before_any_transport_or_credential_use(monkeypatch):
    value = reader(monkeypatch, {})
    coroutine = value._invoke("put_campaigns_sp", {})
    try:
        with pytest.raises(source.MCPReadError, match="allowlist"):
            coroutine.send(None)
    finally:
        coroutine.close()
