import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import jx_alert.analysis.service as service
import jx_alert.notifications.feishu_app as app_module
import jx_alert.notifications.worker as worker
from jx_alert.config import Settings
from jx_alert.notifications.daily_report import enqueue_daily_snapshots
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.notifications.routing import (
    application_deliveries, repair_application_notification,
)
from jx_alert.storage.db import Base
from jx_alert.storage.models import (
    AdProduct, AnomalyEvent, DailyMetric, DataCoverage, NotificationLog, Product, Store,
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    path = tmp_path / "routing.json"
    config = {
        "default_chat_id": "operations",
        "groups": {
            "operations": {"chat_id": "oc_operations", "admin_open_ids": ["ou_admin"]},
            "test": {"chat_id": "oc_test", "admin_open_ids": []},
        },
        "stores": {"s": {"open_ids": ["ou_operator"]}},
        "management": {"open_ids": ["ou_manager"]},
    }
    path.write_text(json.dumps(config), encoding="utf-8")
    settings = Settings(
        _env_file=None, app_dry_run=False, feishu_enabled=True, feishu_transport="app",
        feishu_app_id="cli_test", feishu_app_secret="test-secret",
        feishu_routing_file=str(path),
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "SessionLocal", Session)
    return Session, settings, config, path


def enqueue(Session):
    with Session() as db:
        event = AnomalyEvent(
            store_id="s", entity_id="SKU-1", dedupe_key="s:SKU-1:orders_decrease",
            rule="orders_decrease", severity="P1", status="NEW",
            evaluation_scope="s:sales:sku_msku", reason="订单下降",
            metrics={"current": 0, "baseline_7d": 10},
        )
        db.add(event)
        db.flush()
        service.enqueue_anomaly_notifications(db, [event])
        service.enqueue_anomaly_notifications(db, [event])
        db.commit()


def test_targeted_worker_does_not_send_other_pending_messages(setup, monkeypatch):
    Session, settings, config, path = setup
    from jx_alert.notifications.outbox import enqueue_notification
    sent = []

    class Client:
        def send_text(self, text, before_submit, **kwargs):
            before_submit({})
            sent.append(text)
            return {"message_id": "test-message"}

    monkeypatch.setattr(app_module.FeishuAppClient, "from_settings", lambda _: Client())
    with Session() as db:
        first = enqueue_notification(db, idempotency_key="first", text="first", delivery={
            "receive_id": "oc_test", "receive_id_type": "chat_id", "open_ids": []})
        other = enqueue_notification(db, idempotency_key="other", text="other", delivery={
            "receive_id": "oc_test", "receive_id_type": "chat_id", "open_ids": []})
        db.commit()
        first_id, other_id = first.id, other.id
    assert worker.process_outbox_batch(outbox_ids=[first_id])["sent"] == 1
    assert sent == ["first"]
    with Session() as db:
        assert db.get(NotificationOutbox, other_id).status == "PENDING"


def test_delivery_cas_survives_second_precision_database_timestamp(setup, monkeypatch):
    from sqlalchemy import event
    from jx_alert.notifications.outbox import enqueue_notification
    Session, settings, config, path = setup
    engine = Session.kw["bind"]

    @event.listens_for(engine, "after_cursor_execute")
    def coarsen_stored_claim(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE notification_outbox"):
            # Simulate MariaDB DATETIME(0) storage while leaving comparison bind
            # parameters untouched. SQLite otherwise preserves microseconds.
            cursor.execute("UPDATE notification_outbox SET claimed_at = "
                           "strftime('%Y-%m-%d %H:%M:%S', claimed_at) || '.000000' "
                           "WHERE status IN ('PREPARING', 'SUBMITTING') AND claimed_at IS NOT NULL")

    class Client:
        def send_text(self, text, before_submit, **kwargs):
            before_submit({})
            return {"message_id": "confirmed-message"}

    monkeypatch.setattr(app_module.FeishuAppClient, "from_settings", lambda _: Client())
    with Session() as db:
        row = enqueue_notification(db, idempotency_key="precision", text="test", delivery={
            "receive_id": "oc_test", "receive_id_type": "chat_id", "open_ids": []})
        db.commit()
        row_id = row.id
    worker.process_outbox_batch(outbox_ids=[row_id])
    with Session() as db:
        assert db.get(NotificationOutbox, row_id).status == "DELIVERED"


def test_one_bot_separate_group_mentions_and_manager_delivery_with_independent_retry(
    setup, monkeypatch,
):
    Session, settings, config, path = setup
    enqueue(Session)
    sent = []

    class FakeApp:
        @classmethod
        def from_settings(cls, settings):
            return cls()

        def send_text(self, text, before_submit, **kwargs):
            sent.append(kwargs)
            if kwargs["receive_id"] == "ou_manager" and len(sent) == 2:
                raise RuntimeError("token acquisition echoed test-secret")
            before_submit({})
            return {"message_id": "om_confirmed"}

        send_card = send_text

    monkeypatch.setattr(app_module, "FeishuAppClient", FakeApp)
    first = worker.process_outbox_batch()
    assert (first["sent"], first["failed"], first["claimed"]) == (1, 1, 2)
    assert sent[0]["open_ids"] == ["ou_admin", "ou_operator"]
    assert sent[1]["receive_id_type"] == "open_id"
    assert sent[1]["open_ids"] == []
    assert sent[0]["idempotency_key"] != sent[1]["idempotency_key"]
    with Session() as db:
        jobs = db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)).all()
        assert len(jobs) == 2
        assert jobs[0].status == "DELIVERED"
        assert jobs[1].status == "PRE_SEND_RETRY" and jobs[1].attempts == 1
        assert "test-secret" not in jobs[1].last_error
        jobs[1].next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        db.commit()
    assert worker.process_outbox_batch()["sent"] == 1
    assert [message["receive_id"] for message in sent] == [
        "oc_operations", "ou_manager", "ou_manager",
    ]
    assert sent[1]["idempotency_key"] == sent[2]["idempotency_key"]
    assert worker.process_outbox_batch()["claimed"] == 0
    with Session() as db:
        assert all(log.channel == "feishu_app" for log in db.scalars(select(NotificationLog)))
        assert all("test-secret" not in (log.error or "")
                   for log in db.scalars(select(NotificationLog)))


