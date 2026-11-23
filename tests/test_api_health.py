from fastapi.testclient import TestClient

from jx_alert import api


def test_health_reports_sources_without_secrets(monkeypatch, settings, session_factory):
    monkeypatch.setattr(api, "engine", session_factory.engine)
    monkeypatch.setattr(api, "init_db", lambda: None)
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    with TestClient(api.app) as client:
        body = client.get("/health").json()
    assert body == {"status": "ok", "service": "jx-alert-system", "data_source": "sparkx", "dry_run": False,
                    "scheduler_enabled": False, "feishu_delivery_enabled": True, "sparkx_configured": True,
                    "deepseek_configured": False}
    assert "synthetic-sparkx-token" not in str(body)
