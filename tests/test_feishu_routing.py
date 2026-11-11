import json
from jx_alert.notifications.worker import _load_routing, _routed_open_ids


def test_store_and_entity_open_id_routing(tmp_path):
    path = tmp_path / "routing.json"
    path.write_text(json.dumps({
        "stores": {"store-1": ["ou_store_owner"]},
        "entities": {"store-1:ASIN1": ["ou_product_owner"], "ASIN2": ["ou_global_owner"]},
    }), encoding="utf-8")
    routing = _load_routing(str(path))
    ids = _routed_open_ids(routing, "store-1", "ASIN1")
    assert ids == ["ou_store_owner", "ou_product_owner"]


def test_invalid_routing_id_is_rejected(tmp_path):
    path = tmp_path / "routing.json"
    path.write_text('{"stores":{"store-1":["not-an-open-id"]}}', encoding="utf-8")
    try:
        _load_routing(str(path))
    except RuntimeError as exc:
        assert "Open ID" in str(exc)
    else:
        raise AssertionError("invalid routing ID should be rejected")