@pytest.mark.parametrize("missing", ["owner", "admin", "manager"])
def test_missing_route_role_is_durable_and_repair_does_not_duplicate_other_role(setup, missing):
    Session, settings, config, path = setup
    if missing == "owner":
        config["stores"] = {}
    elif missing == "admin":
        config["groups"]["operations"]["admin_open_ids"] = []
    else:
        config["management"] = {}
    path.write_text(json.dumps(config), encoding="utf-8")
    enqueue(Session)
    with Session() as db:
        jobs = db.scalars(select(NotificationOutbox)).all()
        assert len(jobs) == 2
        failed = next(job for job in jobs if job.status == "ROUTING_FAILED")
        failure_role = "management" if missing == "manager" else "group"
        assert failed.payload["routing_role"] == failure_role
        assert db.scalar(select(AnomalyEvent)) is not None
        config["stores"] = {"s": {"open_ids": ["ou_operator"]}}
        config["groups"]["operations"]["admin_open_ids"] = ["ou_admin"]
        config["management"] = {"open_ids": ["ou_manager"]}
        path.write_text(json.dumps(config), encoding="utf-8")
        assert repair_application_notification(db, settings, failed.id)["status"] == "SUPERSEDED"
        db.flush()
        jobs = db.scalars(select(NotificationOutbox)).all()
        assert len(jobs) == 3
        assert sum(job.status == "PENDING" for job in jobs) == 2


def test_malformed_group_mapping_does_not_suppress_environment_manager(setup):
    Session, settings, config, path = setup
    settings.feishu_manager_open_ids = "ou_env_manager"
    path.write_text("malformed", encoding="utf-8")
    with Session() as db:
        destinations = application_deliveries(
            db, settings, store_id="s", entity_id="sku", metrics={},
            scope="s:sales:sku_msku", severity="P1", status="NEW",
        )
    assert any(item.get("receive_id") == "ou_env_manager" for item in destinations)
    assert any(item.get("routing_failure") for item in destinations)


