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
def console(settings, session_factory, tmp_path, drafts, gate, monkeypatch):
    secrets = tmp_path / "secrets"
    create_owner_credentials(secrets / "owner.json", PASSWORD)
    configured = settings.model_copy(update={"console_public_url": ORIGIN})
    fake = runner_for()
    # The tools this token gets in write mode (the console asks SparkX live; no network in tests).
    monkeypatch.setattr("jx_alert.admin_console.offered_tool_names", lambda settings_, write=True: [
        "batch_update_ads", "get_ads_perf", "get_entity_metadata", "get_operation_log", "get_user_authorized_context"])

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
    assert result["cards"][0]["header"]["title"]["content"] == "广告日报 · 2026-11-22"
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
    assert sent.json()["cards"] == 2 and sent.json()["files"] == 2
    with session_factory() as db:
        queued = db.query(NotificationOutbox).order_by(NotificationOutbox.id).all()
        # Each card is followed by its Excel copy, to the same recipient.
        assert [(row.payload["delivery"]["role"], row.payload["transport"]) for row in queued] == [
            ("jx_review_group", "app"), ("jx_review_group", "app_file"), ("jx_review_lead", "app"),
            ("jx_review_lead", "app_file")]
        assert all(row.idempotency_key.startswith(f"jx:test:review:{job}:") for row in queued)
        # Owner (Nov 2026): no 测试 wording in the message or the file; the keys and folder tell it apart.
        title = queued[0].payload["card"]["header"]["title"]["content"]
        assert title.startswith("广告异常复盘") and "测试" not in title
        excel = queued[1].payload["file"]
        assert excel["name"] == "广告异常复盘_2026-11-16至2026-11-22.xlsx" and excel["path"].startswith("tests/")
    assert console.post("/console/api/reports/test-send", json=body, headers=headers).json()["status"] == "ALREADY_QUEUED"
    assert wait_for(console, job)["sent"] == ["group", "lead"]
    deliveries = console.get("/console/api/deliveries").json()["rows"]
    assert deliveries[0]["lane"] == "异常复盘（测试）" and deliveries[0]["test"] is True
    assert deliveries[0]["title"] == "Excel：" + excel["name"]
    # The saved Excel copies are listed and can be downloaded; nothing outside the archive can be.
    archive_rows = console.get("/console/api/archive").json()["rows"]
    assert [(row["name"], row["lane_label"]) for row in archive_rows] == [(excel["name"], "测试发送")]
    download = console.get("/console/api/archive/file", params={"path": excel["path"]})
    assert download.status_code == 200 and download.content[:2] == b"PK"
    assert download.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml")
    assert "filename*=UTF-8''" in download.headers["content-disposition"]
    for bad in ("../secrets/sparkx.json", "tests/2026-11/../../x.xlsx", "/etc/passwd", "daily/2026-11/missing.xlsx"):
        assert console.get("/console/api/archive/file", params={"path": bad}).status_code == 404
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


def test_optional_skills_can_be_switched_off_and_on(console, settings):
    """Owner (Nov 2026): every official Skill is installed; optional read Skills can be ticked on or off,
    the write Skills follow the 「广告写入」 switch."""
    from test_skills import install_fake

    install_fake(settings)
    headers = login(console)
    view = console.get("/console/api/skills").json()
    states = {entry["name"]: entry["state"] for entry in view["catalog"]}
    assert states == {"sparkx-query-ads-performance": "on", "sparkx-weekly-ads-report": "on",
                      "sparkx-edit-ads": "write_off", "sparkx-promo-prep": "write_off",
                      "sparkx-query-market-data": "missing_tools", "sparkx-skill-platform": "unsupported"}
    assert view["offered_tools"] == ["batch_update_ads", "get_ads_perf", "get_entity_metadata", "get_operation_log",
                                     "get_user_authorized_context"]
    assert "batch_update_ads" in view["write_tools"] and "batch_update_ads" not in view["read_tools"]
    body = {"revision": view["choices"]["revision"], "disabled": ["sparkx-weekly-ads-report"]}
    saved = console.post("/console/api/skills/choices", json=body, headers=headers)
    assert saved.status_code == 200 and saved.json()["choices"]["disabled"] == ["sparkx-weekly-ads-report"]
    assert {entry["name"]: entry["state"] for entry in saved.json()["catalog"]}["sparkx-weekly-ads-report"] == "off"
    assert console.post("/console/api/skills/choices", json=body, headers=headers).status_code == 409  # stale revision
    locked = {"revision": saved.json()["choices"]["revision"], "disabled": ["sparkx-query-ads-performance"]}
    assert console.post("/console/api/skills/choices", json=locked, headers=headers).status_code == 400
    writer = {"revision": saved.json()["choices"]["revision"], "disabled": ["sparkx-promo-prep"]}
    refused = console.post("/console/api/skills/choices", json=writer, headers=headers)
    assert refused.status_code == 400 and "广告写入" in refused.json()["error"]
    unknown = {"revision": saved.json()["choices"]["revision"], "disabled": ["sparkx-skill-platform"]}  # never installed
    assert console.post("/console/api/skills/choices", json=unknown, headers=headers).status_code == 400
    assert console.post("/console/api/skills/choices", json=body, headers={"origin": ORIGIN}).status_code == 403


