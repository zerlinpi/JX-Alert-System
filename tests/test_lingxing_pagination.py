import pytest

from jx_alert.lingxing.client import LingxingClient, LingxingError


def _client():
    return LingxingClient(
        base_url="https://openapi.lingxing.com",
        app_id="0123456789abcdef",
        app_key="app",
        access_token="test-token",
    )


@pytest.mark.parametrize("kind, method, iterate", [
    ("sb", "advertising_report", "iter_advertising_report"),
    ("sd", "advertising_product_report", "iter_advertising_product_report"),
])
def test_sb_activity_and_sd_product_follow_v2_cursor_when_available(monkeypatch, kind, method, iterate):
    client = _client()
    calls = []
    def read(report_type, request):
        calls.append(dict(request))
        return {"code": 0, "total": 2, "next_token": "first" if len(calls) == 1 else "last",
                "data": [{"id": len(calls)}]}
    monkeypatch.setattr(client, method, read)
    assert list(getattr(client, iterate)(kind, {"sid": 101, "report_date": "2026-11-13"}, length=1)) == [
        {"id": 1}, {"id": 2}]
    assert calls[0]["offset"] == 0
    assert calls[1].get("next_token") == "first" and "offset" not in calls[1]


def test_sp_report_uses_next_token_cursor(monkeypatch):
    client = _client()
    calls = []
    pages = [
        {"code": 0, "total": 3, "next_token": "cursor-2", "data": [{"id": 1}, {"id": 2}]},
        {"code": 0, "total": 3, "next_token": "", "data": [{"id": 3}]},
    ]

    def fake_report(report_type, payload):
        calls.append(dict(payload))
        return pages.pop(0)

    monkeypatch.setattr(client, "advertising_report", fake_report)
    rows = list(client.iter_advertising_report(
        "sp", {"sid": 100, "report_date": "2026-11-10"}
    ))
    assert [row["id"] for row in rows] == [1, 2, 3]
    assert "next_token" not in calls[0]
    assert calls[1]["next_token"] == "cursor-2"
    assert "offset" not in calls[1]


def test_sb_report_uses_offset_pagination(monkeypatch):
    client = _client()
    offsets = []
    pages = [
        {"code": 0, "total": 3, "data": [{"id": 1}, {"id": 2}]},
        {"code": 0, "total": 3, "data": [{"id": 3}]},
    ]

    def fake_report(report_type, payload):
        offsets.append(payload["offset"])
        return pages.pop(0)

    monkeypatch.setattr(client, "advertising_report", fake_report)
    rows = list(client.iter_advertising_report(
        "sb", {"sid": 100, "report_date": "2026-11-10"}
    ))
    assert [row["id"] for row in rows] == [1, 2, 3]
    assert offsets == [0, 2]


def test_sp_report_fails_closed_when_cursor_missing(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client,
        "advertising_report",
        lambda *_: {"code": 0, "total": 3, "data": [{"id": 1}]},
    )
    with pytest.raises(LingxingError, match="without next_token"):
        list(client.iter_advertising_report(
            "sp", {"sid": 100, "report_date": "2026-11-10"}
        ))


@pytest.mark.parametrize("report_type", ["sp", "sb", "sd"])
@pytest.mark.parametrize("changed_total", [2, 4])
def test_campaign_report_rejects_changed_total_before_yielding_that_page(
    monkeypatch, report_type, changed_total,
):
    client = _client()
    pages = [
        {"code": 0, "total": 3, "next_token": "page-2", "data": [{"id": 1}]},
        {"code": 0, "total": changed_total, "next_token": "", "data": [{"id": 2}]},
    ]
    monkeypatch.setattr(client, "advertising_report", lambda *_: pages.pop(0))
    rows = client.iter_advertising_report(
        report_type, {"sid": 100, "report_date": "2026-11-10"}
    )

    assert next(rows) == {"id": 1}
    # A total shrinking from 3 to 2 used to return two rows as a complete report.
    with pytest.raises(LingxingError, match="total changed during pagination"):
        next(rows)
    assert pages == []