def test_campaign_uses_exact_advertised_sku_owner_before_shared_asin_fallback(setup):
    Session, settings, config, path = setup
    config["stores"] = {}
    path.write_text(json.dumps(config), encoding="utf-8")
    with Session() as db:
        db.add_all([
            Product(store_id="s", seller_sku="SKU-1", asin="B1", owner_open_ids=["ou_actual"]),
            Product(store_id="s", seller_sku="SKU-2", asin="B1", owner_open_ids=["ou_other"]),
            AdProduct(store_id="s", campaign_type="sp", campaign_id="c", ad_id="a",
                      seller_sku="SKU-1", asin="B1"),
        ])
        db.flush()
        destinations = application_deliveries(
            db, settings, store_id="s", entity_id="c", metrics={},
            scope="s:campaign:sp", severity="P1", status="NEW",
        )
    group = next(item for item in destinations if item["role"] == "group")
    assert group["open_ids"] == ["ou_admin", "ou_actual"]


def test_daily_snapshot_is_idempotent_fixed_to_test_group_and_preserves_missing_data(setup):
    Session, settings, config, path = setup
    settings.feishu_daily_report_enabled = True
    settings.feishu_daily_report_chat_id = "test"
    with Session() as db:
        db.add(Store(sid="s", name="测试店铺", currency_code="USD"))
        db.add(DailyMetric(
            store_id="s", metric_date=date(2026, 11, 11), dimension="store", entity_id="s",
            metric_name="orders", value=0,
        ))
        db.add(DataCoverage(store_id="s", metric_date=date(2026, 11, 11),
                            dimension="store", status="COMPLETE"))
        db.flush()
        for _ in range(2):
            result = enqueue_daily_snapshots(db, settings, report_date="2026-11-11")
            assert result["queued"] == 1
        jobs = db.scalars(select(NotificationOutbox)).all()
        assert len(jobs) == 1
        assert jobs[0].chat_id == "oc_test"
        assert jobs[0].payload["delivery"]["receive_id"] == "oc_test"
        assert jobs[0].payload["fixed_route"] is True
        assert "订单 0" in jobs[0].payload["text"]
        assert "销量 未知（无记录）" in jobs[0].payload["text"]
        assert "商品推广花费：未知（无记录）" in jobs[0].payload["text"]


def test_daily_snapshot_disabled_or_missing_destination_never_sends(setup):
    Session, settings, config, path = setup
    with Session() as db:
        assert enqueue_daily_snapshots(db, settings, report_date="2026-11-11")["skipped"]
        settings.feishu_daily_report_enabled = True
        with pytest.raises(ValueError, match="explicitly configured"):
            enqueue_daily_snapshots(db, settings, report_date="2026-11-11")
        assert db.scalars(select(NotificationOutbox)).all() == []


def test_daily_per_store_dates_include_only_the_selected_collection_scope(setup):
    Session, settings, config, path = setup
    settings.feishu_daily_report_enabled = True
    settings.feishu_daily_report_chat_id = "test"
    with Session() as db:
        db.add_all([Store(sid="s", name="selected"), Store(sid="other", name="unselected")])
        db.flush()
        result = enqueue_daily_snapshots(db, settings, store_dates={"s": "2026-11-10"})
        assert result["queued"] == 1
        job = db.scalar(select(NotificationOutbox))
        assert "2026-11-10" in job.payload["text"]
        assert job.payload["store_id"] == "s"


def test_dry_run_prevents_application_or_webhook_clients_being_created(setup, monkeypatch):
    Session, settings, config, path = setup
    enqueue(Session)
    settings.app_dry_run = True

    def forbidden(*args, **kwargs):
        pytest.fail("Dry-run must not acquire credentials or send anything")

    monkeypatch.setattr(app_module.FeishuAppClient, "from_settings", forbidden)
    monkeypatch.setattr(worker, "FeishuClient", forbidden)
    assert worker.process_outbox_batch()["skipped"] is True


