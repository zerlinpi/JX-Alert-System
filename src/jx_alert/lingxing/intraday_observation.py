"""Explicit private-chat SP observations, isolated from authoritative daily facts."""
from datetime import date, datetime, timezone
import math
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from jx_alert.storage.models import Campaign, RawSnapshot, Store, SyncJob
from jx_alert.store_controls import collection_states


SOURCE = "lingxing_sp_intraday_observation"


def _selected(db, settings, sid, *, now=None):
    from jx_alert.operations import _checked_scope, store_report_plan, CollectionBlocked
    _checked_scope(settings, [sid])
    store = db.get(Store, sid)
    if store is None:
        raise CollectionBlocked("STORE_NOT_SYNCED")
    if not collection_states(settings, [sid])[sid]:
        raise CollectionBlocked("SKIPPED_COLLECTION_DISABLED")
    plan = store_report_plan([{"sid": sid, "status": store.status}], settings.store_timezones_file,
                             now=now)[0]
    if plan["state"] != "READY" or not plan["intraday_enabled"]:
        raise CollectionBlocked(plan["state"])
    return store, plan


def enqueue_observation(db, settings, sid, *, now=None):
    clock = now or datetime.now(timezone.utc)
    _, plan = _selected(db, settings, sid, now=clock)
    day = clock.astimezone(ZoneInfo(plan["timezone"])).date().isoformat()
    for job in db.scalars(select(SyncJob).where(SyncJob.job_type == "intraday_observation",
            SyncJob.status.in_(["PENDING", "RUNNING"])).order_by(SyncJob.id.desc())):
        args = job.metadata_json or {}
        if args.get("store_ids") == [sid] and args.get("observation_date") == day:
            return {"job_id": job.id, "status": job.status, "reused": True}
    job = SyncJob(job_type="intraday_observation", status="PENDING", started_at=clock.astimezone(timezone.utc).replace(tzinfo=None),
        metadata_json={"store_ids": [sid], "observation_date": day,
                       "attempts": 0, "observation_only": True})
    db.add(job)
    db.flush()
    return {"job_id": job.id, "status": job.status, "reused": False}


