"""Group questions use scoped persisted facts, durable replies, and no MCP calls."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from jx_alert import bot_service
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
            captured_at=NOW.replace(tzinfo=None), source_updated_at=NOW.isoformat(),
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
        assert context["coverage"][0] == {"dataset": "店铺销量", "status_label": "未知"}


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
            assert "5174" in value and "5175" in value


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