def test_chat_delivery_mode_keeps_rule_candidates_out_of_group_notifications(setup):
    Session, settings, _, _ = setup
    settings.business_alert_delivery_mode = "chat"
    enqueue(Session)
    with Session() as db:
        assert db.scalar(select(AnomalyEvent)) is not None
        assert db.scalars(select(NotificationOutbox)).all() == []


@pytest.mark.parametrize("missing", ["owner", "admin", "both"])
def test_approved_fallback_sends_missing_roles_to_report_group_without_mentions(setup, monkeypatch, missing):
    Session, settings, config, path = setup
    settings.feishu_group_fallback_enabled = True
    settings.feishu_daily_report_chat_id = "test"
    if missing in {"owner", "both"}:
        config["stores"] = {}
    if missing in {"admin", "both"}:
        config["groups"]["operations"]["admin_open_ids"] = []
    if missing == "both":
        config["management"] = {}
    path.write_text(json.dumps(config), encoding="utf-8")
    original = path.read_bytes()
    enqueue(Session)
    sent = []

    class Client:
        def send_card(self, card, before_submit, **kwargs):
            before_submit({})
            sent.append(kwargs)
            return {"message_id": "synthetic-message"}

    monkeypatch.setattr(app_module.FeishuAppClient, "from_settings", lambda *_: Client())
    with Session() as db:
        jobs = db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)).all()
        group = next(job for job in jobs if job.chat_id == "oc_test")
        delivery = group.payload["delivery"]
        assert delivery["open_ids"] == [] and delivery["group_fallback"]
        assert len(jobs) == (1 if missing == "both" else 2)
        assert all(job.status == "PENDING" for job in jobs)
        if missing == "both":
            assert set(delivery["covered_roles"]) == {"group", "management"}
            assert len(delivery["fallback_reasons"]) == 2
        else:
            assert jobs[-1].payload["delivery"]["receive_id"] == "ou_manager"
    assert worker.process_outbox_batch()["sent"] == (1 if missing == "both" else 2)
    assert sum(item["receive_id"] == "oc_test" for item in sent) == 1
    assert next(item for item in sent if item["receive_id"] == "oc_test")["open_ids"] == []
    assert path.read_bytes() == original, "Fallback must not invent or rewrite owner mappings"


def test_missing_management_uses_same_group_once_and_preserves_verified_mentions(setup):
    Session, settings, config, path = setup
    settings.feishu_group_fallback_enabled = True
    settings.feishu_daily_report_chat_id = "operations"
    config["management"] = {}
    path.write_text(json.dumps(config), encoding="utf-8")
    enqueue(Session)
    with Session() as db:
        jobs = db.scalars(select(NotificationOutbox)).all()
        assert len(jobs) == 1
        delivery = jobs[0].payload["delivery"]
        assert delivery["receive_id"] == "oc_operations"
        assert delivery["open_ids"] == ["ou_admin", "ou_operator"]
        assert delivery["group_fallback"]
        assert set(delivery["covered_roles"]) == {"group", "management"}


@pytest.mark.parametrize("broken", ["missing_alias", "unknown_alias", "missing_chat", "unknown_route",
                                  "invalid_owner", "malformed_owner", "invalid_report_admin",
                                  "invalid_routed_admin", "invalid_defaults", "malformed_file"])
