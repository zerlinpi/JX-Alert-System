"""Backfill independent business sources and archives without sending messages."""
import argparse
from datetime import date, datetime, timedelta, timezone
import json
import re
from zoneinfo import ZoneInfo

from sqlalchemy import select, func

from jx_alert.config import get_settings
from jx_alert.lingxing.api_business import collect_api_business_for_day
from jx_alert.lingxing.business_dashboard import collect_business_for_day
from jx_alert.lingxing.client import LingxingClient
from jx_alert.operations import scoped_stores
from jx_alert.reporting.archive import archive_partitions
from jx_alert.storage.db import SessionLocal, engine
from jx_alert.storage.job_lock import database_job_lock
from jx_alert.storage.models import RawSnapshot, SyncJob, Store
from jx_alert.storage.raw_archive import archive_raw_snapshots
from jx_alert.store_controls import collection_states


def backfill(report_date, source, store_ids=None):
    on = date.fromisoformat(report_date)
    now = datetime.now(timezone.utc)
    if on.isoformat() != report_date or on >= now.astimezone(ZoneInfo('America/Los_Angeles')).date():
        raise ValueError('请选择已经结束的美国报表日')
    if source not in {'api', 'mcp', 'both'}:
        raise ValueError('无效数据源')
    settings = get_settings()
    if settings.app_dry_run:
        return {'status': 'DRY_RUN'}
    with database_job_lock(engine), SessionLocal() as db:
        client, inventory, api_error = None, None, None
        if source in {'api', 'both'}:
            try:
                client = LingxingClient.from_settings()
                inventory = client.get_stores()
            except Exception as exc:
                client = None
                code = re.search(r'(?:code|status)[=:]\s*([0-9-]+)', str(exc))
                api_error = {'error_type': type(exc).__name__, 'provider_code': code.group(1) if code else None}
        cached_identity = inventory is None
        if inventory is None:
            # Cached identity only selects an existing scope. MCP still checks
            # its actual visible stores; this path never evaluates auth events.
            inventory = {'data': [{'sid': r.sid, 'name': r.name, 'status': r.status}
                for r in db.scalars(select(Store))]}
        inventory = scoped_stores(inventory, settings.collection_scope_file, allow_missing=True)
        enabled = collection_states(settings, [str(row['sid']) for row in inventory['data']])
        selected = [row for row in inventory['data'] if int(row.get('status') or 0) == 1
                    and enabled.get(str(row['sid'])) and (not store_ids or str(row['sid']) in store_ids)]
        if store_ids and set(store_ids) - {str(row['sid']) for row in selected}:
            raise ValueError('指定店铺未授权、已暂停或不在采集范围')
        if not selected:
            return {'status': 'NO_ACTIVE_STORES'}
        job = SyncJob(job_type='business_backfill', status='RUNNING', started_at=now.replace(tzinfo=None))
        db.add(job)
        db.commit()
        job_id = job.id
        before = db.scalar(select(func.max(RawSnapshot.id))) or 0
        result = {'report_date': report_date, 'source': source, 'store_count': len(selected),
                  'cached_store_identity': cached_identity}
        if api_error:
            result['api_initialization_error'] = api_error
        try:
            stores = {**inventory, 'data': selected}
            if source in {'api', 'both'}:
                result['api'] = collect_api_business_for_day(client, db, settings, stores, report_date)
            if source in {'mcp', 'both'}:
                result['mcp'] = collect_business_for_day(db, settings, stores, report_date)
            dates = {str(row['sid']): [(on-timedelta(days=1)).isoformat(), report_date] for row in selected}
            result['excel'] = archive_partitions(db, settings, dates)
            result['raw_archive'] = archive_raw_snapshots(db, settings.raw_dir, after_id=before)
            result['status'] = 'PARTIAL_FAILED' if any(result[k].get('status') != 'COMPLETE'
                for k in ('api', 'mcp') if k in result) else 'COMPLETE'
            job = db.get(SyncJob, job_id)
            job.status = 'PARTIAL_FAILED' if result['status'] == 'PARTIAL_FAILED' else 'SUCCEEDED'
            job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            job.store_count = len(selected)
            job.metadata_json = result
            db.commit()
            return {'job_id': job_id, **result}
        except Exception as exc:
            db.rollback()
            job = db.get(SyncJob, job_id)
            job.status = 'FAILED'
            job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            job.metadata_json = {'report_date': report_date, 'source': source, 'error_type': type(exc).__name__}
            db.commit()
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='经营数据补采，不发送飞书通知')
    parser.add_argument('--report-date', required=True, help='美国统计日，YYYY-MM-DD')
    parser.add_argument('--source', choices=['api', 'mcp', 'both'], default='both')
    parser.add_argument('--store-id', action='append', dest='store_ids')
    args = parser.parse_args()
    try:
        result = backfill(args.report_date, args.source, args.store_ids)
        print(json.dumps(result, ensure_ascii=False))
        if result.get('status') in {'PARTIAL_FAILED', 'FAILED'}:
            raise SystemExit(2)
    except Exception as exc:
        print(json.dumps({'status': 'FAILED', 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None
