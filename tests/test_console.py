"""Owner console: login, CSRF, write-only secrets, store directory, Skills and live previews."""
import re
import threading
import time
from datetime import datetime, timezone

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from jx_alert import admin_feishu
from jx_alert.admin_console import OwnerConsole
from jx_alert.console_auth import OwnerPassword, SessionStore, create_owner_credentials
from jx_alert.sparkx import skills as skill_store
from jx_alert.sparkx import stores
from lane_fakes import ROSTER, runner_for

ORIGIN = "https://jx.example"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def drafts():
    return []


@pytest.fixture
def gate():
    """Previews finish at once unless a test clears this gate to keep one running."""
    event = threading.Event()
    event.set()
    yield event
    event.set()


@pytest.fixture
def console(settings, session_factory, tmp_path, drafts, gate):
    secrets = tmp_path / "secrets"
    create_owner_credentials(secrets / "owner.json", PASSWORD)
    configured = settings.model_copy(update={"console_public_url": ORIGIN})
    fake = runner_for()

    def runner(settings_, lane, config, **kwargs):
        drafts.append(dict(config))
        gate.wait(10)
        return fake(settings_, lane, config, **kwargs)

    app = OwnerConsole(configured, OwnerPassword(secrets / "owner.json"), SessionStore(secrets / "sessions.sqlite3"),
                       session_factory=session_factory, runner=runner)
    client = TestClient(Starlette(routes=app.routes()), base_url=ORIGIN)
    client.owner_console = app
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
    assert overview["sparkx"]["configured"] and overview["deepseek"]["configured"]
    assert all(lane["enabled"] for lane in overview["lanes"].values()) and overview["lanes"]["hourly"]["start_hour"] == 9
    assert overview["skills"] == {"installed": False, "release": None, "count": 0}
    assert overview["feishu"]["group"] == "日报群" and overview["feishu"]["ad_lead"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", overview["csrf"])
    reports = console.get("/console/api/reports").json()
    lanes = reports["lanes"]
    assert lanes["review"]["mention_operators"] is True and lanes["review"]["to_operators"] is False
    assert "高花费零出单" in reports["default_prompts"]["review"] and reports["max_prompt"] == 4000
    lanes["hourly"]["mention_operators"] = False
    lanes["daily"]["prompt"] = "重点看 TACOS"
    assert console.post("/console/api/reports", json={"revision": reports["revision"], "lanes": lanes},
                        headers={"origin": ORIGIN}).status_code == 403
    saved = console.post("/console/api/reports", json={"revision": reports["revision"], "lanes": lanes}, headers=headers)
    assert saved.status_code == 200 and saved.json()["lanes"]["hourly"]["mention_operators"] is False
    assert saved.json()["lanes"]["daily"]["prompt"] == "重点看 TACOS"
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
                                                         "model": "deepseek-flash", "timeout": 180, "thinking": False},
                         headers=headers)
    assert saved.status_code == 200 and "sk-new-key-123" not in saved.text and saved.json()["thinking"] is False


def test_store_directory_lists_the_roster_and_saves_verified_operators(console, session_factory, monkeypatch, settings):
    with session_factory() as db:
        stores.record_roster(db, ROSTER, datetime.now(timezone.utc))
        db.commit()
    headers = login(console)
    view = console.get("/console/api/stores").json()
    assert len(view["rows"]) == 7 and [row["short_name"] for row in view["rows"]][:2] == ["Campx", "FITFIZZ"]
    assert all(row["monitored"] and row["operators"] == [] for row in view["rows"])
    assert set(view["rows"][0]) == {"profile_id", "name", "short_name", "country", "country_label", "store_type",
                                    "authorized", "monitored", "operators"}  # operators are assigned by hand only
    assert console.post("/console/api/stores/suggestions/apply", json={"revision": view["revision"]},
                        headers=headers).status_code == 404
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


def wait_for(console, job):
    for _ in range(100):
        state = console.get("/console/api/reports/preview", params={"job": job}).json()
        if state["status"] != "running":
            return state
        time.sleep(0.05)
    raise AssertionError("preview did not finish")


def test_live_preview_runs_in_the_background_and_is_read_only(console, session_factory, drafts):
    headers = login(console)
    started = console.post("/console/api/reports/preview", json={"lane": "daily"}, headers=headers)
    assert started.status_code == 200 and started.json()["lane"] == "daily" and started.json()["reused"] is False
    state = wait_for(console, started.json()["job"])
    assert state["status"] == "done"
    result = state["result"]
    assert result["cards"][0]["header"]["title"]["content"] == "SparkX 广告日报 · 2026-11-22"
    assert "全公司核心指标" in result["text"] and result["info"]["profiles"] == 7 and result["info"]["sparkx_calls"] == 4
    assert result["info"]["mentions"] == [] and "audience" not in result["info"]
    with session_factory() as db:
        from jx_alert.notifications.outbox import NotificationOutbox
        assert db.query(NotificationOutbox).count() == 0
    # Opening the same preview again shows the finished result; only 「重新生成」 runs DeepSeek again.
    again = console.post("/console/api/reports/preview", json={"lane": "daily"}, headers=headers).json()
    assert again["job"] == started.json()["job"] and again["reused"] is True and again["status"] == "done"
    assert len(drafts) == 1
    fresh = console.post("/console/api/reports/preview", json={"lane": "daily", "fresh": True}, headers=headers).json()
    assert fresh["job"] != started.json()["job"] and fresh["reused"] is False
    assert wait_for(console, fresh["job"])["status"] == "done" and len(drafts) == 2
    latest = console.get("/console/api/reports/preview", params={"lane": "daily"}).json()
    assert latest["job"] == fresh["job"] and latest["status"] == "done"
    assert console.get("/console/api/reports/preview", params={"lane": "review"}).json() == {"job": None, "status": None}
    draft = console.post("/console/api/reports/preview", json={"lane": "hourly", "draft": {"high_spend": 55, "prompt": "简短"}},
                         headers=headers)
    assert wait_for(console, draft.json()["job"])["status"] == "done"
    assert drafts[-1]["high_spend"] == 55 and drafts[-1]["prompt"] == "简短"  # unsaved form values are previewed
    assert console.post("/console/api/reports/preview", json={"lane": "other"}, headers=headers).status_code == 400
    assert console.post("/console/api/reports/preview", json={"lane": "hourly", "draft": {"low_spend": 99}},
                        headers=headers).status_code == 400  # low spend must stay below high spend
    assert console.get("/console/api/reports/preview", params={"job": "missing"}).status_code == 404


