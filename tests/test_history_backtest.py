from datetime import date, timedelta

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from jx_alert.analysis.backtest import backtest_history
from jx_alert.storage.db import Base
from jx_alert.storage.models import Store, DailyMetric, DataCoverage, AnomalyEvent
from jx_alert.notifications.outbox import NotificationOutbox


def test_real_evaluator_replay_keeps_source_warehouse_and_outbox_untouched(monkeypatch):
    import jx_alert.analysis.service as service
    from jx_alert.config import Settings
    monkeypatch.setattr(service, "get_settings", lambda: Settings(_env_file=None))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = date(2026, 1, 20)
    with Session(engine) as db:
        db.add(Store(sid="1", name="Test", status=1, timezone="America/Los_Angeles"))
        for offset in range(8):
            day = target - timedelta(days=offset)
            db.add(DailyMetric(store_id="1", metric_date=day, dimension="store", entity_id="1",
                               metric_name="orders", value=0 if offset == 0 else 10))
            db.add(DataCoverage(store_id="1", metric_date=day, dimension="store",
                                status="COMPLETE", row_count=1))
        db.commit()
        result = backtest_history(db, store_id="1", start_date=str(target), end_date=str(target))
        assert result["candidate_count"] == 1
        assert result["candidates"][0]["rule"] == "orders_decrease"
        assert result["false_positive_rate"] is None
        assert db.scalar(select(func.count()).select_from(AnomalyEvent)) == 0
        assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
        key = result["candidates"][0]["key"]
        labeled = backtest_history(db, store_id="1", start_date=str(target), end_date=str(target),
                                    labels={key: "false_positive"})
        assert labeled["false_positive_rate"] == 1