def test_fallback_never_bypasses_a_missing_group_or_malformed_mapping(setup, broken):
    Session, settings, config, path = setup
    settings.feishu_group_fallback_enabled = True
    settings.feishu_daily_report_chat_id = "test"
    config["stores"] = {}
    config["management"] = {}
    if broken == "missing_alias":
        settings.feishu_daily_report_chat_id = ""
    elif broken == "unknown_alias":
        settings.feishu_daily_report_chat_id = "unknown"
    elif broken == "missing_chat":
        config["groups"]["test"]["chat_id"] = ""
    elif broken == "unknown_route":
        config["stores"] = {"s": {"chat_id": "unknown", "open_ids": []}}
    elif broken == "invalid_owner":
        config["stores"] = {"s": {"open_ids": ["unverified-id"]}}
    elif broken == "malformed_owner":
        config["stores"] = {"s": "not-a-route"}
    elif broken == "invalid_report_admin":
        config["groups"]["test"]["admin_open_ids"] = ["unverified-id"]
    elif broken == "invalid_routed_admin":
        config["groups"]["operations"]["admin_open_ids"] = ["unverified-id"]
    elif broken == "invalid_defaults":
        settings.feishu_default_at_open_ids = "unverified-id"
    path.write_text("not-json" if broken == "malformed_file" else json.dumps(config), encoding="utf-8")
    enqueue(Session)
    with Session() as db:
        jobs = db.scalars(select(NotificationOutbox)).all()
        assert jobs and all(job.status == "ROUTING_FAILED" for job in jobs)
        assert all(not job.payload["delivery"].get("receive_id") for job in jobs)


def test_invalid_management_mapping_is_not_treated_as_an_absent_role(setup):
    Session, settings, config, path = setup
    settings.feishu_group_fallback_enabled = True
    settings.feishu_daily_report_chat_id = "operations"
    config["management"] = {"open_ids": ["invalid-user-id"]}
    path.write_text(json.dumps(config), encoding="utf-8")
    enqueue(Session)
    with Session() as db:
        jobs = db.scalars(select(NotificationOutbox)).all()
        assert len(jobs) == 2
        assert sum(job.status == "PENDING" for job in jobs) == 1
        assert sum(job.status == "ROUTING_FAILED" for job in jobs) == 1


def test_enabling_fallback_does_not_replay_history_and_explicit_repair_reuses_delivered_group(setup):
    Session, settings, config, path = setup
    config["management"] = {}
    path.write_text(json.dumps(config), encoding="utf-8")
    enqueue(Session)
    with Session() as db:
        jobs = db.scalars(select(NotificationOutbox)).all()
        group = next(job for job in jobs if job.status == "PENDING")
        failure = next(job for job in jobs if job.status == "ROUTING_FAILED")
        group.status, group.attempts = "DELIVERED", 1
        db.commit()
        settings.feishu_group_fallback_enabled = True
        settings.feishu_daily_report_chat_id = "operations"
        planned = application_deliveries(db, settings, store_id="s", entity_id="SKU-1", metrics={},
            scope="s:sales:sku_msku", severity="P1", status="NEW")
        assert len(planned) == 1
        assert failure.status == "ROUTING_FAILED" and group.status == "DELIVERED"
        result = repair_application_notification(db, settings, failure.id)
        assert result["status"] == "SUPERSEDED" and result["outbox_ids"] == [group.id]
        assert len(db.scalars(select(NotificationOutbox)).all()) == 2
        assert (group.status, group.attempts) == ("DELIVERED", 1)


def _enqueue_sku_card(Session, *, sku="SKU-1", scope="s:sales:sku_msku", metrics=None):
    with Session.begin() as db:
        event = AnomalyEvent(
            store_id="s", entity_id=sku, dedupe_key=f"{scope}:subject", rule="orders_decrease",
            severity="P1", status="NEW", evaluation_scope=scope, reason="订单下降",
            metrics={"current": 1, "baseline_7d": 10, **(metrics or {})},
        )
        db.add(event)
        db.flush()
        for _ in range(2):
            service.enqueue_anomaly_notifications(db, [event])


def _capture_sku_wire(monkeypatch):
    """Use real message serialization, write-ahead fence and receipt validation, no network."""
    calls = []

    def post(client, path, payload, **kwargs):
        if path == client._TOKEN_PATH:
            return {"code": 0, "tenant_access_token": "synthetic", "expire": 7200}
        assert path == client._MESSAGE_PATH
        assert kwargs["params"] == {"receive_id_type": "chat_id"}
        assert payload["msg_type"] == "interactive"
        calls.append((payload["receive_id"], json.loads(payload["content"])))
        return {"code": 0, "data": {"message_id": "om_synthetic"}}

    monkeypatch.setattr(app_module.FeishuAppClient, "_post_json", post)
    return calls