def test_reopening_a_running_preview_follows_it_instead_of_starting_another(console, drafts, gate):
    headers = login(console)
    gate.clear()  # DeepSeek is still working
    first = console.post("/console/api/reports/preview", json={"lane": "review"}, headers=headers).json()
    assert first["status"] == "running" and first["started_at"]
    reopened = console.post("/console/api/reports/preview", json={"lane": "review"}, headers=headers).json()
    assert reopened["job"] == first["job"] and reopened["reused"] is True and reopened["current"] is True
    edited = console.post("/console/api/reports/preview", json={"lane": "review", "draft": {"prompt": "只看 ACOS"}},
                          headers=headers).json()
    assert edited["job"] == first["job"] and edited["current"] is False  # the drawer says it shows older settings
    regenerate = console.post("/console/api/reports/preview", json={"lane": "review", "fresh": True}, headers=headers).json()
    assert regenerate["job"] == first["job"]  # never two DeepSeek runs of one lane at a time
    other = console.post("/console/api/reports/preview", json={"lane": "daily"}, headers=headers)
    # One preview at a time: they share the SparkX token with the scheduled lanes.
    assert other.status_code == 409 and "异常复盘" in other.json()["error"]
    gate.set()
    assert wait_for(console, first["job"])["status"] == "done"
    daily = console.post("/console/api/reports/preview", json={"lane": "daily"}, headers=headers).json()
    assert wait_for(console, daily["job"])["status"] == "done" and len(drafts) == 2
    assert console.post("/console/api/reports/preview", json={"lane": "review", "fresh": "yes"},
                        headers=headers).status_code == 400


def test_a_finished_preview_can_be_sent_as_a_test(console, session_factory, settings):
    from jx_alert.notifications.outbox import NotificationOutbox

    headers = login(console)
    job = console.post("/console/api/reports/preview", json={"lane": "review"}, headers=headers).json()["job"]
    assert wait_for(console, job)["status"] == "done"
    body = {"job": job, "to_group": True, "to_lead": True, "mention": False}
    sent = console.post("/console/api/reports/test-send", json=body, headers=headers)
    assert sent.status_code == 200 and sent.json()["status"] == "QUEUED" and sent.json()["sent"] == ["group", "lead"]
    with session_factory() as db:
        queued = db.query(NotificationOutbox).order_by(NotificationOutbox.id).all()
        assert [row.payload["delivery"]["role"] for row in queued] == ["jx_review_group", "jx_review_lead"]
        assert all(row.idempotency_key.startswith(f"jx:test:review:{job}:") for row in queued)
        assert queued[0].payload["card"]["header"]["title"]["content"].startswith("【测试】SparkX 广告异常复盘")
    assert console.post("/console/api/reports/test-send", json=body, headers=headers).json()["status"] == "ALREADY_QUEUED"
    assert wait_for(console, job)["sent"] == ["group", "lead"]
    deliveries = console.get("/console/api/deliveries").json()["rows"]
    assert deliveries[0]["lane"] == "异常复盘（测试）" and deliveries[0]["test"] is True
    assert console.post("/console/api/reports/test-send", json={**body, "to_group": False, "to_lead": False},
                        headers=headers).status_code == 400
    assert console.post("/console/api/reports/test-send", json={**body, "card": {}}, headers=headers).status_code == 400
    assert console.post("/console/api/reports/test-send", json={**body, "job": "missing"}, headers=headers).status_code == 404
    assert console.post("/console/api/reports/test-send", json=body, headers={"origin": ORIGIN}).status_code == 403
    console.owner_console.settings = console.owner_console.settings.model_copy(update={"app_dry_run": True})
    dry = console.post("/console/api/reports/test-send", json=body, headers=headers)
    assert dry.status_code == 409 and "演练模式" in dry.json()["error"]


def test_skills_status_and_install(console, monkeypatch):
    headers = login(console)
    view = console.get("/console/api/skills").json()
    assert view["installed"] is None and "get_ads_perf" in view["read_tools"] and "batch_update_ads" not in view["read_tools"]
    monkeypatch.setattr(skill_store, "install", lambda settings: (_ for _ in ()).throw(skill_store.SkillsError("离线")))
    failed = console.post("/console/api/skills/install", json={}, headers=headers)
    assert failed.status_code == 502 and failed.json()["error"] == "离线"