def test_the_ad_write_switch_needs_a_confirmation_and_lists_every_attempt(console, settings, session_factory):
    """Owner (Nov 2026): writing is off by default; switching it on enables every write Skill."""
    from jx_alert.storage.models import SyncJob
    from test_skills import install_fake

    install_fake(settings)
    headers = login(console)
    view = console.get("/console/api/sparkx/write").json()
    assert view["enabled"] is False and view["revision"] == "0" * 64 and view["leads"] == ["老板", "广告主管"]
    assert view["offered_write_tools"] == ["batch_update_ads"] and view["log"] == []
    assert console.get("/console/api/overview").json()["writes"] == {"enabled": False}
    unconfirmed = {"enabled": True, "revision": view["revision"], "confirmed": False}
    refused = console.post("/console/api/sparkx/write", json=unconfirmed, headers=headers)
    assert refused.status_code == 400 and "勾选确认" in refused.json()["error"]
    assert console.post("/console/api/sparkx/write", json={**unconfirmed, "confirmed": True},
                        headers={"origin": ORIGIN}).status_code == 403  # no CSRF token
    assert console.post("/console/api/sparkx/write", json={**unconfirmed, "extra": 1}, headers=headers).status_code == 400
    on = console.post("/console/api/sparkx/write", json={**unconfirmed, "confirmed": True}, headers=headers)
    assert on.status_code == 200 and on.json()["enabled"] is True and on.json()["enabled_at"]
    stale = console.post("/console/api/sparkx/write", json={"enabled": False, "revision": view["revision"],
                                                            "confirmed": False}, headers=headers)
    assert stale.status_code == 409
    states = {entry["name"]: entry["state"] for entry in console.get("/console/api/skills").json()["catalog"]}
    assert states["sparkx-edit-ads"] == "on" and states["sparkx-promo-prep"] == "on"
    with session_factory() as db:
        db.add(SyncJob(job_type="sparkx_write", status="EXECUTED", metadata_json={
            "tool": "batch_update_ads", "requester": "老板", "request": {"profileId": 101, "request": {
                "confirmToken": "***"}}, "result": '{"status":"SUCCESS"}'}))
        db.commit()
    log = console.get("/console/api/sparkx/write").json()["log"]
    assert [(row["tool"], row["status_label"], row["requester"]) for row in log] == [("batch_update_ads", "已执行", "老板")]
    assert '"confirmToken": "***"' in log[0]["request"]
    off = console.post("/console/api/sparkx/write", json={"enabled": False, "revision": on.json()["revision"],
                                                          "confirmed": False}, headers=headers)
    assert off.status_code == 200 and off.json()["enabled"] is False and off.json()["enabled_at"] is None


def test_a_used_session_is_renewed_and_an_idle_one_ends(tmp_path):
    """Owner (Nov 2026): the console logged out after 30 minutes even while in use."""
    from jx_alert import console_auth

    now = [1_000_000.0]
    store = SessionStore(tmp_path / "sessions.sqlite3", clock=lambda: now[0])
    token = store.new()
    for _ in range(20):  # 20 working hours, one request each hour: still logged in
        now[0] += 3600
        assert store.valid(token)
    other = store.new()
    now[0] += console_auth.SESSION_IDLE + 1  # half a day without any request
    assert not store.valid(other)
    used = store.new()
    for _ in range(7 * 24):
        now[0] += 3600
        store.valid(used)
    assert not store.valid(used)  # at most 7 days after the login
    assert console_auth.SESSION_TTL == console_auth.SESSION_MAX


def test_sessions_of_the_previous_release_keep_working(tmp_path):
    import sqlite3
    from contextlib import closing
    from jx_alert import console_auth

    path = tmp_path / "sessions.sqlite3"
    store = SessionStore(path, clock=lambda: 1_000.0)
    token = store.new()  # creates the file with both tables
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM session_starts")  # a session written by the old release: no start time
        db.execute("UPDATE sessions SET expires = ?", (1_000.0 + console_auth.LEGACY_TTL,))
        # The old release inserts two values into the unchanged sessions table; that still works.
        db.execute("INSERT INTO sessions VALUES (?, ?)", ("x" * 64, 2_000.0))
    later = SessionStore(path, clock=lambda: 1_000.0 + 1200)
    assert later.valid(token)  # renewed instead of ending after 30 minutes
    assert SessionStore(path, clock=lambda: 1_000.0 + 6 * 3600).valid(token)