def _excel(settings, snapshot):
    if not getattr(settings, "excel_archive_enabled", False):
        return None
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from jx_alert.reporting.daily_snapshot import _cell_value
    day = date.fromisoformat(snapshot.payload["observation_date"])
    folder = Path(settings.excel_archive_dir) / "intraday" / day.strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    target = folder / f"SP-{snapshot.store_id}-{snapshot.id}.xlsx"
    headers = ["店铺ID", "观测日期", "采集时间UTC", "广告活动ID", "活动名称", "花费", "归因订单观测",
               "点击", "曝光", "币种", "口径", "来源"]
    book = Workbook(write_only=True)
    sheet = book.create_sheet("SP盘中观测")
    sheet.append(headers)
    for row in snapshot.payload["rows"]:
        values = [snapshot.store_id, day.isoformat(), snapshot.captured_at.isoformat(),
            str(row.get("campaign_id", "")), row.get("campaign_name"), row.get("cost"),
            row.get("orders"), row.get("clicks"), row.get("impressions"), row.get("confirmed_currency"),
            "盘中观测，非完整日报；归因订单仍会更新", row.get("_source") or "lingxing_openapi"]
        cells = []
        for value in values:
            cell = WriteOnlyCell(sheet, _cell_value(value))
            if isinstance(cell.value, str):
                cell.data_type = "s"
            cells.append(cell)
        sheet.append(cells)
    fd, temporary = tempfile.mkstemp(dir=folder, suffix=".xlsx")
    os.close(fd)
    try:
        book.save(temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        book.close()
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(target)


def run_observation(db, settings, args, *, client=None, now=None):
    """Read one requested day; never update DailyMetric, coverage, ads or alerts."""
    from jx_alert.operations import CollectionBlocked, CollectionConfigurationError
    from jx_alert.lingxing.ad_currency import account_currencies, advertising_currency
    from jx_alert.storage.raw_archive import archive_raw_snapshots
    selected = args.get("store_ids")
    if not isinstance(selected, list) or len(selected) != 1:
        raise CollectionConfigurationError("Observation requires one store")
    sid = selected[0]
    clock = now or datetime.now(timezone.utc)
    store, plan = _selected(db, settings, sid, now=clock)
    day = args.get("observation_date")
    try:
        on = date.fromisoformat(day)
        if on > clock.astimezone(ZoneInfo(plan["timezone"])).date():
            raise ValueError
    except (TypeError, ValueError):
        raise CollectionConfigurationError("Invalid observation date") from None
    if client is None:
        from jx_alert.lingxing.source_gateway import create_collection_client
        client = create_collection_client(settings, db=db, observation_mode=True)
    before = db.scalar(select(func.max(RawSnapshot.id))) or 0
    inventory = client.get_stores()
    candidates = inventory.get("data") if isinstance(inventory, dict) else None
    match = next((r for r in candidates if isinstance(r, dict) and str(r.get("sid")) == sid), None) if isinstance(candidates, list) else None
    if match is None or int(match.get("status") or 0) != 1:
        raise CollectionBlocked("AUTHORIZATION_FAILED")
    if int(match.get("has_ads_setting") or 0) != 1:
        raise CollectionBlocked("ADS_NOT_AUTHORIZED")
    currencies = {}
    if hasattr(client, "seller_ad_accounts"):
        try:
            currencies = account_currencies(client.seller_ad_accounts())
        except Exception:
            pass  # Unconfirmed monetary values remain hidden in operator replies.
    profiles = {r.campaign_id: r.profile_id for r in db.scalars(select(Campaign).where(
        Campaign.store_id == sid, Campaign.campaign_type == "sp"))}
    rows = []
    for row in client.iter_advertising_report("sp", {"sid": int(sid), "report_date": day, "show_detail": 1},
                                             max_pages=50):
        _selected(db, settings, sid, now=clock)  # Re-read the owner switch during page traversal.
        if (not isinstance(row, dict) or row.get("report_date") != day
                or not row.get("campaign_id") or (row.get("sid") and str(row["sid"]) != sid)):
            raise ValueError("Invalid observation grain")
        for metric in ("cost", "orders", "clicks", "impressions"):
            value = row.get(metric)
            if value is not None and (isinstance(value, bool) or not math.isfinite(float(value)) or float(value) < 0):
                raise ValueError("Invalid observation metric")
        code = advertising_currency(currencies, sid, row, campaign_profile=profiles.get(str(row["campaign_id"])))
        declared = row.get("currency_code") or row.get("currency")
        if isinstance(declared, str) and re.fullmatch(r"[A-Z]{3}", declared):
            code = declared if code is None or code == declared else None
        rows.append({**row, "confirmed_currency": code})
    _selected(db, settings, sid, now=clock)
    if hasattr(client, "flush"):
        client.flush(db, raw_only=True)
    snapshot = RawSnapshot(source=SOURCE, store_id=sid, captured_at=clock.astimezone(timezone.utc).replace(tzinfo=None),
        payload={"observation_only": True, "observation_date": day, "store_name": store.name,
                 "report_timezone": plan["timezone"], "availability": "AVAILABLE" if rows else "EMPTY", "rows": rows,
                 "sources": sorted({r.get("_source") or "lingxing_openapi" for r in rows})})
    db.add(snapshot)
    db.commit()
    path = _excel(settings, snapshot)
    raw = archive_raw_snapshots(db, settings.raw_dir, after_id=before)
    return {"status": "COMPLETE", "observation_only": True, "observation_date": day,
        "snapshot_id": snapshot.id, "row_count": len(rows), "availability": snapshot.payload["availability"],
        "excel_file": path, "raw_archive": raw}


def latest_observation(db, sid, day):
    snapshot = db.scalar(select(RawSnapshot).where(RawSnapshot.store_id == sid, RawSnapshot.source == SOURCE,
        RawSnapshot.payload["observation_date"].as_string() == day).order_by(RawSnapshot.id.desc()).limit(1))
    latest = db.scalar(select(SyncJob).where(SyncJob.job_type == "intraday_observation",
        SyncJob.metadata_json["store_ids"][0].as_string() == sid,
        SyncJob.metadata_json["observation_date"].as_string() == day).order_by(SyncJob.id.desc()).limit(1))
    if (snapshot is not None and latest is not None and latest.started_at > snapshot.captured_at
            and latest.status not in {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"}):
        return None
    return snapshot
