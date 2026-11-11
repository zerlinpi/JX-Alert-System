from datetime import datetime
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jx_alert.storage.db import Base
from jx_alert.storage import models  # noqa: F401
from jx_alert.storage.models import AnomalyEvent
from jx_alert.analysis.service import upsert_anomalies


def test_only_complete_scope_can_recover_missing_anomalies():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        row = AnomalyEvent(
            dedupe_key="store-1:sku-1:orders_decrease",
            store_id="store-1",
            rule="orders_decrease",
            severity="P1",
            status="ONGOING",
            evaluation_scope="store-1:sku-orders",
            first_seen=datetime.utcnow(),
            last_seen=datetime.utcnow(),
            metrics={"orders": 0},
            reason="order decrease",
        )
        db.add(row)
        db.commit()

        upsert_anomalies(db, "store-1", [], evaluation_scope="store-1:sku-orders",
                         scope_complete=False)
        db.commit()
        assert db.scalar(select(AnomalyEvent)).status == "ONGOING"

        upsert_anomalies(db, "store-1", [], evaluation_scope="store-1:sku-orders",
                         scope_complete=True)
        db.commit()
        recovered = db.scalar(select(AnomalyEvent))
        assert recovered.status == "RECOVERED"
        assert recovered.recovered_at is not None
