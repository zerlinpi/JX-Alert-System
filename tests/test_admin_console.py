from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.applications import Starlette
from starlette.testclient import TestClient

from jx_alert import admin_groups, admin_mapping
from jx_alert import admin_data
from jx_alert.admin_console import OwnerConsole, SESSION
from jx_alert.config import Settings
from jx_alert.storage.db import Base
from jx_alert.storage.models import Store, DailyMetric, DataCoverage, SyncJob


ORIGIN = "https://console.example.com"
PASSWORD = "isolated owner test password"
DAY = date(2024, 10, 12)


@pytest.fixture
def console(tmp_path, monkeypatch):
    scope, zones, routing = (tmp_path / s for s in ("scope.json", "zones.json", "routing.json"))
    for path, value in (
        (scope, {"store_ids": [5174, 5175]}),
        (zones, {"stores": {"5174": {"timezone": "America/Los_Angeles", "currency_code": "USD"}}}),
        (routing, {"default_chat_id": "ops", "groups": {"ops": {"chat_id": "oc_test", "name": "测试群"}},
                   "stores": {"5174": {"open_ids": ["ou_old"], "chat_id": "ops"}}, "entities": {}}),
    ):
        path.write_text(json.dumps(value), encoding="utf-8")
    settings = Settings(_env_file=None, collection_scope_file=str(scope), store_timezones_file=str(zones),
                        store_controls_file=str(tmp_path / "controls.json"),
                        feishu_routing_file=str(routing), feishu_transport="app",
                        feishu_daily_report_chat_id="ops", feishu_group_fallback_enabled=True)
    engine = create_engine(f"sqlite:///{tmp_path / 'warehouse.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as db:
        db.add_all([Store(sid="5174", name="正常店铺", status=1, has_ads_setting=1,
                          timezone="America/Los_Angeles", currency_code="USD"),
                    Store(sid="5175", name="授权失败店铺", status=2),
                    Store(sid="999", name="外部店铺", status=1)])
        for dim, entity, values in (("store", "5174", {"orders": 0, "units": 0, "sales": 0}),
                                    ("campaign_sp", "camp", {"spend": 100, "orders": 1, "sales": 20,
                                                             "units": 1, "clicks": 4, "impressions": 800})):
            for name, value in values.items():
                db.add(DailyMetric(store_id="5174", metric_date=DAY, dimension=dim,
                                   entity_id=entity, metric_name=name, value=value,
                                   currency="USD" if name in {"spend", "sales"} else None))
        for dim, count in (("store", 1), ("campaign_sp", 1), ("campaign_sb", 0), ("campaign_sd", 0)):
            db.add(DataCoverage(store_id="5174", metric_date=DAY, dimension=dim, status="COMPLETE", row_count=count))
    verified = []
    monkeypatch.setattr(admin_mapping, "_members", lambda s, chat: verified.append(chat) or {"ou_new": "新负责人"})
    provider = SimpleNamespace(issuer_url=ORIGIN + "/", database_path=tmp_path / "oauth.sqlite",
                               _password_valid=lambda supplied: supplied == PASSWORD)
    app = OwnerConsole(settings, provider, session_factory=sessions)
    with TestClient(Starlette(routes=app.routes()), base_url=ORIGIN, follow_redirects=False) as client:
        yield client, routing, verified, settings
    engine.dispose()


def login(client, *, origin=ORIGIN):
    page = client.get("/console/login")
    assert page.status_code == 200
    csrf = re.search(r'name="csrf"\s+value="([A-Za-z0-9_-]+)"', page.text)[1]
    return client.post("/console/login", data={"password": PASSWORD, "csrf": csrf}, headers={"Origin": origin})


def test_private_routes_scope_and_verified_save(console):
    client, routing, verified, _ = console
    assert client.get("/console/").headers["location"] == "/console/login"
    assert client.get("/console/api/stores").status_code == 401
    assert login(client, origin="https://elsewhere.example").status_code == 403
    res = login(client)
    assert res.status_code == 303
    assert all(term in res.headers["set-cookie"].lower() for term in ("httponly", "secure", "samesite=strict"))
    assert client.get("/console/").status_code == 200
    payload = client.get("/console/api/stores").json()
    assert payload["totals"] == {"stores": 2, "authorized": 1, "mapped": 1, "mapping_unknown": 0}
    assert client.get("/console/api/stores/999").status_code == 404
    revision = client.get("/console/api/stores/5174/members").json()["revision"]
    submitted = {"open_ids": ["ou_new"], "revision": revision}
    path = "/console/api/stores/5174/owners"
    original = routing.read_bytes()
    assert client.post(path, json=submitted, headers={"Origin": ORIGIN}).status_code == 403
    assert routing.read_bytes() == original
    headers = {"Origin": ORIGIN, "X-CSRF-Token": payload["csrf"]}
    saved = client.post(path, json=submitted, headers=headers)
    assert saved.is_success and saved.json()["owners"] == [{"id": "ou_new", "name": "新负责人"}]
    assert client.post(path, json=submitted, headers=headers).status_code == 409
    assert verified == ["oc_test", "oc_test"]
    assert client.post("/console/logout", headers=headers).status_code == 303
    assert client.get("/console/api/stores").status_code == 401
    assert SESSION not in client.cookies


def test_dashboard_keeps_zero_and_empty_separate_from_missing(console):
    client, _, _, _ = console
    assert login(client).is_redirect
    response = client.get("/console/api/stores/5174", params={"date": DAY.isoformat()})
    assert response.status_code == 200
    data = response.json()
    assert data["metrics"] == {"orders": 0, "units": 0, "sales": 0, "spend": 100, "ad_orders": 1}
    assert data["sales_data_status"] == "ZERO"
    assert data["currency_codes"] == {"sales": "USD", "spend": "USD"}
    assert data["ad_breakdown"][1]["status"] == "接口无记录"
    assert data["ad_breakdown"][1]["spend"] is None
    assert data["trend"][0]["spend"] is None and data["trend"][-1]["spend"] == 100
    assert client.get("/console/api/stores/5174?date=9999-01-01").status_code == 400


def _enable_console_collection(console, monkeypatch):
    import jx_alert.config as config
    settings = console[3]
    settings.app_dry_run = False
    settings.scheduler_enabled = True
    settings.mcp_operations_enabled = True
    monkeypatch.setattr(config, 'get_settings', lambda: settings)
    return settings


def test_manual_latest_collection_requires_owner_csrf_and_exact_scoped_store(console, monkeypatch):
    client, routing, _, _ = console
    _enable_console_collection(console, monkeypatch)
    path = '/console/api/stores/5174/collection'
    assert client.get(path).status_code == 401 and client.post(path, json={}).status_code == 401
    assert login(client).is_redirect
    csrf = client.get('/console/api/stores').json()['csrf']
    headers = {'Origin': ORIGIN, 'X-CSRF-Token': csrf}
    assert client.post(path, json={}, headers={'Origin': ORIGIN}).status_code == 403
    assert client.post(path, json={}, headers={**headers, 'Origin': 'https://wrong.example'}).status_code == 403
    assert client.post(path, json={'report_date': '9999-01-01'}, headers=headers).status_code == 400
    assert client.post('/console/api/stores/999/collection', json={}, headers=headers).status_code == 404
    first = client.post(path, json={}, headers=headers)
    assert first.status_code == 200 and first.json()['status'] == 'PENDING' and not first.json()['reused']
    again = client.post(path, json={}, headers=headers)
    assert again.json()['job_id'] == first.json()['job_id'] and again.json()['reused']
    engine = create_engine(f"sqlite:///{routing.parent / 'warehouse.sqlite'}")
    try:
        with sessionmaker(engine)() as db:
            jobs = list(db.scalars(select(SyncJob).where(SyncJob.job_type == 'manual_collection')))
            assert len(jobs) == 1
            assert jobs[0].metadata_json['store_ids'] == ['5174']
            assert jobs[0].metadata_json['report_date'] is None and jobs[0].metadata_json['backfill_days'] == 2
    finally:
        engine.dispose()


def test_manual_sync_preserves_pause_authorization_and_service_gates(console, monkeypatch):
    from jx_alert.store_controls import get_store_controls, save_store_controls
    client, _, _, settings = console
    assert login(client).is_redirect
    headers = {'Origin': ORIGIN, 'X-CSRF-Token': client.get('/console/api/stores').json()['csrf']}
    path = '/console/api/stores/5174/collection'
    assert client.post(path, json={}, headers=headers).status_code == 409
    _enable_console_collection(console, monkeypatch)
    save_store_controls(settings, '5174', False, True, get_store_controls(settings, '5174')['revision'])
    paused = client.post(path, json={}, headers=headers)
    assert paused.status_code == 409 and '采集已关闭' in paused.json()['error']
    unauthorized = client.post('/console/api/stores/5175/collection', json={}, headers=headers)
    assert unauthorized.status_code == 409 and '授权失败' in unauthorized.json()['error']


def test_bulk_collection_is_owner_only_and_requires_same_origin_csrf_and_empty_body(console, monkeypatch):
    client, _, _, _ = console
    _enable_console_collection(console, monkeypatch)
    path = '/console/api/collection/all'
    assert client.get(path).status_code == 401 and client.post(path, json={}).status_code == 401
    assert login(client).is_redirect
    headers = {'Origin': ORIGIN, 'X-CSRF-Token': client.get('/console/api/stores').json()['csrf']}
    assert client.post(path, json={}, headers={'Origin': ORIGIN}).status_code == 403
    assert client.post(path, json={}, headers={**headers, 'Origin': 'https://wrong.example'}).status_code == 403
    assert client.post(path, json={'store_ids': ['999']}, headers=headers).status_code == 400
    result = client.post(path, json={}, headers=headers)
    assert result.status_code == 200 and result.json()['selected_store_count'] == 1
    assert result.json()['skipped_reason_counts'] == {'授权失败': 1}
    reused = client.post(path, json={}, headers=headers)
    assert reused.json()['job_id'] == result.json()['job_id'] and reused.json()['reused']
    assert client.post(path, json={}, headers=headers).status_code == 429
    assert client.get(path).json()['job_id'] == result.json()['job_id']
    assert 'id="sync-all"' in client.get('/console/').text


def test_manual_sync_status_is_scoped_and_never_exposes_raw_job_metadata(console):
    client, routing, _, _ = console
    engine = create_engine(f"sqlite:///{routing.parent / 'warehouse.sqlite'}")
    try:
        with sessionmaker(engine).begin() as db:
            db.add(SyncJob(job_type='manual_collection', status='PENDING', metadata_json={
                'store_ids': ['5174'], 'retry_at': '2026-11-16T09:00:00', 'private_marker': 'must-not-leak',
                'planned_store_dates': {'5174': '2026-11-14'}, 'result': {'raw_payload': 'must-not-leak'}}))
            db.add(SyncJob(job_type='manual_collection', status='FAILED', metadata_json={
                'store_ids': ['5175'], 'error': 'outside-selected-store'}))
        assert login(client).is_redirect
        response = client.get('/console/api/stores/5174/collection')
        data = response.json()
        assert response.status_code == 200 and data['status_label'] == '等待重试'
        assert data['report_date'] == '2026-11-14' and data['retry_at'] == '2026-11-16T09:00:00'
        assert set(data) == {'job_id', 'status', 'status_label', 'report_date', 'retry_at'}
        assert 'must-not-leak' not in response.text and 'outside-selected-store' not in response.text
        assert client.get('/console/api/stores/999/collection').status_code == 404
    finally:
        engine.dispose()


def test_dashboard_reads_legacy_empty_sales_from_partition_evidence(console):
    client, routing, _, _ = console
    empty_day = date(2024, 10, 11)
    engine = create_engine(f"sqlite:///{routing.parent / 'warehouse.sqlite'}")
    try:
        with sessionmaker(engine).begin() as db:
            db.add_all(DataCoverage(store_id="5174", metric_date=empty_day, dimension=dim,
                       status="COMPLETE", row_count=count)
                       for dim, count in (("store", 1), ("sku_asin", 0), ("sku_msku", 0)))
        assert login(client).is_redirect
        data = client.get("/console/api/stores/5174", params={"date": empty_day.isoformat()}).json()
        assert data["metrics"]["orders"] is None
        assert data["sales_data_status"] == "EMPTY"
        assert data["coverage"][0] == {"name": "店铺销量", "status": "接口无记录", "rows": 0}
        assert data["coverage"][1]["status"] == "尚未采集"
        with sessionmaker(engine)() as db:
            row = db.scalar(select(DataCoverage).where(DataCoverage.dimension == "store",
                            DataCoverage.metric_date == empty_day))
            assert row.row_count == 1
    finally:
        engine.dispose()


def test_mapping_configuration_failure_leaves_store_data_accessible(console):
    client, routing, _, _ = console
    assert login(client).is_redirect
    routing.write_text("invalid configuration", encoding="utf-8")
    result = client.get("/console/api/stores").json()
    assert result["totals"]["mapping_unknown"] == 2
    result = client.get("/console/api/stores/5174?date=" + DAY.isoformat()).json()
    assert result["metrics"]["orders"] == 0 and result["routing"]["error"]
    assert client.get("/console/api/stores/5174/members").status_code == 503


def test_https_host_body_limits_and_session_expiry(console):
    client, routing, _, _ = console
    assert client.get("/console/login", headers={"Host": "other.example.com"}).status_code == 400
    assert client.post("/console/login", content="x" * 8193, headers={
        "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}).status_code == 413
    assert login(client).is_redirect
    with sqlite3.connect(routing.parent / "console-sessions.sqlite3") as db:
        db.execute("UPDATE sessions SET expires=0")
    assert client.get("/console/api/stores").status_code == 401
    assert client.get("/console/").headers["location"] == "/console/login"


def test_failed_recollection_keeps_historical_values_from_looking_current(console):
    client, routing, _, _ = console
    engine = create_engine(f"sqlite:///{routing.parent / 'warehouse.sqlite'}")
    try:
        with sessionmaker(engine).begin() as db:
            db.add(SyncJob(job_type="daily_collection", status="FAILED",
                           metadata_json={"report_dates": {"5174": DAY.isoformat()}}))
    finally:
        engine.dispose()
    assert login(client).is_redirect
    response = client.get("/console/api/stores/5174?date=" + DAY.isoformat())
    assert response.status_code == 200
    data = response.json()
    assert all(value is None for value in data["metrics"].values())
    assert data["data_warning"] and all(row["status"] == "采集失败待核实" for row in data["coverage"])


def test_default_date_does_not_fall_back_over_latest_failed_collection(console):
    _, routing, _, settings = console
    engine = create_engine(f"sqlite:///{routing.parent / 'warehouse.sqlite'}")
    try:
        with sessionmaker(engine).begin() as db:
            db.add(SyncJob(job_type="daily_collection", status="FAILED", metadata_json={
                "report_dates": {"5174": "2024-10-13"}}))
        with sessionmaker(engine)() as db:
            value = admin_data.store_detail(db, settings, db.get(Store, "5174"),
                                           now=datetime(2024, 10, 14, 14, tzinfo=timezone.utc))
            assert value["report_date"] == "2024-10-13" and value["data_warning"]
            assert all(v is None for v in value["metrics"].values())
    finally:
        engine.dispose()


def test_store_controls_are_independent_persistent_and_private(console):
    client, _, _, settings = console
    path = "/console/api/stores/5174/controls"
    assert client.post(path, json={}).status_code == 401
    assert login(client).is_redirect
    overview = client.get("/console/api/stores").json()
    initial = next(row["controls"] for row in overview["stores"] if row["sid"] == "5174")
    assert initial["collection_enabled"] is True and initial["notifications_enabled"] is True
    change = {"collection_enabled": False, "notifications_enabled": True, "revision": initial["revision"]}
    assert client.post(path, json=change, headers={"Origin": ORIGIN}).status_code == 403
    headers = {"Origin": ORIGIN, "X-CSRF-Token": overview["csrf"]}
    assert client.post("/console/api/stores/999/controls", json=change, headers=headers).status_code == 404
    result = client.post(path, json=change, headers=headers)
    assert result.status_code == 200 and result.json()["collection_enabled"] is False
    assert result.json()["notifications_enabled"] is True
    assert client.post(path, json=change, headers=headers).status_code == 409
    change.update(collection_enabled=True, notifications_enabled=False, revision=result.json()["revision"])
    result = client.post(path, json=change, headers=headers)
    assert result.status_code == 200 and result.json()["collection_enabled"] is True
    assert result.json()["notifications_enabled"] is False
    assert client.get("/console/api/stores/5174?date=" + DAY.isoformat()).json()["metrics"]["spend"] == 100
    controls = json.loads(Path(settings.store_controls_file).read_text(encoding="utf-8"))
    assert controls["stores"]["5174"]["notifications_enabled"] is False


def test_invalid_controls_keep_history_visible_but_never_allow_saving(console):
    client, _, _, settings = console
    Path(settings.store_controls_file).write_text('{"stores": "corrupt"}', encoding="utf-8")
    assert login(client).is_redirect
    overview = client.get("/console/api/stores").json()
    assert all(row["controls"]["collection_enabled"] is None for row in overview["stores"])
    detail = client.get("/console/api/stores/5174?date=" + DAY.isoformat()).json()
    assert detail["controls"]["error"] and detail["metrics"]["spend"] == 100
    headers = {"Origin": ORIGIN, "X-CSRF-Token": overview["csrf"]}
    assert client.post("/console/api/stores/5174/controls", headers=headers, json={
        "collection_enabled": True, "notifications_enabled": True, "revision": "0" * 64}).status_code == 503


def test_notification_group_is_owner_only_and_requires_preview_confirmation_and_csrf(console, monkeypatch):
    client, routing, _, _ = console
    endpoint = "/console/api/notification-group"
    monkeypatch.setattr(admin_groups, "_joined_chats", lambda *_, **kw: {"oc_new_group": "新通知群"})
    assert client.get(endpoint).status_code == 401
    assert client.post(endpoint, json={}).status_code == 401
    assert login(client).is_redirect
    current = client.get(endpoint).json()
    assert current["chat_id"] == "oc_test" and current["name"] == "测试群"
    assert client.get(endpoint + "/chats").json()["chats"] == [{"id": "oc_new_group", "name": "新通知群"}]
    original = json.loads(routing.read_text(encoding="utf-8"))
    csrf = client.get("/console/api/stores").json()["csrf"]
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    selected = {"chat_id": "oc_new_group", "revision": current["revision"]}
    assert client.post(endpoint + "/preview", json=selected, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post(endpoint, json={**selected, "confirmed": True}, headers={
        **headers, "Origin": "https://other.example"}).status_code == 403
    assert client.post(endpoint + "/preview", json=selected, headers=headers).json()["changed"] is True
    assert client.post(endpoint, json={**selected, "confirmed": False}, headers=headers).status_code == 400
    assert json.loads(routing.read_text(encoding="utf-8")) == original
    result = client.post(endpoint, json={**selected, "confirmed": True}, headers=headers)
    assert result.status_code == 200 and result.json()["name"] == "新通知群"
    changed = json.loads(routing.read_text(encoding="utf-8"))
    assert changed["stores"] == original["stores"] and changed["groups"]["ops"]["chat_id"] == "oc_new_group"
    assert client.post(endpoint, json={**selected, "confirmed": True}, headers=headers).status_code == 409
    page = client.get("/console/").text
    assert 'data-view="notification-group"' in page and 'id="confirm-group-change"' in page


def test_notification_group_preview_has_separate_bounded_network_budget(console, monkeypatch):
    client, _, _, _ = console
    monkeypatch.setattr(admin_groups, "_joined_chats", lambda *_, **kw: {"oc_new_group": "新通知群"})
    assert login(client).is_redirect
    csrf = client.get("/console/api/stores").json()["csrf"]
    current = client.get("/console/api/notification-group").json()
    for _ in range(8):
        result = client.post("/console/api/notification-group/preview", json={
            "chat_id": "oc_new_group", "revision": current["revision"]},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
        assert result.status_code == 200
    assert client.post("/console/api/notification-group/preview", json={
        "chat_id": "oc_new_group", "revision": current["revision"]},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 429
