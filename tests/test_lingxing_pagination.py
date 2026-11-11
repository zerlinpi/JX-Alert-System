import pytest

from jx_alert.lingxing.client import LingxingClient, LingxingError


def _client():
    return LingxingClient(
        base_url="https://openapi.lingxing.com",
        app_id="0123456789abcdef",
        app_key="app",
        access_token="test-token",
    )


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