def test_campaign_master_uses_next_token_cursor(monkeypatch):
    client = _client()
    calls = []
    pages = [
        {"code": 0, "total": 2, "next_token": "campaign-cursor", "data": [{"campaign_id": 1}]},
        {"code": 0, "total": 2, "next_token": "", "data": [{"campaign_id": 2}]},
    ]

    def fake_campaigns(campaign_type, payload):
        calls.append(dict(payload))
        return pages.pop(0)

    monkeypatch.setattr(client, "advertising_campaigns", fake_campaigns)
    rows = list(client.iter_advertising_campaigns("sp", {"sid": 101}))
    assert [row["campaign_id"] for row in rows] == [1, 2]
    assert calls[1]["next_token"] == "campaign-cursor"
    assert "offset" not in calls[1]


def test_sp_product_report_uses_cursor_and_sd_uses_offset(monkeypatch):
    client = _client()
    sp_calls = []
    sp_pages = [
        {"code": 0, "total": 2, "next_token": "product-cursor", "data": [{"ad_id": 1}]},
        {"code": 0, "total": 2, "next_token": "", "data": [{"ad_id": 2}]},
    ]

    def fake_sp(report_type, payload):
        sp_calls.append(dict(payload))
        return sp_pages.pop(0)

    monkeypatch.setattr(client, "advertising_product_report", fake_sp)
    sp_rows = list(client.iter_advertising_product_report(
        "sp", {"sid": 101, "report_date": "2026-11-10"}
    ))
    assert [row["ad_id"] for row in sp_rows] == [1, 2]
    assert sp_calls[1]["next_token"] == "product-cursor"

    sd_offsets = []
    sd_pages = [
        {"code": 0, "total": 2, "data": [{"ad_id": 1}]},
        {"code": 0, "total": 2, "data": [{"ad_id": 2}]},
    ]

    def fake_sd(report_type, payload):
        sd_offsets.append(payload["offset"])
        return sd_pages.pop(0)

    monkeypatch.setattr(client, "advertising_product_report", fake_sd)
    sd_rows = list(client.iter_advertising_product_report(
        "sd", {"sid": 101, "report_date": "2026-11-10"}
    ))
    assert [row["ad_id"] for row in sd_rows] == [1, 2]
    assert sd_offsets == [0, 1]



def test_sp_sd_product_ad_master_data_use_next_token_cursor(monkeypatch):
    client = _client()
    calls = []
    pages = [
        {"code": 0, "total": 2, "next_token": "ad-cursor", "data": [{"ad_id": 1}]},
        {"code": 0, "total": 2, "next_token": "", "data": [{"ad_id": 2}]},
    ]

    def fake_ads(campaign_type, payload):
        calls.append((campaign_type, dict(payload)))
        return pages.pop(0)

    monkeypatch.setattr(client, "advertising_product_ads", fake_ads)
    for campaign_type in ("sp", "sd"):
        pages[:] = [
            {"code": 0, "total": 2, "next_token": "ad-cursor", "data": [{"ad_id": 1}]},
            {"code": 0, "total": 2, "next_token": "", "data": [{"ad_id": 2}]},
        ]
        rows = list(client.iter_advertising_product_ads(
            campaign_type, {"sid": 101}
        ))
        assert [row["ad_id"] for row in rows] == [1, 2]
        assert calls[-2][1]["offset"] == 0
        assert calls[-1][1]["next_token"] == "ad-cursor"
        assert "offset" not in calls[-1][1]


def test_product_ad_master_data_fails_closed_when_cursor_missing(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client,
        "advertising_product_ads",
        lambda *_: {"code": 0, "total": 2, "data": [{"ad_id": 1}]},
    )
    with pytest.raises(LingxingError, match="missing next_token"):
        list(client.iter_advertising_product_ads("sp", {"sid": 101}))
