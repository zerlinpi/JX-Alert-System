"""Group questions use scoped persisted facts, durable replies, and no MCP calls."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert import bot_service, config
from jx_alert.config import Settings
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.storage.db import Base
from jx_alert.storage.models import Campaign, DailyMetric, DataCoverage, Store, StorePerformance, SyncJob


NOW = datetime(2026, 11, 14, 0, tzinfo=timezone.utc)
DAY = date(2026, 11, 12)
HEADER_TOKEN = "private-event-header-token"
ANALYSIS = "广告原始数据已列示；绩效源时区待确认，不能判断当前店铺健康。"


class FakeClient:
    calls = []

    @classmethod
    def from_settings(cls, settings):
        return cls()

    def analyze(self, context, question):
        self.calls.append((deepcopy(context), question))
        return ANALYSIS


def event(question="@_user_1 5174 店铺广告和绩效情况", *, message_id="om_question"):
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1", "app_id": "cli_test", "token": HEADER_TOKEN},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_member", "union_id": "private-union"}},
            "message": {"message_id": message_id, "chat_id": "oc_reports", "chat_type": "group",
                "message_type": "text", "content": json.dumps({"text": question}),
                "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}]},
        },
    }


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    scope, zones, routing = (tmp_path / name for name in ("scope.json", "zones.json", "routing.json"))
    scope.write_text(json.dumps({"store_ids": [5174, 5175]}))
    zones.write_text(json.dumps({"stores": {
        "5174": {"timezone": "America/Los_Angeles"},
        "5175": {"timezone": "America/Los_Angeles"},
    }}))
    routing.write_text(json.dumps({"default_chat_id": "reports", "groups": {
        "reports": {"chat_id": "oc_reports"}}}))
    settings = Settings(_env_file=None, app_dry_run=False, feishu_enabled=True, feishu_bot_enabled=True,
        feishu_bot_open_id="ou_bot", feishu_app_id="cli_test", feishu_transport="app",
        feishu_daily_report_chat_id="reports", feishu_routing_file=str(routing),
        collection_scope_file=str(scope), store_timezones_file=str(zones),
        store_controls_file=str(tmp_path / "controls.json"),
        deepseek_credentials_file=str(tmp_path / "not-configured.json"))
    engine = create_engine("sqlite:///" + str(tmp_path / "warehouse.sqlite"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as db:
        db.add_all([Store(sid=sid, name="同创美国", status=1, timezone="America/Los_Angeles", currency_code="USD")
                    for sid in ("5174", "5175")])
        db.add(Store(sid="9999", name="范围外店铺", status=1, timezone="America/Los_Angeles"))
        for kind in ("sp", "sb", "sd"):
            db.add(Campaign(store_id="5174", campaign_type=kind, campaign_id=kind + "-campaign",
                            name="测试活动", state="enabled"))
        for offset in range(22):
            on = DAY - timedelta(days=offset)
            for dimension in ("store", "campaign_sp", "campaign_sb", "campaign_sd"):
                db.add(DataCoverage(store_id="5174", metric_date=on, dimension=dimension,
                                    status="COMPLETE", row_count=1))
                metrics = {"orders": 10, "units": 12, "sales": 2000} if dimension == "store" else {
                    "spend": 100, "orders": 2, "units": 3, "sales": 500, "clicks": 20, "impressions": 2000}
                entity_id = "5174" if dimension == "store" else dimension.removeprefix("campaign_") + "-campaign"
                for metric, value in metrics.items():
                    db.add(DailyMetric(store_id="5174", metric_date=on, dimension=dimension, entity_id=entity_id,
                        metric_name=metric, value=value, currency="USD" if metric in {"sales", "spend"} else None))
        db.add(StorePerformance(store_id="5174", pull_date=DAY, ahr_status="Healthy", ahr_score=220,
            captured_at=NOW.replace(tzinfo=None), source_updated_at=NOW.replace(tzinfo=None).isoformat(),
            freshness_status="NOT_CONFIGURED", indicators={"order_with_defect": "0.00"},
            standards={"orderWithDefect": "1"}))
        db.commit()
    FakeClient.calls = []
    monkeypatch.setattr(bot_service, "_utcnow", lambda: NOW)

    def forbidden_network(*args, **kwargs):
        pytest.fail("Persisted-data group questions must not call MCP or a provider implicitly")

    monkeypatch.setattr(httpx, "Client", forbidden_network)
    yield settings, sessions, engine
    engine.dispose()


def enqueue(warehouse, incoming=None):
    settings, sessions, _ = warehouse
    with sessions() as db:
        result = bot_service.enqueue_group_question(db, settings, incoming or event())
        db.commit()
        return result


def process(warehouse, *, factory=FakeClient.from_settings):
    settings, sessions, engine = warehouse
    return bot_service.process_one_question(settings, sessions, engine, client_factory=factory)


def _enable_collection(warehouse, monkeypatch, *, role="operator"):
    settings = warehouse[0]
    settings.mcp_operations_enabled = settings.scheduler_enabled = True
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    routing = {"default_chat_id": "reports", "groups": {"reports": {"chat_id": "oc_reports"}}}
    if role == "operator":
        routing["stores"] = {"5174": {"open_ids": ["ou_member"]}}
    elif role == "admin":
        routing["groups"]["reports"]["admin_open_ids"] = ["ou_member"]
    Path(settings.feishu_routing_file).write_text(json.dumps(routing), encoding="utf-8")


def _collection_receipt(warehouse, result):
    with warehouse[1]() as db:
        value = db.get(NotificationOutbox, result["outbox_ids"][0])
        return json.dumps(value.payload["card"], ensure_ascii=False)


@pytest.mark.parametrize("role", ["operator", "admin"])
def test_explicit_single_shop_collection_queues_one_scoped_job(warehouse, monkeypatch, role):
    _enable_collection(warehouse, monkeypatch, role=role)
    request = event("@_user_1 请实时采集 5174 店铺。")
    queued = enqueue(warehouse, request)
    answered = process(warehouse)
    assert enqueue(warehouse, request) == {"status": "REUSED", "job_id": queued["job_id"]}
    assert process(warehouse)["status"] == "IDLE"
    assert "等待采集" in _collection_receipt(warehouse, answered)
    assert FakeClient.calls == []
    with warehouse[1]() as db:
        tasks = db.scalars(select(SyncJob).where(SyncJob.job_type == "manual_collection")).all()
        assert len(tasks) == 1
        job = tasks[0]
        receipt = db.get(SyncJob, queued["job_id"]).metadata_json
        assert receipt["collection_job_id"] == job.id
        assert receipt["collection_store_id"] == "5174"
        assert job.metadata_json["store_ids"] == ["5174"]
        assert job.metadata_json["report_date"] is None
        assert job.status == "PENDING"


def test_distinct_requests_share_active_job_and_can_refresh_again_after_completion(warehouse, monkeypatch):
    _enable_collection(warehouse, monkeypatch)
    first = enqueue(warehouse, event("@_user_1 实时采集5174"))
    process(warehouse)
    second = enqueue(warehouse, event("@_user_1 重新采集5174", message_id="om_second"))
    process(warehouse)
    with warehouse[1].begin() as db:
        tasks = db.scalars(select(SyncJob).where(SyncJob.job_type == "manual_collection")).all()
        assert len(tasks) == 1
        first_id = tasks[0].id
        assert db.get(SyncJob, first["job_id"]).metadata_json["collection_job_id"] == first_id
        assert db.get(SyncJob, second["job_id"]).metadata_json["collection_job_id"] == first_id
        tasks[0].status = "SUCCEEDED"
    third = enqueue(warehouse, event("@_user_1 立即采集5174", message_id="om_third"))
    process(warehouse)
    with warehouse[1]() as db:
        assert db.scalar(select(func.count(SyncJob.id)).where(SyncJob.job_type == "manual_collection")) == 2
        assert db.get(SyncJob, third["job_id"]).metadata_json["collection_job_id"] != first_id


@pytest.mark.parametrize("role,phrase,label", [
    ("other", "立即采集5174", "请由该店铺负责人或群管理员"),
    ("operator", "重新采集 5174 和 5175", "请确认要采集的店铺"),
    ("operator", "实时采集 5175", "请由该店铺负责人或群管理员"),
    ("admin", "现在采集 9999", "店铺不在采集范围内"),
])
def test_collection_requires_one_explicit_scoped_store_and_role(warehouse, monkeypatch, role, phrase, label):
    _enable_collection(warehouse, monkeypatch, role=role)
    enqueue(warehouse, event("@_user_1 " + phrase))
    assert label in _collection_receipt(warehouse, process(warehouse))
    assert FakeClient.calls == []
    with warehouse[1]() as db:
        assert db.scalar(select(SyncJob).where(SyncJob.job_type == "manual_collection")) is None


@pytest.mark.parametrize("condition,label", [
    ("paused", "该店铺的采集开关已关闭"),
    ("authorization", "请先恢复该店铺的领星授权"),
    ("scheduler", "主动采集或调度服务未启用"),
    ("operations", "主动采集或调度服务未启用"),
])
def test_collection_obeys_existing_operational_guards(warehouse, monkeypatch, condition, label):
    _enable_collection(warehouse, monkeypatch)
    settings, sessions, _ = warehouse
    if condition == "paused":
        from jx_alert.store_controls import get_store_controls, save_store_controls
        current = get_store_controls(settings, "5174")
        save_store_controls(settings, "5174", False, True, current["revision"])
    elif condition == "authorization":
        with sessions.begin() as db:
            db.get(Store, "5174").status = 2
    elif condition == "scheduler":
        settings.scheduler_enabled = False
    else:
        settings.mcp_operations_enabled = False
    enqueue(warehouse, event("@_user_1 实时采集5174"))
    assert label in _collection_receipt(warehouse, process(warehouse))
    assert FakeClient.calls == []
    with sessions() as db:
        assert db.scalar(select(SyncJob).where(SyncJob.job_type == "manual_collection")) is None


def test_collection_query_only_shows_tasks_receipted_in_group_with_current_permission(warehouse, monkeypatch):
    _enable_collection(warehouse, monkeypatch)
    queued = enqueue(warehouse, event("@_user_1 5174 店铺立即采集"))
    process(warehouse)
    with warehouse[1].begin() as db:
        collection_id = db.get(SyncJob, queued["job_id"]).metadata_json["collection_job_id"]
        job = db.get(SyncJob, collection_id)
        job.metadata_json = {**job.metadata_json, "retry_at": "2026-11-14T00:03:00"}
        outside = SyncJob(job_type="manual_collection", status="SUCCEEDED",
                          metadata_json={"store_ids": ["5174"]})
        db.add(outside)
        db.flush()
        outside_id = outside.id
    enqueue(warehouse, event(f"@_user_1 采集任务 {collection_id} 状态", message_id="om_check"))
    assert "等待重试，最早北京时间 11-14 08:03:00" in _collection_receipt(warehouse, process(warehouse))
    enqueue(warehouse, event(f"@_user_1 采集任务 {outside_id} 状态", message_id="om_outside"))
    assert "未找到可查询的采集任务" in _collection_receipt(warehouse, process(warehouse))
    _enable_collection(warehouse, monkeypatch, role="other")
    blocked = event(f"@_user_1 采集任务 {collection_id} 状态", message_id="om_denied")
    blocked["event"]["sender"]["sender_id"]["open_id"] = "ou_second"
    enqueue(warehouse, blocked)
    assert "未找到可查询的采集任务" in _collection_receipt(warehouse, process(warehouse))
    assert FakeClient.calls == []


def test_ordinary_question_and_model_output_cannot_start_collection(warehouse, monkeypatch):
    _enable_collection(warehouse, monkeypatch)
    request = "不要重新采集 5174，只查看已有数据"

    class AnalysisWithActionText(FakeClient):
        def analyze(self, context, question):
            super().analyze(context, question)
            return "实时采集 5174"

    enqueue(warehouse, event("@_user_1 " + request))
    process(warehouse, factory=AnalysisWithActionText.from_settings)
    assert FakeClient.calls[0][1] == request
    with warehouse[1]() as db:
        assert db.scalar(select(SyncJob).where(SyncJob.job_type == "manual_collection")) is None


def test_store_country_disambiguates_same_name_without_guessing(warehouse):
    settings, sessions, _ = warehouse
    with sessions() as db:
        for sid, country in [("5174", "美国"), ("5175", "加拿大")]:
            store = db.get(Store, sid)
            store.name, store.country = "Campx", country
        db.commit()
        _, matched = bot_service._resolve_store(db, settings, "Campx美国广告情况")
        assert matched == ["5174"]
        candidates, ambiguous = bot_service._resolve_store(db, settings, "Campx广告情况")
        assert set(ambiguous) == {"5174", "5175"}
        assert {r["country"] for r in candidates} == {"美国", "加拿大"}


def _name_stores(warehouse, *, same_brand=False):
    with warehouse[1].begin() as db:
        first, second = db.get(Store, "5174"), db.get(Store, "5175")
        first.name, first.country = "梵界-Geteen-主账号-US", "美国"
        second.name = "梵界-Geteen-主账号-CA" if same_brand else "耀强-Campx-主账号-CA"
        second.country = "加拿大"


@pytest.mark.parametrize("query", [
    "Geteen", "geteen 美国广告情况", "ＧＥＴＥＥＮ 美国", "梵界的情况", "Get 美国", "梵界 Geteen 主账号 US",
])
def test_store_name_and_brand_fragments_resolve_without_id(warehouse, query):
    _name_stores(warehouse)
    with warehouse[1]() as db:
        assert bot_service._resolve_store(db, warehouse[0], query)[1] == ["5174"]


@pytest.mark.parametrize("query", ["US", "美国店铺广告情况", "主账号", "amz", "你好", "范围外店铺", "不存在品牌"])
def test_generic_country_or_out_of_scope_names_do_not_select_a_store(warehouse, query):
    _name_stores(warehouse)
    with warehouse[1]() as db:
        assert bot_service._resolve_store(db, warehouse[0], query)[1] == []


@pytest.mark.parametrize("query,expected", [
    ("Geteen", ["5174", "5175"]), ("Geteen 美国", ["5174"]),
    ("Geteen CA", ["5175"]), ("Geteen 德国", []),
])
def test_partial_brand_uses_real_country_for_disambiguation(warehouse, query, expected):
    _name_stores(warehouse, same_brand=True)
    with warehouse[1]() as db:
        assert bot_service._resolve_store(db, warehouse[0], query)[1] == expected


def test_complete_name_wins_over_shared_chinese_prefix_and_embedded_us_token(warehouse):
    with warehouse[1].begin() as db:
        a, b = db.get(Store, "5174"), db.get(Store, "5175")
        a.name, a.country = "AMZ-义乌昌泉-主账号-US-BR", "巴西"
        b.name, b.country = "AMZ-义乌昌泉-主账号-US", "美国"
        # The full Brazil name contains US as an internal account label.
        assert bot_service._resolve_store(db, warehouse[0], "AMZ 义乌昌泉 主账号 US BR 广告情况")[1] == ["5174"]
        a.name, b.name = "梵界-Geteen-主账号-US", "梵界-Campx-主账号-US"
        a.country = b.country = "美国"
        assert bot_service._resolve_store(db, warehouse[0], "梵界 Geteen 主账号 US情况")[1] == ["5174"]


def test_report_date_numbers_cannot_select_a_store(warehouse):
    settings = warehouse[0]
    with warehouse[1].begin() as db:
        db.add(Store(sid="2026", name="另一品牌", country="美国", status=1))
    Path(settings.collection_scope_file).write_text(json.dumps({"store_ids": [5174, 5175, 2026]}))
    with warehouse[1]() as db:
        assert bot_service._resolve_store(db, settings, "2026-11-14广告情况")[1] == []


def test_partial_name_question_uses_correct_store_without_starting_collection(warehouse):
    _name_stores(warehouse)
    enqueue(warehouse, event("@_user_1 geteen 美国广告和绩效情况"))
    process(warehouse)
    assert len(FakeClient.calls) == 1 and FakeClient.calls[0][0]["store_id"] == "5174"
    with warehouse[1]() as db:
        assert db.scalar(select(SyncJob.id).where(SyncJob.job_type == "manual_collection")) is None


def test_unknown_full_brand_cannot_fall_back_to_shared_chinese_prefix(warehouse):
    _name_stores(warehouse)
    with warehouse[1]() as db:
        assert bot_service._resolve_store(db, warehouse[0], "梵界-UnknownBrand-主账号-US")[1] == []
        assert bot_service._resolve_store(db, warehouse[0], "梵界-其他品牌-主账号-US")[1] == []
        assert bot_service._resolve_store(db, warehouse[0], "Geteen CPC ROAS情况")[1] == ["5174"]


def test_identical_frozen_question_reuses_model_but_changed_evidence_does_not(warehouse):
    for i in range(2):
        enqueue(warehouse, event(message_id="om_cache_" + str(i)))
        process(warehouse)
    assert len(FakeClient.calls) == 1
    with warehouse[1].begin() as db:
        fact = db.scalar(select(DailyMetric).where(DailyMetric.store_id == "5174",
            DailyMetric.metric_date == DAY, DailyMetric.dimension == "store", DailyMetric.metric_name == "orders"))
        fact.value += 1
    enqueue(warehouse, event(message_id="om_changed"))
    process(warehouse)
    assert len(FakeClient.calls) == 2


def test_compact_prompt_preserves_all_kpis_values_dates_and_gaps(warehouse):
    from jx_alert.ai.deepseek import _input_json, compact_context
    with warehouse[1]() as db:
        context, _ = bot_service.question_context(db, warehouse[0], "5174", now=NOW)
    compact = compact_context(context)
    assert set(compact["advertising_kpis"]) == {"CTR", "CPC", "CDR", "CPS", "ROAS", "CPA", "TCOAS"}
    for key, item in context["advertising_kpis"].items():
        assert compact["advertising_kpis"][key]["current"] == item["current"]
        assert compact["advertising_kpis"][key]["current_date"] == item["current_date"]
        assert compact["advertising_kpis"][key]["status"] == item["status"]
    assert compact["coverage"] == context["coverage"]
    assert compact["health"]["confirmation_reason"] == context["health"]["confirmation_reason"]
    original = json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(_input_json(context, "广告情况").encode()) < len(original) * .75


def test_model_cache_expires_and_does_not_roll_forward_on_cache_hits(warehouse, monkeypatch):
    enqueue(warehouse)
    process(warehouse)
    monkeypatch.setattr(bot_service, "_utcnow", lambda: NOW + timedelta(minutes=9))
    enqueue(warehouse, event(message_id="om_cache_hit"))
    process(warehouse)
    assert len(FakeClient.calls) == 1
    monkeypatch.setattr(bot_service, "_utcnow", lambda: NOW + timedelta(minutes=11))
    enqueue(warehouse, event(message_id="om_cache_expired"))
    process(warehouse)
    assert len(FakeClient.calls) == 2


def test_oversized_model_context_keeps_the_existing_real_data_card_fallback(warehouse, monkeypatch):
    from jx_alert.ai.deepseek import DeepSeekClient, MAX_CONTEXT_BYTES
    original = bot_service.question_context

    def large_context(*args, **kwargs):
        context, data_card = original(*args, **kwargs)
        context["oversized_metadata"] = "x" * MAX_CONTEXT_BYTES
        return context, data_card

    monkeypatch.setattr(bot_service, "question_context", large_context)
    enqueue(warehouse)
    result = process(warehouse, factory=lambda settings: DeepSeekClient("test-only-key"))
    with warehouse[1]() as db:
        job = db.get(SyncJob, result["job_id"])
        assert job.status == "SUCCEEDED" and job.metadata_json["analysis_provider"] == "unavailable"
        assert "analysis_cache_key" not in job.metadata_json
        content = json.dumps([db.get(NotificationOutbox, key).payload["card"] for key in result["outbox_ids"]], ensure_ascii=False)
    assert "智能分析暂不可用" in content and "经营与广告" in content


def test_materiality_policy_applies_to_kpi_context_without_changing_source_tables(warehouse):
    settings, sessions, _ = warehouse
    settings.alert_materiality_enabled = True
    with sessions.begin() as db:
        metric = db.scalar(select(DailyMetric).where(DailyMetric.store_id == "5174",
            DailyMetric.metric_date == DAY, DailyMetric.dimension == "campaign_sp", DailyMetric.metric_name == "spend"))
        metric.value = 200
    with sessions() as db:
        context, _ = bot_service.question_context(db, settings, "5174", now=NOW)
        assert context["advertising_kpis"]["CPC"]["status"] == "WATCH"
        assert context["notification_policy"]["version"] == 1
        assert not any(row["metric"] == "CPC" for row in context["screening_candidates"])


def test_ambiguous_collection_name_returns_names_and_sites_without_queueing(warehouse, monkeypatch):
    _name_stores(warehouse, same_brand=True)
    _enable_collection(warehouse, monkeypatch, role="admin")
    enqueue(warehouse, event("@_user_1 实时采集Geteen"))
    reply = _collection_receipt(warehouse, process(warehouse))
    assert all(label in reply for label in ("请确认要采集的店铺", "梵界-Geteen-主账号-US", "加拿大", "完整店铺名和站点"))
    assert "店铺ID" not in reply and "5174" not in reply
    with warehouse[1]() as db:
        assert db.scalar(select(SyncJob.id).where(SyncJob.job_type == "manual_collection")) is None
    assert not FakeClient.calls


@pytest.mark.parametrize("role", ["operator", "admin"])
def test_named_collection_and_named_progress_keep_existing_receipts_and_deduplication(warehouse, monkeypatch, role):
    _name_stores(warehouse)
    _enable_collection(warehouse, monkeypatch, role=role)
    request = event("@_user_1 实时采集 geteen 美国")
    first = enqueue(warehouse, request)
    reply = _collection_receipt(warehouse, process(warehouse))
    assert "梵界-Geteen-主账号-US" in reply and "等待采集" in reply
    assert "店铺ID" not in reply and "任务号" not in reply
    assert enqueue(warehouse, request)["status"] == "REUSED"
    enqueue(warehouse, event("@_user_1 重新采集 梵界 美国", message_id="om_again"))
    process(warehouse)
    with warehouse[1].begin() as db:
        jobs = db.scalars(select(SyncJob).where(SyncJob.job_type == "manual_collection")).all()
        assert len(jobs) == 1 and jobs[0].metadata_json["store_ids"] == ["5174"]
        jobs[0].metadata_json = {**jobs[0].metadata_json, "retry_at": "2026-11-14T00:03:00"}
        assert db.get(SyncJob, first["job_id"]).metadata_json["collection_store_id"] == "5174"
    enqueue(warehouse, event("@_user_1 查询Geteen美国采集状态", message_id="om_progress"))
    reply = _collection_receipt(warehouse, process(warehouse))
    assert "等待重试，最早北京时间 11-14 08:03:00" in reply and "梵界-Geteen-主账号-US" in reply
    assert not FakeClient.calls


@pytest.mark.parametrize("query", [
    "Geteen 暂时不要实时采集", "Geteen 是否可以实时采集", "请说明Geteen实时采集",
    "实时采集Geteen和未知店铺", "实时采集Geteen以及Campx", "实时采集Geteen，不要实际运行",
    "实时采集Geteen是什么意思", "实时采集Geteen吗",
])
def test_negation_description_and_multi_store_requests_never_queue_a_partial_match(warehouse, monkeypatch, query):
    _name_stores(warehouse)
    _enable_collection(warehouse, monkeypatch, role="admin")
    enqueue(warehouse, event("@_user_1 " + query))
    process(warehouse)
    with warehouse[1]() as db:
        assert db.scalar(select(SyncJob.id).where(SyncJob.job_type == "manual_collection")) is None


@pytest.mark.parametrize("condition", ["no_role", "paused", "authorization"])
def test_named_collection_cannot_bypass_role_switch_or_authorization(warehouse, monkeypatch, condition):
    _name_stores(warehouse)
    _enable_collection(warehouse, monkeypatch, role="other" if condition == "no_role" else "operator")
    if condition == "paused":
        from jx_alert.store_controls import get_store_controls, save_store_controls
        current = get_store_controls(warehouse[0], "5174")
        save_store_controls(warehouse[0], "5174", False, True, current["revision"])
    elif condition == "authorization":
        with warehouse[1].begin() as db:
            db.get(Store, "5174").status = 2
    enqueue(warehouse, event("@_user_1 实时采集Geteen美国"))
    process(warehouse)
    with warehouse[1]() as db:
        assert db.scalar(select(SyncJob.id).where(SyncJob.job_type == "manual_collection")) is None
    assert not FakeClient.calls


@pytest.mark.parametrize("condition", ["other_group", "unreceipted", "permission_revoked"])
def test_named_progress_rechecks_group_receipt_and_current_permission(warehouse, monkeypatch, condition):
    _name_stores(warehouse)
    _enable_collection(warehouse, monkeypatch)
    queued = enqueue(warehouse, event("@_user_1 实时采集Geteen美国"))
    process(warehouse)
    with warehouse[1].begin() as db:
        receipt = db.get(SyncJob, queued["job_id"])
        original = db.get(SyncJob, receipt.metadata_json["collection_job_id"])
        original.status = "SUCCEEDED"
        if condition == "other_group":
            receipt.metadata_json = {**receipt.metadata_json, "chat_id": "oc_other"}
        elif condition == "unreceipted":
            receipt.metadata_json = {**receipt.metadata_json, "analysis_provider": "deepseek"}
        else:
            _enable_collection(warehouse, monkeypatch, role="other")
    enqueue(warehouse, event("@_user_1 Geteen美国采集进度", message_id="om_progress"))
    reply = _collection_receipt(warehouse, process(warehouse))
    assert "未找到可查询的采集任务" in reply and "采集完成" not in reply
    assert not FakeClient.calls


def test_model_context_lists_adverse_evidence_explicitly_for_review(warehouse):
    settings, sessions, _ = warehouse
    with sessions() as db:
        metric = db.scalar(select(DailyMetric).where(DailyMetric.store_id == "5174",
            DailyMetric.metric_date == DAY, DailyMetric.dimension == "campaign_sp", DailyMetric.metric_name == "spend"))
        metric.value = 200
        db.commit()
        context, _ = bot_service.question_context(db, settings, "5174", now=NOW)
        assert context["screening_candidate_count"] == len(context["screening_candidates"]) > 0
        candidate = next(c for c in context["screening_candidates"] if c["metric"] == "CPC" and c["entity_id"] == "sp-campaign")
        assert candidate["current"] == 10 and candidate["baseline"] == 5


def test_model_context_cannot_confuse_previous_day_values_with_missing_current_day(warehouse):
    settings, sessions, _ = warehouse
    with sessions() as db:
        context, _ = bot_service.question_context(db, settings, "5174", now=NOW + timedelta(days=1),
                                                 question="2026-11-13 店铺情况")
        orders = next(row for row in context["sales_and_spend"] if row["metric"] == "订单")
        assert orders["current_date"] == (DAY + timedelta(days=1)).isoformat()
        assert orders["current_display"] == "未知（无记录）"
        assert orders["previous_date"] == DAY.isoformat()
        assert orders["previous_display"] == "10"
        assert context["coverage"][0] == {"dataset": "店铺销量", "status_label": "尚未采集"}


def test_default_question_uses_one_recent_complete_report_date_with_explicit_target_notice(warehouse):
    settings, sessions, _ = warehouse
    with sessions() as db:
        context, value = bot_service.question_context(db, settings, "5174", now=NOW + timedelta(days=1))
        assert context["requested_report_date"] == "2026-11-13"
        assert context["report_date"] == DAY.isoformat()
        assert context["date_selection"] == "LATEST_COMPLETE_DATE"
        assert context["sales_and_spend"][0]["current_display"] == "10"
        assert context["sales_and_spend"][0]["current_date"] == DAY.isoformat()
        assert context["advertising_kpis"]["CTR"]["current_date"] == DAY.isoformat()
        assert context["advertising_kpis"]["CDR"]["current_date"] == (DAY - timedelta(days=14)).isoformat()
        assert all(r["report_date"] == DAY.isoformat() for r in context["advertising_totals"])
        text = json.dumps(value, ensure_ascii=False)
        assert "查询目标日 2026-11-13 尚未完成采集" in text
        assert "报表日 2026-11-12" in text and "前一日 2026-11-11" in text


@pytest.mark.parametrize("status", ["FAILED", "PARTIAL", "PENDING", "COMPLETE"])
def test_default_question_does_not_hide_any_existing_target_partition(warehouse, status):
    settings, sessions, _ = warehouse
    with sessions() as db:
        db.add(DataCoverage(store_id="5174", metric_date=DAY + timedelta(days=1), dimension="store", status=status))
        db.commit()
        context, _ = bot_service.question_context(db, settings, "5174", now=NOW + timedelta(days=1))
        assert context["report_date"] == "2026-11-13"
        assert context["date_selection"] == "REQUESTED_DATE"
        assert context["sales_and_spend"][0]["current_display"] == ("接口无记录" if status == "COMPLETE" else "未知（无记录）")


def test_default_question_does_not_relabel_stale_history_or_unauthorized_store(warehouse):
    settings, sessions, _ = warehouse
    with sessions() as db:
        context, _ = bot_service.question_context(db, settings, "5174", now=NOW + timedelta(days=9))
        assert context["date_selection"] == "NOT_COLLECTED"
        assert context["report_date"] != DAY.isoformat()
        db.get(Store, "5174").status = 2
        db.commit()
        context, _ = bot_service.question_context(db, settings, "5174", now=NOW + timedelta(days=1))
        assert context["date_selection"] == "STORE_UNAVAILABLE"
        assert context["report_date"] == "2026-11-13"
        assert context["authorization_warning"]


def test_report_date_fallback_requires_consistent_same_day_coverage_and_actual_store_facts(warehouse):
    settings, sessions, _ = warehouse
    with sessions() as db:
        coverage = db.scalar(select(DataCoverage).where(DataCoverage.store_id == "5174",
            DataCoverage.metric_date == DAY, DataCoverage.dimension == "campaign_sp"))
        coverage.row_count = 99
        db.commit()
        context, _ = bot_service.question_context(db, settings, "5174", now=NOW + timedelta(days=1))
        assert context["report_date"] == (DAY - timedelta(days=1)).isoformat()
        assert all(r["report_date"] == context["report_date"] for r in context["advertising_totals"])


@pytest.mark.parametrize("explicit", [False, True])
def test_failed_collection_without_new_coverage_is_not_masked_by_historical_fallback(warehouse, explicit):
    settings, sessions, _ = warehouse
    with sessions() as db:
        db.add(SyncJob(job_type="daily_collection", status="PARTIAL_FAILED", metadata_json={
            "report_dates": {"5174": "2026-11-13"}, "completed_steps": ["store_status"]}))
        db.commit()
        context, value = bot_service.question_context(db, settings, "5174", now=NOW + timedelta(days=1),
            question="2026-11-13 店铺情况" if explicit else None)
        assert context["date_selection"] == "COLLECTION_FAILED"
        assert context["report_date"] == "2026-11-13"
        assert "采集失败" in json.dumps(value, ensure_ascii=False)


def test_failed_refresh_masks_old_complete_facts_and_latest_success_removes_failure_guard(warehouse):
    settings, sessions, _ = warehouse
    with sessions() as db:
        db.add(SyncJob(job_type="daily_collection", status="FAILED", metadata_json={"report_dates": {"5174": DAY.isoformat()}}))
        db.commit()
        context, _ = bot_service.question_context(db, settings, "5174", now=NOW)
        assert context["date_selection"] == "COLLECTION_FAILED"
        assert context["sales_and_spend"][0]["current_display"] == "未知（无记录）"
        assert all(r["spend"] is None for r in context["advertising_totals"])
        db.add(SyncJob(job_type="daily_collection", status="SUCCEEDED", metadata_json={"report_dates": {"5174": DAY.isoformat()}}))
        db.commit()
        context, _ = bot_service.question_context(db, settings, "5174", now=NOW)
        assert context["date_selection"] == "REQUESTED_DATE"
        assert context["sales_and_spend"][0]["current_display"] == "10"


def test_later_product_or_health_failure_keeps_committed_daily_values_with_a_warning(warehouse):
    settings, sessions, _ = warehouse
    with sessions() as db:
        db.add(SyncJob(job_type="daily_collection", status="PARTIAL_FAILED", metadata_json={
            "report_dates": {"5174": DAY.isoformat()}, "completed_steps": ["sales", "campaign_reports"]}))
        db.commit()
        context, value = bot_service.question_context(db, settings, "5174", now=NOW)
        assert context["sales_and_spend"][0]["current_display"] == "10"
        assert context["advertising_totals"][0]["spend"] == 100
        assert "其他维度采集未完成" in context["report_date_warning"]
        assert "其他维度采集未完成" in json.dumps(value, ensure_ascii=False)


def test_other_stores_cannot_push_a_relevant_failed_attempt_out_of_a_global_job_limit(warehouse):
    settings, sessions, _ = warehouse
    with sessions() as db:
        db.add(SyncJob(job_type="daily_collection", status="FAILED", metadata_json={"report_dates": {"5174": "2026-11-13"}}))
        db.flush()
        db.add_all([SyncJob(job_type="daily_collection", status="SUCCEEDED", metadata_json={
            "report_dates": {"5175": "2026-11-13"}}) for _ in range(350)])
        db.commit()
        context, _ = bot_service.question_context(db, settings, "5174", now=NOW + timedelta(days=1))
        assert context["date_selection"] == "COLLECTION_FAILED"
        assert context["report_date"] == "2026-11-13"


@pytest.mark.parametrize("metadata", [{"report_dates": "2026-11-13"},
                                      {"ad_report_dates": {"5174": ["2026-11-12", "2026-11-13"]}}])
def test_failure_guard_supports_legacy_scalar_dates_and_advertising_refresh_ranges(warehouse, metadata):
    settings, sessions, _ = warehouse
    with sessions() as db:
        db.add(SyncJob(job_type="daily_collection", status="FAILED", metadata_json=metadata))
        db.commit()
        context, _ = bot_service.question_context(db, settings, "5174", now=NOW + timedelta(days=1))
        assert context["date_selection"] == "COLLECTION_FAILED"


def test_current_or_future_day_is_explicitly_incomplete_and_cannot_move_conversion_maturity_forward(warehouse):
    settings, sessions, _ = warehouse
    with sessions() as db:
        context, value = bot_service.question_context(db, settings, "5174", now=NOW, question="今天店铺情况")
        assert context["requested_report_date"] == context["report_date"] == "2026-11-13"
        assert context["last_completed_report_date"] == DAY.isoformat()
        assert context["report_date_is_complete"] is False
        assert context["authorization_warning"] is None
        assert all(row["current"] is None for row in context["advertising_kpis"].values())
        assert "查询报表日期尚未结束" in json.dumps(value, ensure_ascii=False)


@pytest.mark.parametrize("query", ["2026-11-13 店铺情况", "2026年11月13日店铺情况", "11月13日店铺情况", "昨天店铺情况"])
def test_explicit_date_and_relative_yesterday_never_fall_back(warehouse, query):
    settings, sessions, _ = warehouse
    with sessions() as db:
        context, _ = bot_service.question_context(db, settings, "5174", now=NOW + timedelta(days=1), question=query)
        assert context["report_date"] == "2026-11-13" and context["date_selection"] == "EXPLICIT_DATE"


@pytest.mark.parametrize("query", ["2026-11-31店铺情况", "2026-11-12和2026-11-13店铺情况"])
def test_invalid_or_multiple_report_dates_get_a_clarification_without_a_model_call(warehouse, query):
    enqueue(warehouse, event("@_user_1 5174 " + query))
    result = process(warehouse)
    assert FakeClient.calls == []
    with warehouse[1]() as db:
        job = db.get(SyncJob, result["job_id"])
        assert job.metadata_json["analysis_provider"] == "date_clarification"
        row = db.get(NotificationOutbox, result["outbox_ids"][0])
        assert "请确认报表日期" in json.dumps(row.payload["card"], ensure_ascii=False)


def test_empty_ad_response_is_labelled_without_inventing_currency_or_zero_cost(warehouse):
    settings, sessions, _ = warehouse
    with sessions() as db:
        for fact in db.scalars(select(DailyMetric).where(DailyMetric.store_id == "5174",
                DailyMetric.metric_date == DAY, DailyMetric.dimension == "campaign_sb")).all():
            db.delete(fact)
        coverage = db.scalar(select(DataCoverage).where(DataCoverage.store_id == "5174",
            DataCoverage.metric_date == DAY, DataCoverage.dimension == "campaign_sb"))
        coverage.row_count = 0
        db.commit()
        context, value = bot_service.question_context(db, settings, "5174", now=NOW)
        sb = next(row for row in context["advertising_totals"] if row["ad_type"] == "品牌推广")
        assert sb["no_records"] is True and sb["coverage"] == "COMPLETE"
        assert sb["spend"] is None and sb["spend_currency"] is None
        result = bot_service._answer_cards(context, value, "")
        rows = [r for card in result for e in card["body"]["elements"] if e["tag"] == "table" for r in e["rows"]]
        raw_sb = next(row for row in rows if row["c0"] == "品牌推广" and "c6" in row)
        assert all(raw_sb[f"c{i}"] == "接口无记录" for i in range(1, 7))


def test_ingestion_persists_only_needed_fields_and_same_message_is_one_durable_job(warehouse):
    first = enqueue(warehouse)
    second = enqueue(warehouse)
    assert first["status"] == "QUEUED" and second["status"] == "REUSED"
    assert first["job_id"] == second["job_id"]
    with warehouse[1]() as db:
        assert db.scalar(select(func.count(SyncJob.id))) == 1
        row = db.get(SyncJob, first["job_id"])
        serialized = json.dumps(row.metadata_json)
        assert HEADER_TOKEN not in serialized and "private-union" not in serialized
        assert row.metadata_json["question"] == "5174 店铺广告和绩效情况"
        assert row.metadata_json["message_id"] == "om_question"
        assert db.scalar(select(NotificationOutbox)) is None


@pytest.mark.parametrize("invalid", [
    "event_type", "app_id", "sender_type", "sender_id", "chat_type", "chat_id", "message_type",
    "not_mentioned", "another_bot", "message_id", "malformed_json", "json_array", "json_null", "empty", "long",
])
def test_only_real_users_mentioning_this_bot_in_the_allowed_group_are_queued(warehouse, invalid):
    incoming = event()
    header, sender, message = incoming["header"], incoming["event"]["sender"], incoming["event"]["message"]
    if invalid == "event_type":
        header["event_type"] = "different.event"
    elif invalid == "app_id":
        header["app_id"] = "cli_other_app"
    elif invalid == "sender_type":
        sender["sender_type"] = "app"
    elif invalid == "sender_id":
        sender["sender_id"]["open_id"] = "unverified-user"
    elif invalid in {"chat_type", "chat_id", "message_type", "message_id"}:
        message[invalid] = {"chat_type": "p2p", "chat_id": "oc_other_group",
                            "message_type": "image", "message_id": "unverified-message"}[invalid]
    elif invalid == "not_mentioned":
        message["mentions"] = []
    elif invalid == "another_bot":
        message["mentions"][0]["id"]["open_id"] = "ou_someone_else"
    else:
        message["content"] = {"malformed_json": "{", "json_array": "[]", "json_null": "null",
            "empty": json.dumps({"text": "@_user_1 "}), "long": json.dumps({"text": "中" * 1201})}[invalid]
    assert enqueue(warehouse, incoming)["status"] == "IGNORED"
    with warehouse[1]() as db:
        assert db.scalar(select(SyncJob)) is None
        assert db.scalar(select(NotificationOutbox)) is None


def test_single_sid_reply_has_actual_sales_advertising_ratios_health_and_chinese_analysis(warehouse):
    queued = enqueue(warehouse)
    result = process(warehouse)
    assert result["status"] == "QUEUED" and result["job_id"] == queued["job_id"]
    assert len(FakeClient.calls) == 1
    context, question = FakeClient.calls[0]
    assert context["store_id"] == "5174" and context["report_date"] == DAY.isoformat()
    assert question == "5174 店铺广告和绩效情况"
    assert context["advertising_kpis"]["CPC"]["current"] == 5
    assert context["advertising_kpis"]["TCOAS"]["current"] == pytest.approx(0.15)
    assert context["health"]["status"] == "UNCONFIRMED"
    assert HEADER_TOKEN not in json.dumps(context)
    with warehouse[1]() as db:
        job = db.get(SyncJob, result["job_id"])
        assert job.status == "SUCCEEDED" and job.metadata_json["analysis_provider"] == "deepseek"
        rows = [db.get(NotificationOutbox, key) for key in result["outbox_ids"]]
        assert all(row.payload["delivery"]["receive_id"] == "oc_reports" for row in rows)
        assert all(row.payload["delivery"]["reply_to_message_id"] == "om_question" for row in rows)
        content = json.dumps([row.payload["card"] for row in rows], ensure_ascii=False)
        assert ANALYSIS in content
        assert all(code in content for code in ("CTR", "CPC", "CDR", "CPS", "ROAS", "CPA", "TCOAS"))
        assert all(label in content for label in ("经营与广告", "广告明细", "归因订单", "归因销量", "健康评分", "源时区待确认"))
        assert "2,000.00" in content and "220" in content
        assert all(row.status == "PENDING" for row in rows)  # Queueing is not confirmed delivery.


@pytest.mark.parametrize("question", ["同创美国的情况", "你好", "9999 店铺情况", "151740 店铺情况"])
def test_ambiguous_or_outside_scope_store_gets_clarification_without_model_call(warehouse, question):
    enqueue(warehouse, event("@_user_1 " + question))
    result = process(warehouse)
    assert FakeClient.calls == []
    with warehouse[1]() as db:
        job = db.get(SyncJob, result["job_id"])
        assert job.metadata_json["analysis_provider"] == "store_clarification"
        row = db.get(NotificationOutbox, result["outbox_ids"][0])
        value = json.dumps(row.payload["card"], ensure_ascii=False)
        assert "请确认店铺" in value
        assert "范围外店铺" not in value
        if question.startswith("同创美国"):
            assert "同创美国" in value and "完整店铺名和站点" in value
            assert "店铺ID" not in value


def test_missing_deepseek_key_still_replies_with_real_data_without_network(warehouse):
    enqueue(warehouse)
    settings, sessions, engine = warehouse
    result = bot_service.process_one_question(settings, sessions, engine)
    assert result["status"] == "QUEUED"
    with sessions() as db:
        job = db.get(SyncJob, result["job_id"])
        assert job.metadata_json["analysis_provider"] == "unavailable"
        rows = [db.get(NotificationOutbox, key) for key in result["outbox_ids"]]
        value = json.dumps([row.payload["card"] for row in rows], ensure_ascii=False)
        assert "智能分析暂不可用" in value
        assert all(label in value for label in ("经营与广告", "CTR", "TCOAS", "店铺绩效快报"))


def test_reprocessing_same_message_never_creates_another_reply(warehouse):
    queued = enqueue(warehouse)
    result = process(warehouse)
    again = enqueue(warehouse)
    assert again["job_id"] == queued["job_id"] and again["status"] == "REUSED"
    assert process(warehouse)["status"] == "IDLE"
    # Simulate an interrupted caller replaying an already queued reply.
    with warehouse[1]() as db:
        job = db.get(SyncJob, result["job_id"])
        job.status = "PENDING"
        db.commit()
    replay = process(warehouse)
    assert replay["outbox_ids"] == result["outbox_ids"]
    with warehouse[1]() as db:
        assert db.scalar(select(func.count(NotificationOutbox.id))) == len(result["outbox_ids"])


def test_expired_question_is_not_analyzed_or_sent(warehouse):
    queued = enqueue(warehouse)
    with warehouse[1]() as db:
        job = db.get(SyncJob, queued["job_id"])
        job.started_at = (NOW - timedelta(hours=3)).replace(tzinfo=None)
        db.commit()
    assert process(warehouse)["status"] == "EXPIRED"
    assert FakeClient.calls == []
    with warehouse[1]() as db:
        assert db.scalar(select(NotificationOutbox)) is None


@pytest.mark.parametrize("flag,value", [("feishu_bot_enabled", False), ("feishu_enabled", False),
                                       ("app_dry_run", True)])
def test_disabled_or_dry_run_bot_does_not_enqueue_or_process(warehouse, flag, value):
    setattr(warehouse[0], flag, value)
    assert enqueue(warehouse)["status"] == "DISABLED"
    assert process(warehouse)["status"] == "DISABLED"
    assert FakeClient.calls == []
    with warehouse[1]() as db:
        assert db.scalar(select(SyncJob)) is None
        assert db.scalar(select(NotificationOutbox)) is None


def test_busy_queue_replies_once_without_adding_model_work(warehouse):
    warehouse[0].feishu_bot_max_pending = 1
    enqueue(warehouse)
    incoming = event(message_id="om_busy")
    busy = enqueue(warehouse, incoming)
    assert busy["status"] == "BUSY"
    assert enqueue(warehouse, incoming)["status"] == "REUSED"
    assert FakeClient.calls == []
    with warehouse[1]() as db:
        row = db.get(SyncJob, busy["job_id"])
        assert row.status == "SUCCEEDED"
        assert len(row.metadata_json["outbox_ids"]) == 1
        reply = db.get(NotificationOutbox, row.metadata_json["outbox_ids"][0])
        assert "提问较多" in json.dumps(reply.payload["card"], ensure_ascii=False)


def test_unexpected_processing_failure_has_bounded_recovery_and_no_replies(warehouse, monkeypatch):
    queued = enqueue(warehouse)
    failures = []

    def unavailable(*args, **kwargs):
        failures.append(True)
        raise ValueError("warehouse context unavailable")

    monkeypatch.setattr(bot_service, "question_context", unavailable)
    for _ in range(4):
        try:
            process(warehouse)
        except ValueError:
            pass
    with warehouse[1]() as db:
        assert db.get(SyncJob, queued["job_id"]).status == "FAILED"
        assert db.scalar(select(NotificationOutbox)) is None
    assert len(failures) == 2 and FakeClient.calls == []


def private_event(question="5174 店铺广告和绩效情况", *, message_id="om_private", sender="ou_member", chat="oc_private"):
    value = event(question, message_id=message_id)
    value["event"]["sender"]["sender_id"]["open_id"] = sender
    value["event"]["message"].update(chat_type="p2p", chat_id=chat)
    value["event"]["message"].pop("mentions")
    return value


def test_mapped_owner_private_question_needs_no_mention_and_replies_only_in_private_chat(warehouse, monkeypatch):
    _enable_collection(warehouse, monkeypatch)
    queued = enqueue(warehouse, private_event())
    assert queued["status"] == "QUEUED"
    assert enqueue(warehouse, private_event())["status"] == "REUSED"
    result = process(warehouse)
    assert result["status"] == "QUEUED" and len(FakeClient.calls) == 1
    with warehouse[1]() as db:
        meta = db.get(SyncJob, queued["job_id"]).metadata_json
        assert meta["chat_type"] == "p2p" and meta["sender_id"] == "ou_member"
        deliveries = [db.get(NotificationOutbox, row).payload["delivery"] for row in result["outbox_ids"]]
    assert deliveries and all(d["role"] == "private_question" and d["receive_id"] == "oc_private"
        and d["sender_open_id"] == "ou_member" and d["reply_to_message_id"] == "om_private"
        and d["open_ids"] == [] for d in deliveries)


@pytest.mark.parametrize("is_member", [True, False])
def test_private_read_checks_current_group_members_before_any_data_or_model_work(warehouse, monkeypatch, is_member):
    from jx_alert import admin_mapping
    lookups = []
    def members(settings, chat):
        lookups.append(chat)
        return {"ou_member": "测试成员"} if is_member else {}
    monkeypatch.setattr(admin_mapping, "_members", members)
    enqueue(warehouse, private_event())
    assert lookups == []  # SDK intake stays a durable, network-free operation.
    result = process(warehouse)
    assert result["status"] == ("QUEUED" if is_member else "SKIPPED")
    assert lookups and set(lookups) == {"oc_reports"}
    assert len(FakeClient.calls) == int(is_member)
    if not is_member:
        with warehouse[1]() as db:
            assert db.scalar(select(NotificationOutbox)) is None


def test_private_membership_api_failure_is_not_a_read_permission_fallback(warehouse, monkeypatch):
    from jx_alert import admin_mapping
    def unavailable(*args):
        raise admin_mapping.MappingError(502, "无法读取飞书群成员。")
    monkeypatch.setattr(admin_mapping, "_members", unavailable)
    queued = enqueue(warehouse, private_event())
    with pytest.raises(admin_mapping.MappingError):
        process(warehouse)
    assert FakeClient.calls == []
    with warehouse[1]() as db:
        assert db.scalar(select(NotificationOutbox)) is None
        assert db.get(SyncJob, queued["job_id"]).status == "RUNNING"
    monkeypatch.setattr(admin_mapping, "_members", lambda *a: {"ou_member": "已核实成员"})
    assert process(warehouse)["status"] == "QUEUED"


@pytest.mark.parametrize("role", ["operator", "admin", "other"])
def test_private_realtime_collection_keeps_existing_store_role_requirement(warehouse, monkeypatch, role):
    from jx_alert import admin_mapping
    _enable_collection(warehouse, monkeypatch, role=role)
    monkeypatch.setattr(admin_mapping, "_members", lambda *a: {"ou_member": "普通群成员"})
    enqueue(warehouse, private_event("实时采集 Geteen 美国"))
    # Select an unambiguous persisted name, not a model-resolved store.
    with warehouse[1].begin() as db:
        db.get(Store, "5174").name = "Geteen-US"
        db.get(Store, "5174").country = "美国"
    result = process(warehouse)
    assert result["status"] == "QUEUED" and FakeClient.calls == []
    with warehouse[1]() as db:
        jobs = list(db.scalars(select(SyncJob).where(SyncJob.job_type == "manual_collection")))
        assert len(jobs) == int(role != "other")
    if role == "other":
        assert "请由该店铺负责人或群管理员" in _collection_receipt(warehouse, result)


def test_private_cache_is_not_shared_between_different_senders_even_with_same_chat(warehouse, monkeypatch):
    _enable_collection(warehouse, monkeypatch)
    settings = warehouse[0]
    routing = json.loads(Path(settings.feishu_routing_file).read_text())
    routing["stores"]["5174"]["open_ids"].append("ou_second")
    Path(settings.feishu_routing_file).write_text(json.dumps(routing))
    enqueue(warehouse, private_event())
    process(warehouse)
    enqueue(warehouse, private_event(message_id="om_second_private", sender="ou_second"))
    process(warehouse)
    assert len(FakeClient.calls) == 2


def test_group_switch_skips_old_question_before_model_and_accepts_new_group(warehouse):
    queued = enqueue(warehouse)
    path = Path(warehouse[0].feishu_routing_file)
    routing = json.loads(path.read_text())
    routing["groups"]["reports"]["chat_id"] = "oc_new_group"
    path.write_text(json.dumps(routing))
    assert process(warehouse) == {"status": "SKIPPED", "job_id": queued["job_id"]}
    assert FakeClient.calls == []
    assert enqueue(warehouse, event(message_id="om_old_again"))["status"] == "IGNORED"
    new = event(message_id="om_new_group")
    new["event"]["message"]["chat_id"] = "oc_new_group"
    assert enqueue(warehouse, new)["status"] == "QUEUED"
    result = process(warehouse)
    with warehouse[1]() as db:
        assert all(db.get(NotificationOutbox, oid).payload["delivery"]["receive_id"] == "oc_new_group"
                   for oid in result["outbox_ids"])


def test_group_switch_during_model_analysis_does_not_redirect_old_private_content(warehouse):
    enqueue(warehouse)
    class SwitchingClient(FakeClient):
        def analyze(self, context, question):
            path = Path(warehouse[0].feishu_routing_file)
            routing = json.loads(path.read_text())
            routing["groups"]["reports"]["chat_id"] = "oc_new_group"
            path.write_text(json.dumps(routing))
            return super().analyze(context, question)
    assert process(warehouse, factory=lambda _: SwitchingClient())["status"] == "SKIPPED"
    with warehouse[1]() as db:
        assert db.scalar(select(NotificationOutbox)) is None


def test_private_queue_limit_does_not_call_membership_or_model_from_intake(warehouse):
    warehouse[0].feishu_bot_max_pending = 1
    enqueue(warehouse)
    incoming = private_event()
    busy = enqueue(warehouse, incoming)
    assert busy["status"] == "BUSY" and enqueue(warehouse, incoming)["status"] == "REUSED"
    with warehouse[1]() as db:
        assert db.get(SyncJob, busy["job_id"]).metadata_json["outbox_ids"] == []
        assert db.scalar(select(NotificationOutbox)) is None
    assert FakeClient.calls == []


def test_private_read_does_not_keep_membership_after_notification_group_switch(warehouse, monkeypatch):
    from jx_alert import admin_mapping
    checked = []
    def members(settings, chat):
        checked.append(chat)
        return {"ou_member": "旧群成员"} if chat == "oc_reports" else {}
    monkeypatch.setattr(admin_mapping, "_members", members)
    enqueue(warehouse, private_event())
    path = Path(warehouse[0].feishu_routing_file)
    routing = json.loads(path.read_text())
    routing["groups"]["reports"]["chat_id"] = "oc_new_group"
    path.write_text(json.dumps(routing))
    assert process(warehouse)["status"] == "SKIPPED"
    assert checked == ["oc_new_group"] and FakeClient.calls == []


def test_private_role_revoked_during_analysis_prevents_reply(warehouse, monkeypatch):
    from jx_alert import admin_mapping
    _enable_collection(warehouse, monkeypatch)
    monkeypatch.setattr(admin_mapping, "_members", lambda *a: {})
    enqueue(warehouse, private_event())
    class RevokedClient(FakeClient):
        def analyze(self, context, question):
            path = Path(warehouse[0].feishu_routing_file)
            routing = json.loads(path.read_text())
            routing["stores"]["5174"]["open_ids"] = []
            path.write_text(json.dumps(routing))
            return super().analyze(context, question)
    assert process(warehouse, factory=lambda _: RevokedClient())["status"] == "SKIPPED"
    with warehouse[1]() as db:
        assert db.scalar(select(NotificationOutbox)) is None


def test_private_collection_receipt_is_bound_to_original_sender(warehouse, monkeypatch):
    _enable_collection(warehouse, monkeypatch)
    enqueue(warehouse, private_event("实时采集5174"))
    first = process(warehouse)
    with warehouse[1]() as db:
        receipt = db.get(SyncJob, first["job_id"])
        collection_id = receipt.metadata_json["collection_job_id"]
    # Even a currently configured operator cannot borrow another sender's
    # private receipt, if a corrupted/replayed envelope reuses that chat ID.
    path = Path(warehouse[0].feishu_routing_file)
    routing = json.loads(path.read_text())
    routing["stores"]["5174"]["open_ids"].append("ou_second")
    path.write_text(json.dumps(routing))
    enqueue(warehouse, private_event(f"采集任务{collection_id}状态", message_id="om_other_sender", sender="ou_second"))
    assert "未找到可查询的采集任务" in _collection_receipt(warehouse, process(warehouse))


@pytest.mark.parametrize("missing_field", ["schema", "tenant"])
def test_queue_validation_keeps_schema_and_configured_tenant_checks(warehouse, missing_field):
    data = private_event()
    settings = warehouse[0]
    if missing_field == "schema":
        data.pop("schema")
    else:
        settings = settings.model_copy(update={"feishu_bot_tenant_key": "confirmed-tenant"})
    with warehouse[1]() as db:
        assert bot_service.enqueue_group_question(db, settings, data)["status"] == "IGNORED"
        assert db.scalar(select(SyncJob)) is None


@pytest.mark.parametrize("legacy", [True, False])
def test_switch_back_to_same_group_never_revives_an_old_queued_question(warehouse, legacy):
    queued = enqueue(warehouse)
    if legacy:
        with warehouse[1].begin() as db:
            job = db.get(SyncJob, queued["job_id"])
            job.metadata_json = {key: value for key, value in job.metadata_json.items() if key != "question_route"}
    path = Path(warehouse[0].feishu_routing_file)
    routing = json.loads(path.read_text())
    routing["groups"]["reports"]["notifications_changed_at"] = "2026-11-14T00:00:00+00:00"
    # Same chat ID, different route generation, as in A->B->A.
    path.write_text(json.dumps(routing))
    assert process(warehouse)["status"] == "SKIPPED" and FakeClient.calls == []
    new = event(message_id="om_new_generation")
    assert enqueue(warehouse, new)["status"] == "QUEUED"
    assert process(warehouse)["status"] == "QUEUED"


def test_busy_reply_uses_current_route_generation(warehouse):
    path = Path(warehouse[0].feishu_routing_file)
    routing = json.loads(path.read_text())
    routing["groups"]["reports"]["notifications_changed_at"] = "2026-11-14T00:00:00+00:00"
    path.write_text(json.dumps(routing))
    warehouse[0].feishu_bot_max_pending = 1
    enqueue(warehouse)
    result = enqueue(warehouse, event(message_id="om_busy_current"))
    assert result["status"] == "BUSY"
    with warehouse[1]() as db:
        outbox_id = db.get(SyncJob, result["job_id"]).metadata_json["outbox_ids"][0]
        route = db.get(NotificationOutbox, outbox_id).payload["delivery"]
        assert route["routing_changed_at"] == "2026-11-14T00:00:00+00:00"