def _wire_mentions(card):
    return [element["content"] for element in card["body"]["elements"]
            if element["tag"] == "markdown" and "<at " in element["content"]]


@pytest.mark.parametrize("ad", [False, True], ids=["msku-sales", "advertised-msku"])
def test_exact_sku_card_and_mentions_do_not_expand_to_asin_or_sibling_owners(setup, monkeypatch, ad):
    Session, settings, config, path = setup
    config.update(management={"severities": []}, entities={
        "s:SKU-1": {"chat_id": "operations", "open_ids": ["ou_actual", "ou_configured", "ou_nonmember"]},
        "s:B0SHARED": ["ou_sibling_asin_owner"],
        # Store-local ownership wins over this legacy, global SKU mapping.
        "SKU-1": ["ou_global"], "other:SKU-1": ["ou_other_store"],
    })
    config["groups"]["operations"]["verified_member_open_ids"] = [
        "ou_admin", "ou_operator", "ou_actual", "ou_configured", "ou_sibling_asin_owner",
        "ou_sibling", "ou_global", "ou_other_store",
    ]
    path.write_text(json.dumps(config), encoding="utf-8")
    with Session.begin() as db:
        db.add_all([
            Product(store_id="s", seller_sku="SKU-1", asin="B0SHARED", owner_open_ids=["ou_actual", "ou_admin"]),
            Product(store_id="s", seller_sku="SIBLING", asin="B0SHARED", owner_open_ids=["ou_sibling"]),
            Product(store_id="other", seller_sku="SKU-1", asin="B0SHARED", owner_open_ids=["ou_sibling"]),
            # A refreshed ad link is not evidence that the old incident affected this sibling.
            AdProduct(store_id="s", campaign_type="sp", campaign_id="campaign", ad_id="ad",
                      seller_sku="SIBLING", asin="B0SHARED"),
        ])
    if ad:
        _enqueue_sku_card(Session, scope="s:product_ad:sp", metrics={
            "seller_sku": "SKU-1", "asin": "B0SHARED", "ad_id": "ad", "campaign_id": "campaign"})
    else:
        _enqueue_sku_card(Session)
    calls = _capture_sku_wire(monkeypatch)
    assert worker.process_outbox_batch()["sent"] == 1
    assert worker.process_outbox_batch()["claimed"] == 0
    [(chat, card)] = calls
    assert chat == "oc_operations"
    assert _wire_mentions(card) == [
        "负责人：<at id=ou_admin></at> <at id=ou_operator></at> <at id=ou_actual></at> <at id=ou_configured></at>"]
    contents = json.dumps(card, ensure_ascii=False)
    assert "商品编码 SKU-1" in contents and "B0SHARED" in contents
    assert "SIBLING" not in contents
    with Session() as db:
        [row] = db.scalars(select(NotificationOutbox)).all()
        assert row.status == "DELIVERED" and row.attempts == 1
        assert db.scalar(select(NotificationLog)).message_id == "om_synthetic"


@pytest.mark.parametrize("fallback", [False, True])
def test_missing_exact_sku_cannot_borrow_same_named_asin_for_card_or_owner(setup, monkeypatch, fallback):
    Session, settings, config, path = setup
    config.update(stores={}, management={"severities": []})
    settings.feishu_group_fallback_enabled = fallback
    settings.feishu_daily_report_chat_id = "test"
    path.write_text(json.dumps(config), encoding="utf-8")
    with Session.begin() as db:
        db.add(Product(store_id="s", seller_sku="OTHER-SKU", asin="SKU-1", owner_open_ids=["ou_other"]))
    _enqueue_sku_card(Session)
    calls = _capture_sku_wire(monkeypatch)
    assert worker.process_outbox_batch()["sent"] == int(fallback)
    if fallback:
        [(chat, card)] = calls
        assert chat == "oc_test" and _wire_mentions(card) == []
        assert "商品编码 SKU-1" in json.dumps(card, ensure_ascii=False)
    else:
        assert not calls
    with Session() as db:
        [row] = db.scalars(select(NotificationOutbox)).all()
        assert "OTHER-SKU" not in str(row.payload["card"])
        assert row.status == ("DELIVERED" if fallback else "ROUTING_FAILED")
        if not fallback:
            assert row.last_error == "missing_operations_owner"


