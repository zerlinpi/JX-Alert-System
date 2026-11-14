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
