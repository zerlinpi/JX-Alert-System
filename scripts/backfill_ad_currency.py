"""Preview or fill missing advertising currencies from exact archived report rows."""
import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import gzip
import json
import os
from pathlib import Path
import tempfile

from sqlalchemy import select, update

from jx_alert.config import get_settings
from jx_alert.lingxing.ad_currency import account_currencies, advertising_currency, is_ad_money
from jx_alert.lingxing.client import LingxingClient
from jx_alert.lingxing.report_metrics import AD_REPORT_METRICS
from jx_alert.storage.db import SessionLocal, engine
from jx_alert.storage.job_lock import database_job_lock
from jx_alert.storage.models import Campaign, DailyMetric, RawSnapshot


SOURCES = {
    **{f"lingxing_ads_{kind}_campaign_report": f"campaign_{kind}" for kind in ("sp", "sb", "sd")},
    **{f"lingxing_{kind}_product_ad_report": f"product_ad_{kind}" for kind in ("sp", "sd")},
}


def archived_money(db, sid, currencies):
    profiles = {(row.campaign_type, row.campaign_id): row.profile_id for row in db.scalars(
        select(Campaign).where(Campaign.store_id == sid)
    )}
    snapshots = db.scalars(select(RawSnapshot).where(
        RawSnapshot.store_id == sid, RawSnapshot.source.in_(SOURCES),
    ).order_by(RawSnapshot.id.desc()).execution_options(yield_per=50))
    seen, evidence = set(), {}
    for snapshot in snapshots:
        dimension = SOURCES[snapshot.source]
        raw_date = snapshot.payload.get("report_date")
        if not raw_date:
            continue
        day = date.fromisoformat(raw_date)
        if (dimension, day) in seen:
            continue
        seen.add((dimension, day))
        kind = dimension.rsplit("_", 1)[-1]
        rows = snapshot.payload.get("rows", [])
        entity_field = "ad_id" if dimension.startswith("product_ad_") else "campaign_id"
        counts = Counter(str(row.get(entity_field)) for row in rows)
        for row in rows:
            entity = str(row.get(entity_field))
            if entity == "None" or counts[entity] != 1 or row.get("report_date") != raw_date:
                continue
            code = advertising_currency(currencies, sid, row,
                campaign_profile=profiles.get((kind, str(row.get("campaign_id")))))
            if not code:
                continue
            for field, metric in AD_REPORT_METRICS.items():
                if is_ad_money(metric) and row.get(field) is not None:
                    evidence[dimension, day, entity, metric] = (float(row[field]), code, snapshot.id)
    return evidence


def backfill(db, accounts, *, backup=None):
    currencies = account_currencies(accounts)
    if not currencies:
        raise ValueError("No confirmed seller advertising account currencies")
    db.add(RawSnapshot(source="lingxing_seller_ad_accounts", payload={"rows": accounts}))
    counts = Counter()
    for sid in sorted({sid for sid, _ in currencies}):
        candidates = db.execute(select(DailyMetric.id, DailyMetric.dimension, DailyMetric.metric_date,
            DailyMetric.entity_id, DailyMetric.metric_name, DailyMetric.value).where(
            DailyMetric.store_id == sid, DailyMetric.dimension.in_(SOURCES.values()),
            DailyMetric.metric_name.in_({m for m in AD_REPORT_METRICS.values() if is_ad_money(m)}),
            DailyMetric.currency.is_(None),
        )).all()
        if not candidates:
            continue
        evidence = archived_money(db, sid, currencies)
        updates = defaultdict(list)
        for row_id, dimension, day, entity, metric, value in candidates:
            match = evidence.get((dimension, day, entity, metric))
            if match is None or match[0] != value:
                counts["unresolved"] += 1
                continue
            _, code, snapshot_id = match
            counts[code] += 1
            if backup is not None:
                backup.write(json.dumps({"id": row_id, "currency_before": None, "currency_after": code,
                                         "report_snapshot_id": snapshot_id}) + "\n")
                updates[code].append(row_id)
        for code, ids in updates.items():
            for offset in range(0, len(ids), 1000):
                db.execute(update(DailyMetric).where(DailyMetric.id.in_(ids[offset:offset + 1000]),
                    DailyMetric.currency.is_(None)).values(currency=code),
                    execution_options={"synchronize_session": False})
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    accounts = LingxingClient.from_settings().seller_ad_accounts()
    with database_job_lock(engine), SessionLocal() as db:
        if not args.apply:
            print(json.dumps({"status": "PREVIEW", "matched_facts": backfill(db, accounts)}))
            return
        directory = Path(get_settings().raw_dir) / "maintenance"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        fd, pending = tempfile.mkstemp(prefix=".ad-currency-", dir=directory)
        with os.fdopen(fd, "wb") as stream:
            with gzip.open(stream, "wt", encoding="utf-8") as backup:
                counts = backfill(db, accounts, backup=backup)
            stream.flush()
            os.fsync(stream.fileno())
        path = directory / f"ad-currency-{stamp}.jsonl.gz"
        os.replace(pending, path)
        db.commit()
        print(json.dumps({"status": "APPLIED", "updated_facts": counts, "backup": str(path)}))


if __name__ == "__main__":
    main()