@pytest.mark.parametrize("member", [True, False])
def test_global_exact_sku_mapping_used_only_as_configured_and_with_group_roster(setup, monkeypatch, member):
    Session, settings, config, path = setup
    settings.feishu_group_fallback_enabled = True
    settings.feishu_daily_report_chat_id = "test"
    config.update(stores={}, management={"severities": []}, entities={
        "SKU-1": {"chat_id": "operations", "open_ids": ["ou_legacy_exact"]}})
    config["groups"]["operations"]["verified_member_open_ids"] = (
        ["ou_admin", "ou_legacy_exact"] if member else ["ou_admin"])
    path.write_text(json.dumps(config), encoding="utf-8")
    _enqueue_sku_card(Session)
    calls = _capture_sku_wire(monkeypatch)
    assert worker.process_outbox_batch()["sent"] == 1
    [(chat, card)] = calls
    assert chat == ("oc_operations" if member else "oc_test")
    assert _wire_mentions(card) == (
        ["负责人：<at id=ou_admin></at> <at id=ou_legacy_exact></at>"] if member else [])


def test_missing_advertised_sku_does_not_substitute_shared_asin_for_owner(setup, monkeypatch):
    Session, settings, config, path = setup
    settings.feishu_group_fallback_enabled = True
    settings.feishu_daily_report_chat_id = "test"
    config.update(stores={}, management={"severities": []}, entities={"s:B0SHARED": ["ou_asin_owner"]})
    path.write_text(json.dumps(config), encoding="utf-8")
    with Session.begin() as db:
        db.add(Product(store_id="s", seller_sku="OTHER", asin="B0SHARED", owner_open_ids=["ou_other"]))
    _enqueue_sku_card(Session, scope="s:product_ad:sd",
                      metrics={"seller_sku": "SKU-1", "ad_id": "ad", "asin": "B0SHARED"})
    calls = _capture_sku_wire(monkeypatch)
    assert worker.process_outbox_batch()["sent"] == 1
    [(chat, card)] = calls
    assert chat == "oc_test" and not _wire_mentions(card)
    assert "商品编码 SKU-1" in json.dumps(card, ensure_ascii=False)


@pytest.mark.parametrize("actual_store,actual_sku", [("s", "sku-1"), ("s", "SKU-1 "), ("S", "SKU-1")])
def test_exact_product_rejects_sql_collation_near_matches(actual_store, actual_sku):
    from sqlalchemy import MetaData, String, event
    from jx_alert.product_identity import exact_store_product

    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def add_collation(conn, _record):
        def compare(a, b):
            a, b = a.casefold().rstrip(" "), b.casefold().rstrip(" ")
            return (a > b) - (a < b)

        conn.create_collation("CASE_AND_PAD_INSENSITIVE", compare)

    # Emulate relevant production collation behavior without depending on a
    # server or mutating the application's shared model metadata.
    table = Product.__table__.to_metadata(MetaData())
    for col in (table.c.store_id, table.c.seller_sku):
        col.type = String(col.type.length, collation="CASE_AND_PAD_INSENSITIVE")
    table.create(engine)
    with sessionmaker(bind=engine).begin() as db:
        db.add(Product(store_id=actual_store, seller_sku=actual_sku, owner_open_ids=["ou_wrong"]))
        db.flush()
        assert db.scalar(select(Product).where(Product.store_id == "s", Product.seller_sku == "SKU-1"))
        assert exact_store_product(db, "s", "SKU-1") is None
    engine.dispose()


