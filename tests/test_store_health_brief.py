from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.config import Settings
from jx_alert.notifications.outbox import NotificationOutbox
from jx_alert.notifications.store_health import (
    build_health_brief_cards, card_fits, enqueue_store_health_brief, health_brief_snapshots,
)
from jx_alert.storage.db import Base
from jx_alert.storage.models import Store


CLOCK = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)


@pytest.fixture
def warehouse(tmp_path):
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"store_ids": list(range(1, 65))}))
    routing = tmp_path / "routing.json"
    routing.write_text(json.dumps({"default_chat_id": "reports", "groups": {
        "reports": {"chat_id": "oc_reports"}}}))
    settings = Settings(_env_file=None, collection_scope_file=str(scope),
        feishu_routing_file=str(routing), feishu_daily_report_chat_id="reports", feishu_transport="app")
    engine = create_engine("sqlite:///" + str(tmp_path / "warehouse.sqlite"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as db:
        db.add_all([Store(sid=str(i), name="测试店铺" + str(i), status=2 if i == 64 else 1)
                    for i in range(1, 66)])
        db.commit()
    yield sessions, settings
    engine.dispose()


def test_scope_includes_missing_and_unauthorized_without_leaking_other_accounts(warehouse):
    sessions, settings = warehouse
    with sessions() as db:
        result = health_brief_snapshots(db, settings, now=CLOCK)
        assert result["store_count"] == 64
        assert {r["store_id"] for r in result["stores"]} == {str(i) for i in range(1, 65)}
        assert result["status_counts"] == {"NO_DATA": 63, "AUTHORIZATION_FAILED": 1}
        assert all(len(row["indicators"]) == 7 for row in result["stores"])
        with pytest.raises(ValueError, match="outside"):
            health_brief_snapshots(db, settings, store_id="65", now=CLOCK)
        with pytest.raises(ValueError, match="aware"):
            health_brief_snapshots(db, settings, now=CLOCK.replace(tzinfo=None))
        settings.collection_scope_file += ".missing"
        with pytest.raises(ValueError, match="readable"):
            health_brief_snapshots(db, settings, now=CLOCK)


def test_repeated_send_after_restart_preserves_original_cards_and_delivery_state(warehouse):
    sessions, settings = warehouse
    with sessions() as db:
        result = enqueue_store_health_brief(db, settings, now=CLOCK)
        rows = db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)).all()
        assert result["store_count"] == 64
        assert len({sid for row in rows for sid in row.payload["store_ids"]}) == 64
        assert all(card_fits(row.payload["card"]) for row in rows)
        frozen = json.dumps([r.payload for r in rows], sort_keys=True)
        assert all(r.payload["delivery"]["receive_id"] == "oc_reports" for r in rows)
        for row in rows:
            row.status = "DELIVERED"
        db.commit()
    with sessions() as db:
        reused = enqueue_store_health_brief(db, settings, now=CLOCK)
        assert reused["status"] == "REUSED" and reused["outbox_ids"] == result["outbox_ids"]
        rows = db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)).all()
        assert all(r.status == "DELIVERED" for r in rows)
        assert json.dumps([r.payload for r in rows], sort_keys=True) == frozen


def test_large_chinese_rows_split_to_fit_json_envelope_without_losing_stores(warehouse):
    sessions, settings = warehouse
    with sessions() as db:
        sample = health_brief_snapshots(db, settings, store_id="1", now=CLOCK)["stores"][0]
    snapshots = []
    for i in range(64):
        value = deepcopy(sample)
        value.update(store_id=str(i + 1), store_name="测" * 200, indicator_summary="待" * 500)
        snapshots.append(value)
    pages = build_health_brief_cards(snapshots, report_day="2026-11-13")
    assert len(pages) > 7
    assert {sid for _, ids in pages for sid in ids} == {str(i) for i in range(1, 65)}
    assert all(card_fits(card) for card, _ in pages)


def test_mcp_query_reports_complete_scope_counts_and_pages_without_sending(warehouse, monkeypatch):
    import jx_alert.mcp_server as server
    sessions, settings = warehouse
    monkeypatch.setattr(server, "SessionLocal", sessions)
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    seen = set()
    offset = 0
    while offset is not None:
        result = server.get_store_health_brief(offset=offset)
        assert result["store_count"] == 64
        assert len(result["stores"]) <= 20
        seen.update(r["store_id"] for r in result["stores"])
        offset = result["next_offset"]
    assert len(seen) == 64
    assert server.get_store_health_brief(store_id="64")["stores"][0]["status"] == "AUTHORIZATION_FAILED"
    with sessions() as db:
        assert db.scalar(select(NotificationOutbox)) is None
    with pytest.raises(ValueError):
        server.get_store_health_brief(limit=101)
