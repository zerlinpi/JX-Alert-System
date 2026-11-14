import json

import httpx
import pytest

from jx_alert.lingxing.client import LingxingClient, LingxingError, build_signature


@pytest.fixture
def detail_api(monkeypatch):
    requests = []
    pages = []
    original = httpx.Client

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=pages.pop(0))

    monkeypatch.setattr(
        httpx, "Client", lambda timeout: original(timeout=timeout, transport=httpx.MockTransport(handler))
    )
    client = LingxingClient(
        base_url="https://openapi.lingxing.com",
        app_id="0123456789abcdef", app_key="test-app", access_token="test-token",
    )
    return client, pages, requests


@pytest.mark.parametrize(("dimension", "endpoint", "extra", "expected_extra"), [
    ("creative_sb", "listHsaProductAdReport", {}, {}),
    ("target_sb", "listHsaTargetingReport", {}, {"sponsored_type": "ALL", "target_type": "ALL"}),
    ("purchased_asin_sb", "hsaPurchasedAsinReports", {}, {}),
    ("target_sd", "sdTargetReports", {}, {}),
    ("purchased_asin_sd", "sdAsinReports", {"show_detail": 1}, {"show_detail": 1}),
])
def test_detail_report_uses_only_documented_signed_read_only_endpoints(
    detail_api, dimension, endpoint, extra, expected_extra
):
    client, pages, requests = detail_api
    pages.append({"code": 0, "total": 0, "data": [], "next_token": None})
    result = list(client.iter_advertising_detail_report(
        dimension, {"sid": 5174, "report_date": "2026-11-13", **extra},
    ))
    assert result == []
    request, = requests
    assert request.method == "POST"
    assert request.url.path == "/pb/openapi/newad/" + endpoint
    assert request.headers["X-API-VERSION"] == "2"
    body = json.loads(request.content)
    assert body == {"sid": 5174, "report_date": "2026-11-13", "offset": 0, "length": 100, **expected_extra}
    assert request.url.params["sign"] == build_signature(
        {**body, "timestamp": int(request.url.params["timestamp"])},
        app_id=client.app_id, app_key=client.app_key, access_token=client.access_token,
    )


def test_detail_report_follows_token_without_offset(detail_api):
    client, pages, requests = detail_api
    pages.extend([
        {"code": 0, "total": 3, "data": [{"target_id": 1}, {"target_id": 2}], "next_token": "next-2"},
        {"code": 0, "total": 3, "data": [{"target_id": 3}], "next_token": ""},
    ])
    rows = list(client.iter_advertising_detail_report(
        "target_sd", {"profile_id": "123456", "report_date": "2026-11-13"}, length=2,
    ))
    assert rows == [{"target_id": 1}, {"target_id": 2}, {"target_id": 3}]
    assert [json.loads(request.content) for request in requests] == [
        {"profile_id": 123456, "report_date": "2026-11-13", "offset": 0, "length": 2},
        {"profile_id": 123456, "report_date": "2026-11-13", "next_token": "next-2", "length": 2},
    ]


@pytest.mark.parametrize(("page", "message"), [
    ({"code": 0, "total": 2, "data": [{"target_id": 2}]}, "total changed"),
    ({"code": 0, "total": 3, "data": [{"target_id": 2}]}, "stopped before"),
    ({"code": 0, "total": 3, "data": [{"target_id": 2}], "next_token": "next-2"}, "repeated next_token"),
    ({"code": 0, "total": 3, "data": [], "next_token": "next-3"}, "stopped before"),
    ({"code": 0, "total": 3, "data": ["invalid"]}, "array of objects"),
    ({"code": 0, "total": 3.0, "data": [{"target_id": 2}]}, "total is missing or invalid"),
    ({"code": 403, "total": 0, "data": []}, "API error"),
])
def test_detail_report_rejects_invalid_pages_before_yielding_them(detail_api, page, message):
    client, pages, _ = detail_api
    pages.extend([
        {"code": 0, "total": 3, "data": [{"target_id": 1}], "next_token": "next-2"},
        page,
    ])
    rows = client.iter_advertising_detail_report("target_sd", {"sid": 5174, "report_date": "2026-11-13"})
    assert next(rows) == {"target_id": 1}
    with pytest.raises(LingxingError, match=message):
        next(rows)


def test_detail_report_rejects_filtered_or_resumed_partitions(detail_api):
    client, _, requests = detail_api
    payload = {"sid": 5174, "report_date": "2026-11-13"}
    with pytest.raises(ValueError, match="all target types"):
        list(client.iter_advertising_detail_report("target_sb", {**payload, "target_type": "keyword"}))
    with pytest.raises(ValueError, match="Unsupported advertising detail report parameter"):
        list(client.iter_advertising_detail_report("target_sd", {**payload, "next_token": "skip-1"}))
    with pytest.raises(ValueError, match="Unsupported advertising detail dimension"):
        list(client.iter_advertising_detail_report("/bad-path", payload))
    assert requests == []