def test_empty_scoped_msku_route_does_not_forward_store_owner_to_global_group(setup, monkeypatch):
    Session, _, config, path = setup
    # No verified_member_open_ids on either group: we cannot rely on filtering a
    # wrongly addressed card. The store's selected group is not the default.
    config["default_chat_id"] = "test"
    config["groups"]["test"]["admin_open_ids"] = ["ou_global_admin"]
    config["stores"]["s"]["chat_id"] = "operations"
    config.update(management={"severities": []}, entities={
        "s:SKU-1": {},
        "SKU-1": {"chat_id": "test", "open_ids": ["ou_global_owner"]},
    })
    path.write_text(json.dumps(config), encoding="utf-8")
    _enqueue_sku_card(Session)
    calls = _capture_sku_wire(monkeypatch)
    assert worker.process_outbox_batch()["sent"] == 1
    [(chat, card)] = calls
    assert chat == "oc_operations"
    assert _wire_mentions(card) == ["负责人：<at id=ou_admin></at> <at id=ou_operator></at>"]


def test_unused_global_msku_route_is_not_read_by_scoped_incident_planner(setup):
    Session, settings, config, path = setup
    config.update(management={"severities": []}, entities={
        "s:SKU-1": {"open_ids": ["ou_scoped"]},
        "SKU-1": {"chat_id": "unknown", "open_ids": ["not-an-id"]},
    })
    path.write_text(json.dumps(config), encoding="utf-8")
    with Session() as db:
        [destination] = application_deliveries(
            db, settings, store_id="s", entity_id="SKU-1", metrics={},
            scope="s:sales:sku_msku", severity="P1", status="NEW",
        )
    # The worker still applies its existing full-file safety validation.
    assert destination["receive_id"] == "oc_operations"
    assert destination["open_ids"] == ["ou_admin", "ou_operator", "ou_scoped"]


@pytest.mark.parametrize("kind", ["scoped_asin", "global_asin", "global_colon"])
def test_ambiguous_raw_msku_route_cannot_redirect_exact_incident_card(setup, monkeypatch, kind):
    Session, _, config, path = setup
    sku = "other:SKU-1" if kind == "global_colon" else "SKU-1"
    config["groups"]["test"]["admin_open_ids"] = ["ou_unrelated_admin"]
    config["stores"]["s"]["chat_id"] = "operations"
    wrong = {"chat_id": "test", "open_ids": ["ou_wrong"]}
    config.update(management={"severities": []}, entities={sku: wrong})
    if kind == "scoped_asin":
        config["entities"][f"s:{sku}"] = wrong
    path.write_text(json.dumps(config), encoding="utf-8")
    with Session.begin() as db:
        db.add(Product(store_id="s", seller_sku=sku, asin="B0ACTUAL", owner_open_ids=["ou_actual"]))
        if kind != "global_colon":
            db.add(Product(store_id="s" if kind == "scoped_asin" else "other",
                           seller_sku="OTHER", asin=sku, owner_open_ids=["ou_wrong"]))
    _enqueue_sku_card(Session, sku=sku)
    calls = _capture_sku_wire(monkeypatch)
    assert worker.process_outbox_batch()["sent"] == 1
    [(chat, card)] = calls
    assert chat == "oc_operations"
    assert _wire_mentions(card) == ["负责人：<at id=ou_admin></at> <at id=ou_operator></at> <at id=ou_actual></at>"]
    content = json.dumps(card, ensure_ascii=False)
    assert f"商品编码 {sku} / 商品编号 B0ACTUAL" in content
    assert "商品编码 OTHER" not in content


def test_exact_ad_card_retains_incident_asin_if_product_has_since_changed(setup, monkeypatch):
    Session, _, config, path = setup
    config["management"] = {"severities": []}
    path.write_text(json.dumps(config), encoding="utf-8")
    with Session.begin() as db:
        db.add(Product(store_id="s", seller_sku="SKU-1", asin="B0NEW", owner_open_ids=["ou_actual"]))
    _enqueue_sku_card(Session, scope="s:product_ad:sp",
                      metrics={"seller_sku": "SKU-1", "ad_id": "ad", "asin": "B0ORIGINAL"})
    calls = _capture_sku_wire(monkeypatch)
    assert worker.process_outbox_batch()["sent"] == 1
    [(chat, card)] = calls
    assert chat == "oc_operations"
    identity = json.dumps(card, ensure_ascii=False)
    assert "商品编码 SKU-1 / 商品编号 B0ORIGINAL" in identity
    assert "B0NEW" not in identity
