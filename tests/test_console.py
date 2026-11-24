"""Owner console: login, CSRF, write-only secrets, store directory and live previews."""
import re
from datetime import datetime, timezone

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from jx_alert import admin_feishu
from jx_alert.admin_console import OwnerConsole
from jx_alert.console_auth import OwnerPassword, SessionStore, create_owner_credentials
from jx_alert.sparkx import stores
from test_sparkx_analysis import DAILY

ORIGIN = "https://jx.example"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def console(settings, session_factory, tmp_path):
    secrets = tmp_path / "secrets"
    create_owner_credentials(secrets / "owner.json", PASSWORD)
    configured = settings.model_copy(update={"console_public_url": ORIGIN})

    def collector(settings_, lane, *, now, directory, on_roster):
        on_roster(DAILY["roster"])
        return dict(DAILY)

    app = OwnerConsole(configured, OwnerPassword(secrets / "owner.json"), SessionStore(secrets / "sessions.sqlite3"),
                       session_factory=session_factory, collector=collector)
    client = TestClient(Starlette(routes=app.routes()), base_url=ORIGIN)
    return client


def login(client):
    page = client.get("/console/login")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    response = client.post("/console/login", data={"csrf": token, "password": PASSWORD}, headers={"origin": ORIGIN},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/console/"
    csrf = re.search(r'name="jx-console-csrf" content="([0-9a-f]{64})"', client.get("/console/").text).group(1)
    return {"origin": ORIGIN, "x-csrf-token": csrf}


def test_login_is_required_and_rejects_wrong_passwords(console):
    assert console.get("/console/", follow_redirects=False).headers["location"] == "/console/login"
    assert console.get("/console/api/overview").status_code == 401
    page = console.get("/console/login")
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    wrong = console.post("/console/login", data={"csrf": token, "password": "x" * 20}, headers={"origin": ORIGIN})
    assert wrong.status_code == 403 and "口令不正确" in wrong.text
    foreign = console.post("/console/login", data={"csrf": token, "password": PASSWORD},
                           headers={"origin": "https://evil.example"})
    assert foreign.status_code == 403


def test_overview_and_csrf_protected_writes(console, settings):
    headers = login(console)
    overview = console.get("/console/api/overview").json()
    assert overview["sparkx"]["configured"] and all(lane["enabled"] for lane in overview["lanes"].values())
    assert overview["feishu"]["group"] == "日报群" and re.fullmatch(r"[0-9a-f]{64}", overview["csrf"])
    reports = console.get("/console/api/reports").json()
    lanes = reports["lanes"]
    assert lanes["review"]["mention_operators"] is True and lanes["review"]["to_operators"] is False
    lanes["hourly"]["mention_operators"] = False
    assert console.post("/console/api/reports", json={"revision": reports["revision"], "lanes": lanes},
                        headers={"origin": ORIGIN}).status_code == 403
    saved = console.post("/console/api/reports", json={"revision": reports["revision"], "lanes": lanes}, headers=headers)
    assert saved.status_code == 200 and saved.json()["lanes"]["hourly"]["mention_operators"] is False
    lanes["daily"]["unknown_switch"] = True
    rejected = console.post("/console/api/reports", json={"revision": saved.json()["revision"], "lanes": lanes},
                            headers=headers)
    assert rejected.status_code == 400


def test_secrets_are_write_only(console):
    headers = login(console)
    view = console.get("/console/api/sparkx").json()
    saved = console.post("/console/api/sparkx", json={"revision": view["revision"], "token": "new-sparkx-token"},
                         headers=headers)
    assert saved.status_code == 200 and "new-sparkx-token" not in saved.text and saved.json()["token_source"] == "console"
    deepseek = console.get("/console/api/deepseek").json()
    saved = console.post("/console/api/deepseek", json={"revision": deepseek["revision"], "api_key": "sk-new-key-123",
                                                         "model": "deepseek-flash", "timeout": 30}, headers=headers)
    assert saved.status_code == 200 and "sk-new-key-123" not in saved.text


def test_store_directory_lists_the_roster_and_saves_verified_operators(console, session_factory, monkeypatch, settings):
    with session_factory() as db:
        stores.record_roster(db, DAILY["roster"], datetime.now(timezone.utc))
        db.commit()
    headers = login(console)
    view = console.get("/console/api/stores").json()
    assert len(view["rows"]) == 7 and [row["short_name"] for row in view["rows"]][:2] == ["Campx", "FITFIZZ"]
    assert all(row["monitored"] and row["operators"] == [] for row in view["rows"])
    monkeypatch.setattr(admin_feishu, "verified_people",
                        lambda settings_, ids: [{"open_id": uid, "name": "皮泽霖"} for uid in ids])
    saved = console.post("/console/api/stores/save", json={"revision": view["revision"], "changes": {
        "101": {"monitored": True, "operator_ids": ["ou_op1"]}, "105": {"monitored": False, "operator_ids": []}}},
        headers=headers)
    assert saved.status_code == 200
    rows = {row["profile_id"]: row for row in saved.json()["rows"]}
    assert rows["101"]["operators"] == [{"id": "ou_op1", "name": "皮泽霖"}] and rows["105"]["monitored"] is False
    stale = console.post("/console/api/stores/save", json={"revision": view["revision"], "changes": {
        "101": {"monitored": True, "operator_ids": []}}}, headers=headers)
    assert stale.status_code == 409


def test_existing_vhost_routes_reach_a_clear_answer(console):
    root = console.get("/", follow_redirects=False)
    assert root.status_code == 302 and root.headers["location"] == "/console/"
    for path in ("/mcp", "/authorize", "/oauth/owner", "/.well-known/oauth-protected-resource/mcp"):
        response = console.post(path) if path == "/mcp" else console.get(path)
        assert response.status_code == 410 and "SparkX MCP" in response.json()["error"]
    assert console.get("/anything-else").status_code == 404


def test_live_preview_is_read_only(console, session_factory):
    headers = login(console)
    preview = console.post("/console/api/reports/preview", json={"lane": "daily"}, headers=headers)
    assert preview.status_code == 200
    body = preview.json()
    assert body["card"]["header"]["title"]["content"] == "SparkX 广告日报 · 2026-11-22"
    assert "全公司核心指标" in body["text"] and body["info"]["profiles"] == 7
    with session_factory() as db:
        from jx_alert.notifications.outbox import NotificationOutbox
        assert db.query(NotificationOutbox).count() == 0
    assert console.post("/console/api/reports/preview", json={"lane": "other"}, headers=headers).status_code == 400
