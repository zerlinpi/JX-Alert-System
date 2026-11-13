from datetime import date
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from jx_alert.notifications.cards import anomaly_card, change, number, with_mentions
from jx_alert.notifications.daily_report import build_daily_card
from jx_alert.notifications.feishu_app import FeishuAppClient
from jx_alert.notifications import worker
from jx_alert.notifications.outbox import NotificationOutbox, enqueue_notification
from jx_alert.storage.db import Base
from jx_alert.storage.models import DailyMetric, DataCoverage, Store, StorePerformance


def test_card_preserves_explicit_zero_missing_and_incomplete_comparison():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = Store(sid="5174", name="Geteen-US", timezone="America/Los_Angeles", currency_code="USD")
        db.add(store)
        db.add(StorePerformance(store_id=store.sid, pull_date=date(2026, 11, 11), ahr_score=248,
                                ahr_status="Healthy", freshness_status="NOT_CONFIGURED"))
        for day, value in [(date(2026, 11, 10), 10), (date(2026, 11, 11), 0)]:
            db.add(DailyMetric(store_id=store.sid, entity_id=store.sid, metric_date=day,
                               dimension="store", metric_name="orders", value=value))
            db.add(DataCoverage(store_id=store.sid, metric_date=day, dimension="store", status="COMPLETE"))
        db.flush()
        result = build_daily_card(db, store, date(2026, 11, 11))
        table = next(e for e in result["body"]["elements"] if e["tag"] == "table")
        assert table["rows"][0] == {"c0": "订单", "c1": "0", "c2": "10", "c3": "-100.0%"}
        assert table["rows"][1]["c1"] == "未知（无记录）"
        assert table["rows"][3]["c1"] == "未知（无记录）"
        assert table["rows"][3]["c3"] == "待完整采集"
        assert "美国太平洋时间" in json.dumps(result, ensure_ascii=False)
        tables = [e for e in result["body"]["elements"] if e["tag"] == "table"]
        assert tables[2]["rows"][0] == {"c0": "健康评分", "c1": "248", "c2": "源时区待确认"}


def sample_card():
    return anomaly_card(state="新异常", severity="P1", store="测试店铺", campaign="活动",
                        product="MSKU test", rule="spend_no_conversion", reason="待核查<at id=all></at>",
                        metrics={"spend": 20, "orders": 0, "clicks": 12, "seller_sku": "<at id=all></at>"})


def test_only_mapped_mentions_create_card_tags_without_mutating_original():
    original = sample_card()
    result = with_mentions(original, ["ou_admin", "ou_operator", "ou_admin"])
    mention = result["body"]["elements"][0]["content"]
    assert mention.count("<at ") == 2 and "id=all" not in mention
    assert original["body"]["elements"][0]["content"].startswith("待核查&lt;")
    with pytest.raises(ValueError):
        with_mentions(original, ['ou_bad"><at id=all>'])


def test_card_delivery_reuses_uuid_refreshes_token_and_keeps_native_tables(monkeypatch):
    requests, calls = [], 0

    def handler(request):
        nonlocal calls
        requests.append(request)
        if request.url.path.endswith("/internal"):
            calls += 1
            return httpx.Response(200, json={"code": 0, "tenant_access_token": f"test-{calls}", "expire": 7200})
        if len([r for r in requests if r.url.path.endswith('/messages')]) == 1:
            return httpx.Response(401, json={"code": 99991663})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_test_card"}})

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    client = FeishuAppClient("cli_test", "synthetic-secret")
    result = client.send_card(sample_card(), receive_id="oc_test", open_ids=["ou_admin"], idempotency_key="same-key")
    assert result["message_id"] == "om_test_card"
    sent = [json.loads(r.content) for r in requests if r.url.path.endswith('/messages')]
    assert sent[0]["uuid"] == sent[1]["uuid"]
    assert sent[1]["msg_type"] == "interactive"
    content = json.loads(sent[1]["content"])
    assert any(e["tag"] == "table" for e in content["body"]["elements"])


def test_worker_sends_persisted_card_without_regressing_old_text(monkeypatch, tmp_path):
    from sqlalchemy.orm import sessionmaker
    path = tmp_path / "card.db"
    engine = create_engine("sqlite:///" + str(path))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = SimpleNamespace(app_dry_run=False, feishu_enabled=True, feishu_transport="app",
                               feishu_timeout=1, feishu_routing_file="unused")
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    sent = []

    class Client:
        def send_card(self, value, **kwargs):
            sent.append(("card", value))
            return {"message_id": "om_card"}

        def send_text(self, value, **kwargs):
            sent.append(("text", value))
            return {"message_id": "om_text"}

    monkeypatch.setattr(FeishuAppClient, "from_settings", lambda _: Client())
    delivery = {"role": "daily_snapshot", "receive_id": "oc_test", "receive_id_type": "chat_id", "open_ids": []}
    with sessions() as db:
        enqueue_notification(db, idempotency_key="card", text="fallback", delivery=delivery, card=sample_card())
        enqueue_notification(db, idempotency_key="old", text="old text", delivery=delivery)
        db.commit()
    result = worker.process_outbox_batch()
    assert result["sent"] == 2
    assert {kind for kind, _ in sent} == {"card", "text"}
    with sessions() as db:
        assert {row.status for row in db.scalars(select(NotificationOutbox))} == {"DELIVERED"}
    assert worker.process_outbox_batch()["sent"] == 0


def test_comparison_zero_denominator_and_nonfinite_data_are_explicit():
    assert change(4, 0, complete=True) == "前值为零"
    assert change(0, 0, complete=True) == "持平"
    assert change(None, 1, complete=True) == "未知（缺少对比）"
    assert change(float('nan'), 1, complete=True) == "未知（无效值）"
    assert number(0, money=True, currency="USD") == "美元 0.00"
    assert number(float('inf')) == "未知（无效值）"
