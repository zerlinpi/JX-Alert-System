"""A separate single-owner browser session for store data and Feishu routing."""
import asyncio
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import html
from importlib.resources import files
import json
import logging
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from jx_alert import ad_source_controls, admin_ad_subscribers, admin_data, admin_executive, admin_groups
from jx_alert import admin_mapping, admin_sources, sparkx_config, sparkx_reports, store_controls
from jx_alert import admin_review_skills, admin_skill_library
from jx_alert.agent_skill_package import MAX_REQUEST as SKILL_UPLOAD_LIMIT
from jx_alert.ai import deepseek_config
from jx_alert.lingxing.source_credentials import SourceCredentialsError
from jx_alert.mcp_oauth import SqliteOAuthProvider, _private_file
from jx_alert.notifications.receipt import unique_json_fields
from jx_alert.storage.db import SessionLocal
from jx_alert.storage.models import Store, SyncJob


SESSION = "__Host-jx-console-session"
LOGIN_CSRF = "__Host-jx-console-login"
SESSION_TTL = 1800
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_SID = re.compile(r"[1-9][0-9]{0,29}")
_LOG = logging.getLogger(__name__)
_HEADERS = {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin", "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                               "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _csrf(session):
    return _digest("jx-console-csrf:" + session)


def _cookie(response, name, value, ttl):
    response.set_cookie(name, value, max_age=ttl, path="/", secure=True, httponly=True, samesite="strict")


def _error(message, status):
    return JSONResponse({"error": message}, status, headers=_HEADERS)


class ConsoleError(Exception):
    def __init__(self, detail, status):
        super().__init__(detail)
        self.detail, self.status = detail, status


async def _body(request, media, limit=8192, *, strict_json=False):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != media:
        raise ConsoleError("请求格式无效。", 415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise ConsoleError("请求内容过长。", 413)
        body.extend(chunk)
    try:
        if media == "application/json":
            result = json.loads(body, object_pairs_hook=unique_json_fields if strict_json else None)
            if not isinstance(result, dict):
                raise ValueError
            return result
        values = parse_qsl(body.decode(), keep_blank_values=True, max_num_fields=4, strict_parsing=True)
        if len(dict(values)) != len(values):
            raise ValueError
        return dict(values)
    except (ValueError, UnicodeError, RecursionError):
        raise ConsoleError("请求格式无效。", 400) from None


class OwnerConsole:
    def __init__(self, settings, provider, *, session_factory=SessionLocal):
        self.settings = settings
        self.verify_password = provider._password_valid
        url = urlsplit(provider.issuer_url)
        self.origin = f"{url.scheme}://{url.netloc}"
        self.host = url.netloc.lower()
        self.session_factory = session_factory
        self.database = Path(provider.database_path).with_name("console-sessions.sqlite3")
        if self.database == Path(provider.database_path):
            raise ValueError("Console and OAuth require separate databases")
        self._limits = {}
        self._limit_lock = threading.Lock()

    def _rate_allowed(self, request, bucket, *, per_ip, total):
        minute = int(time.time()) // 60
        address = request.client.host if request.client else "unknown"
        keys = ((bucket, "total"), (bucket, address[:100]))
        with self._limit_lock:
            self._limits = {key: v for key, v in self._limits.items() if v[0] == minute}
            if any(self._limits.get(key, (minute, 0))[1] >= maximum
                   for key, maximum in zip(keys, (total, per_ip))):
                return False
            for key in keys:
                self._limits[key] = (minute, self._limits.get(key, (minute, 0))[1] + 1)
        return True

    def _new_session(self):
        _private_file(self.database, create=True)
        token = secrets.token_urlsafe(32)
        now = time.time()
        with closing(sqlite3.connect(self.database, timeout=5)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, expires REAL NOT NULL)")
            db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            db.execute("INSERT INTO sessions VALUES (?, ?)", (_digest(token), now + SESSION_TTL))
        return token

    def _session(self, token, *, remove=False):
        if not _TOKEN.fullmatch(token) or not self.database.exists():
            return False
        _private_file(self.database)
        with closing(sqlite3.connect(self.database, timeout=5)) as db, db:
            if remove:
                db.execute("DELETE FROM sessions WHERE id = ?", (_digest(token),))
                return True
            found = db.execute("SELECT 1 FROM sessions WHERE id=? AND expires>?",
                               (_digest(token), time.time())).fetchone()
        return found is not None

    def _valid_host(self, request):
        return request.url.scheme == "https" and request.headers.getlist("host") == [self.host]

    def _valid_origin(self, request):
        return request.headers.getlist("origin") == [self.origin]

    def _asset(self, name):
        return files("jx_alert").joinpath("admin_ui", name).read_text(encoding="utf-8")

    def _login_page(self, error="", status=200):
        token = secrets.token_urlsafe(32)
        page = self._asset("login.html").replace("{{CSRF}}", token).replace("{{ERROR}}", html.escape(error))
        response = HTMLResponse(page, status, headers=_HEADERS)
        _cookie(response, LOGIN_CSRF, token, 600)
        return response

    async def home(self, request):
        if not self._valid_host(request):
            return _error("请通过管理台的 HTTPS 地址访问。", 400)
        token = request.cookies.get(SESSION, "")
        if not await asyncio.to_thread(self._session, token):
            return RedirectResponse("/console/login", 303, headers=_HEADERS)
        return HTMLResponse(self._asset("index.html").replace("{{CSRF}}", _csrf(token)), headers=_HEADERS)

    async def login(self, request):
        if not self._valid_host(request):
            return _error("请通过管理台的 HTTPS 地址访问。", 400)
        if request.method == "GET":
            if await asyncio.to_thread(self._session, request.cookies.get(SESSION, "")):
                return RedirectResponse("/console/", 303, headers=_HEADERS)
            return self._login_page()
        if not self._valid_origin(request):
            return _error("请求来源无效，请重新打开登录页面。", 403)
        if not self._rate_allowed(request, "login", per_ip=6, total=30):
            return self._login_page("登录尝试过多，请一分钟后重试。", 429)
        try:
            form = await _body(request, "application/x-www-form-urlencoded")
        except ConsoleError as exc:
            return self._login_page(exc.detail, exc.status)
        supplied = form.get("csrf", "")
        expected = request.cookies.get(LOGIN_CSRF, "")
        if not _TOKEN.fullmatch(supplied) or not hmac.compare_digest(supplied, expected):
            return self._login_page("登录页面已过期，请重试。", 403)
        password = form.get("password", "")
        if not 16 <= len(password) <= 1024 or not await asyncio.to_thread(self.verify_password, password):
            return self._login_page("口令不正确，请重试。", 403)
        token = await asyncio.to_thread(self._new_session)
        response = RedirectResponse("/console/", 303, headers=_HEADERS)
        _cookie(response, SESSION, token, SESSION_TTL)
        _cookie(response, LOGIN_CSRF, "", 0)
        return response

    async def asset(self, request):
        name = request.path_params["asset"]
        mime = {"console.css": "text/css", "console.js": "text/javascript",
                "review-skills.js": "text/javascript", "skill-library.js": "text/javascript"}
        if not self._valid_host(request) or name not in mime:
            return _error("资源不存在。", 404)
        return Response(self._asset(name), media_type=mime[name], headers=_HEADERS)

    def _stores_data(self):
        with self.session_factory() as db:
            data = admin_data.store_overview(db, self.settings, admin_data.scoped_ids(self.settings))
        ids = [s["sid"] for s in data["stores"]]
        try:
            controls = store_controls.store_control_snapshots(self.settings, ids)
        except store_controls.StoreControlError as exc:
            controls = {sid: self._unknown_controls(exc) for sid in ids}
        for row in data["stores"]:
            row["controls"] = controls[row["sid"]]
        return data

    @staticmethod
    def _unknown_controls(exc):
        return {"collection_enabled": None, "notifications_enabled": None,
                "revision": None, "updated_at": None, "error": exc.detail}

    def _selected(self, sid):
        if not _SID.fullmatch(sid) or sid not in admin_data.scoped_ids(self.settings):
            raise ConsoleError("店铺不在已配置范围内。", 404)

    def _read_store(self, sid, selected_date):
        self._selected(sid)
        with self.session_factory() as db:
            store = db.get(Store, sid)
            if store is None:
                raise ConsoleError("该店铺尚未完成同步。", 404)
            try:
                mapping = admin_mapping.get_store_mapping(self.settings, sid)
                owner_count = len(mapping["owners"])
            except admin_mapping.MappingError as exc:
                mapping = {"owners": [], "group": None, "revision": None, "error": exc.detail}
                owner_count = None
            date_value = None
            if selected_date:
                try:
                    date_value = date.fromisoformat(selected_date)
                    if date_value.isoformat() != selected_date:
                        raise ValueError
                except ValueError:
                    raise ConsoleError("请选择有效的报表日期。", 400) from None
            try:
                data = admin_data.store_detail(db, self.settings, store, requested=date_value)
            except ValueError:
                raise ConsoleError("所选日期尚未结束，或店铺报表时区待确认。", 400) from None
            try:
                controls = store_controls.get_store_controls(self.settings, sid)
            except store_controls.StoreControlError as exc:
                controls = self._unknown_controls(exc)
            return {"store": admin_data.store_row(sid, store, {"owner_count": owner_count,
                     "group": mapping["group"]}), **data, "routing": mapping, "controls": controls,
                    "collection": self._collection_receipt(db, sid)}

    @staticmethod
    def _collection_receipt(db, sid):
        """Return only the selected store's durable, bounded manual-job status."""
        labels = {"PENDING": "等待采集", "RUNNING": "正在同步", "SUCCEEDED": "同步完成",
                  "SUCCEEDED_WITH_WARNINGS": "同步完成，部分数据待核查", "PARTIAL_FAILED": "部分采集失败",
                  "FAILED": "同步失败", "SKIPPED": "已停止同步", "BLOCKED": "同步条件待核实"}
        candidates = db.scalars(select(SyncJob).where(SyncJob.job_type == 'manual_collection',
            SyncJob.metadata_json['store_ids'][0].as_string() == sid).order_by(SyncJob.id.desc()).limit(100))
        for job in candidates:
            args = job.metadata_json or {}
            if args.get('store_ids') != [sid]:
                continue
            result = args.get('result') if isinstance(args.get('result'), dict) else {}
            dates = result.get('report_dates') or args.get('planned_store_dates') or {}
            due = dates.get(sid) if isinstance(dates, dict) else None
            retry = args.get('retry_at') if job.status == 'PENDING' else None
            return {'job_id': job.id, 'status': job.status,
                    'status_label': '等待重试' if retry else labels.get(job.status, '状态待确认'),
                    'report_date': due, 'retry_at': retry}
        return {'job_id': None, 'status': 'NONE', 'status_label': '尚未手动同步', 'report_date': None, 'retry_at': None}

    def _collection(self, sid, *, enqueue=False):
        self._selected(sid)
        from jx_alert.operations import enqueue_collection, CollectionConfigurationError, CollectionBlocked
        if enqueue and (self.settings.app_dry_run or not self.settings.scheduler_enabled
                        or not self.settings.mcp_operations_enabled):
            raise ConsoleError('手动同步服务尚未开启，请先核实调度和主动采集开关。', 409)
        with self.session_factory() as db:
            if db.get(Store, sid) is None:
                raise ConsoleError('该店铺尚未完成同步。', 404)
            if enqueue:
                try:
                    queued = enqueue_collection(db, store_ids=[sid], backfill_days=2)
                except CollectionBlocked as exc:
                    labels = {'SKIPPED_COLLECTION_DISABLED': '该店铺采集已关闭，请先开启数据采集。',
                              'AUTHORIZATION_FAILED': '该店铺授权失败，请先恢复领星授权。',
                              'STORE_UNAVAILABLE': '该店铺未启用，无法开始同步。',
                              'WAITING_TIMEZONE': '店铺报表时区待确认，尚不能同步最新完整日报。'}
                    raise ConsoleError(labels.get(exc.status, '该店铺采集条件未满足，请核实授权、范围和时区。'), 409) from None
                except CollectionConfigurationError:
                    raise ConsoleError('采集范围或报表日期配置待核实，暂未开始同步。', 409) from None
                db.commit()
                receipt = self._collection_receipt(db, sid)
                return {**receipt, 'reused': queued['reused']}
            return self._collection_receipt(db, sid)

    def _bulk_selection(self, db):
        """Choose only currently collectable scoped stores, preserving both switches."""
        from jx_alert.operations import store_report_plan
        ids = sorted(admin_data.scoped_ids(self.settings), key=int)
        stores = {s.sid: s for s in db.scalars(select(Store).where(Store.sid.in_(ids)))}
        switches = store_controls.collection_states(self.settings, ids)
        try:
            plan = store_report_plan([{'sid': sid, 'status': stores[sid].status} for sid in ids if sid in stores],
                self.settings.store_timezones_file,
                allow_unknown_timezone_history=getattr(self.settings, 'allow_unconfigured_timezone_history', False))
        except (OSError, ValueError, RuntimeError):
            raise ConsoleError('采集范围或报表日期配置待核实，暂未开始同步。', 409) from None
        states = {row['store_id']: row['state'] for row in plan}
        selected, skipped = [], []
        for sid in ids:
            reason = ('采集已关闭' if not switches[sid] else '尚未同步店铺' if sid not in stores else
                      '授权失败' if stores[sid].status == 2 else '店铺未启用' if stores[sid].status != 1 else
                      '报表日期或时区待确认' if states.get(sid) != 'READY' else None)
            if reason:
                skipped.append({'store_id': sid, 'reason': reason})
            else:
                selected.append(sid)
        return selected, skipped

    def _bulk_receipt(self, db, job=None, *, skipped=None):
        """Expose bounded per-store progress, never raw task results or provider text."""
        scope = set(admin_data.scoped_ids(self.settings))
        labels = {'PENDING': '等待采集', 'RUNNING': '正在同步', 'SUCCEEDED': '同步完成',
                  'SUCCEEDED_WITH_WARNINGS': '同步完成，部分数据待核查', 'PARTIAL_FAILED': '部分采集失败',
                  'FAILED': '同步失败', 'SKIPPED': '已停止同步', 'BLOCKED': '同步条件待核实',
                  'COMPLETE': '同步完成', 'COMPLETE_WITH_WARNINGS': '同步完成，部分数据待核查',
                  'REUSED_COMPLETED_CHECKPOINT': '已完成，沿用检查点', 'PARTIAL': '部分数据待重试',
                  'AWAITING_RESULT': '等待逐店结果',
                  'SKIPPED_COLLECTION_DISABLED': '采集已关闭'}
        args = (job.metadata_json or {}) if job else {}
        result = args.get('result') if isinstance(args.get('result'), dict) else {}
        selected = [sid for sid in args.get('store_ids', []) if sid in scope]
        omitted = skipped or []
        omitted = [row for row in omitted if row.get('store_id') in scope]
        dates = result.get('report_dates') or args.get('planned_store_dates') or {}
        completed = args.get('completed_store_dates') or result.get('completed_store_dates') or {}
        statuses = args.get('store_statuses') or {}
        observations = result.get('stores') or {}
        stores = {s.sid: s.name for s in db.scalars(select(Store).where(Store.sid.in_(selected)))}
        switches = store_controls.collection_states(self.settings, selected)
        rows = []
        for sid in selected:
            prior = observations.get(sid) if isinstance(observations.get(sid), dict) else {}
            status = ('SKIPPED_COLLECTION_DISABLED' if not switches[sid] else
                      'COMPLETE' if sid in completed else statuses.get(sid) or prior.get('status') or
                      ('AWAITING_RESULT' if job.status == 'RUNNING' else job.status))
            rows.append({'store_id': sid, 'store_name': stores.get(sid, '尚未同步店铺'),
                         'status': status, 'status_label': labels.get(status, '状态待确认'),
                         'report_date': dates.get(sid) if isinstance(dates, dict) else None})
        done = sum(row['status'] in {'COMPLETE', 'COMPLETE_WITH_WARNINGS', 'REUSED_COMPLETED_CHECKPOINT',
                                    'SUCCEEDED', 'SUCCEEDED_WITH_WARNINGS'} for row in rows)
        paused = sum(row['status'] == 'SKIPPED_COLLECTION_DISABLED' for row in rows)
        counts = {}
        for row in omitted:
            counts[row['reason']] = counts.get(row['reason'], 0) + 1
        retry = args.get('retry_at') if job is not None and job.status == 'PENDING' else None
        return {'job_id': job.id if job else None, 'status': job.status if job else 'NONE',
                'status_label': '等待重试' if retry else labels.get(job.status, '尚未批量同步') if job else '尚未批量同步',
                'selected_store_count': len(selected), 'completed_store_count': done,
                'remaining_store_count': len(rows) - done - paused,
                'paused_store_count': paused, 'skipped_store_count': len(omitted),
                'skipped_reason_counts': counts, 'stores': rows, 'retry_at': retry}

    def _bulk_collection(self, *, enqueue=False):
        from jx_alert.operations import enqueue_collection, CollectionConfigurationError
        from jx_alert.storage.job_lock import database_job_lock, JobAlreadyRunning
        if enqueue and (self.settings.app_dry_run or not self.settings.scheduler_enabled
                        or not self.settings.mcp_operations_enabled):
            raise ConsoleError('手动同步服务尚未开启，请先核实调度和主动采集开关。', 409)
        with self.session_factory() as db:
            request = aliased(SyncJob)
            query = select(SyncJob, request.metadata_json).join(request,
                request.metadata_json['collection_job_id'].as_integer() == SyncJob.id).where(
                request.job_type == 'console_bulk_collection', SyncJob.job_type == 'manual_collection'
            ).order_by(request.id.desc()).limit(1)
            if not enqueue:
                latest = db.execute(query).first()
                return self._bulk_receipt(db, latest[0], skipped=latest[1]['skipped']) if latest else self._bulk_receipt(db)
            try:
                with database_job_lock(db.get_bind(), 'console_bulk_submit'):
                    active = db.execute(query.where(SyncJob.status.in_(['PENDING', 'RUNNING']))).first()
                    if active is not None:
                        return {**self._bulk_receipt(db, active[0], skipped=active[1]['skipped']), 'reused': True}
                    selected, skipped = self._bulk_selection(db)
                    if not selected:
                        return {**self._bulk_receipt(db, skipped=skipped), 'status': 'NO_ELIGIBLE',
                                'status_label': '没有可同步店铺', 'reused': False}
                    queued = enqueue_collection(db, store_ids=selected, backfill_days=2)
                    job = db.get(SyncJob, queued['job_id'])
                    # Keep the receipt independent of worker-owned progress, including reused jobs.
                    db.add(SyncJob(job_type='console_bulk_collection', status='SUCCEEDED',
                        metadata_json={'collection_job_id': job.id, 'skipped': skipped}))
                    db.commit()
                    return {**self._bulk_receipt(db, job, skipped=skipped), 'reused': queued['reused']}
            except CollectionConfigurationError:
                raise ConsoleError('店铺采集条件已变化，请刷新店铺列表后重试。', 409) from None
            except JobAlreadyRunning:
                raise ConsoleError('全店同步正在提交，请刷新进度。', 409) from None

    def _save_controls(self, sid, payload):
        self._selected(sid)
        with self.session_factory() as db:
            if db.get(Store, sid) is None:
                raise ConsoleError("该店铺尚未完成同步。", 404)
        return store_controls.save_store_controls(self.settings, sid, **payload)

    def _ad_source_store(self, sid):
        self._selected(sid)
        with self.session_factory() as db:
            store = db.get(Store, sid)
            if store is None:
                raise ConsoleError("该店铺尚未完成同步。", 404)
            return store

    @staticmethod
    def _verify_sparkx_scope(settings, store, binding):
        from anyio import run
        from jx_alert.sparkx_backend import verify_sparkx_binding

        return run(verify_sparkx_binding, settings, store, binding)

    def _ad_source_observations(self, db, store, selection):
        from jx_alert.sparkx_backend import list_sparkx_snapshots

        return list_sparkx_snapshots(db, store, selection, limit=2)

    @staticmethod
    def _ad_source_last_attempt(db, store, selection):
        from jx_alert.sparkx_backend import _observation_scope_id

        item = SyncJob.metadata_json["sparkx_observations"][store.sid]
        return db.scalar(select(item["status"].as_string()).where(
            SyncJob.job_type == "hourly_ad_monitor",
            item["selection_id"].as_string() == _observation_scope_id(store, selection),
        ).order_by(SyncJob.id.desc()).limit(1))

    def _ad_source_view(self, store, value=None):
        selection = ad_source_controls.source_snapshot(self.settings, store.sid)
        view = value or ad_source_controls.public_snapshot(self.settings, store, selection)
        if view["revision"] != selection["revision"] or view["observation_source"] != "sparkx":
            return view
        try:
            with self.session_factory() as db:
                rows = self._ad_source_observations(db, store, selection)
                try:
                    attempt = self._ad_source_last_attempt(db, store, selection)
                except SQLAlchemyError:
                    attempt = "STATUS_UNAVAILABLE"  # Keep separately retrieved history.
            keys = {"period", "report_date", "row_count", "captured_at"}
            # Never forward arbitrary history fields/campaign rows as UI metadata.
            latest = []
            for row in rows[:2]:
                if (not isinstance(row, dict) or row.get("period") not in {"daily", "hourly"}
                        or type(row.get("row_count")) is not int or not 0 < row["row_count"] <= 1_000_000):
                    continue
                date_value, captured = row.get("report_date"), row.get("captured_at")
                if (not isinstance(date_value, str) or date.fromisoformat(date_value).isoformat() != date_value
                        or not isinstance(captured, str) or len(captured) > 40
                        or datetime.fromisoformat(captured).utcoffset() != timedelta(0)):
                    continue
                latest.append({key: row[key] for key in keys})
        except (OSError, TypeError, ValueError, KeyError, RuntimeError, ImportError, SQLAlchemyError):
            return {**view, "observation_status": "STATUS_UNAVAILABLE", "latest_observations": []}
        active = (getattr(self.settings, "hourly_ad_monitor_enabled", False)
                  and getattr(self.settings, "scheduler_enabled", False) and not self.settings.app_dry_run)
        try:
            collection = store_controls.collection_enabled(self.settings, store.sid)
        except store_controls.StoreControlError:
            status = "STATUS_UNAVAILABLE"
        else:
            attempt_status = {"PARTIAL": "ATTEMPT_INCOMPLETE", "BLOCKED": "ATTEMPT_BLOCKED",
                              "STATUS_UNAVAILABLE": "STATUS_UNAVAILABLE"}.get(attempt)
            status = ("COLLECTION_PAUSED" if not collection else "HOURLY_DISABLED" if not active
                      else attempt_status or ("OBSERVED" if latest else "WAITING"))
        return {**view, "observation_status": status, "latest_observations": latest}

    def _ad_source(self, sid, operation, token, payload=None):
        store = self._ad_source_store(sid)
        if operation == "read":
            return self._ad_source_view(store)

        def authorize():
            if not self._session(token):
                raise ConsoleError("登录已过期，请重新登录。", 401)
            current = self._ad_source_store(sid)
            if any(getattr(current, name) != getattr(store, name)
                   for name in ("status", "marketplace_id", "currency_code", "timezone")):
                raise ConsoleError("店铺授权或报表范围已变化，请刷新后重试。", 409)

        authorize()
        if operation == "save":
            value = ad_source_controls.save_selection(self.settings, store, payload, authorize=authorize)
            return self._ad_source_view(store, value)
        binding, proof = ad_source_controls.prepare_verification(self.settings, store, payload)
        try:
            result = self._verify_sparkx_scope(self.settings, store, binding)
        except Exception:
            # Vendor responses and exception text must not reach browser or logs.
            raise ConsoleError("SparkX 未能核验该店铺的只读广告范围。请检查服务端授权及档案绑定后重试。", 502) from None
        if (not isinstance(result, dict) or result.get("status") != "VERIFIED"
                or any(result.get(key) != value for key, value in binding.items())
                or type(result.get("row_count")) is not int or result["row_count"] <= 0):
            raise ConsoleError("SparkX 未提供与此店铺匹配的完整只读样本。", 502)
        value = ad_source_controls.save_verification(
            self.settings, store, payload, binding, proof, authorize=authorize)
        return self._ad_source_view(store, value)

    _SPARKX_REPORT_ERRORS = {
        "SPARKX_NOT_CONFIGURED": "尚未配置 SparkX MCP Token，请先在上方「SparkX MCP 连接」保存并测试。",
        "SPARKX_NO_PROFILES": "尚未填写允许使用的 Profile ID，请在上方「SparkX MCP 连接」保存。",
        "SPARKX_AUTH_REJECTED": "SparkX 拒绝了该 Token（401）：Token 无效、过期或缺少只读权限。",
        "SPARKX_TOKEN_INVALID": "SparkX 拒绝了该 Token（401）。",
        "SPARKX_FORBIDDEN": "SparkX 拒绝访问（403），请核对 Token 的授权范围。",
        "SPARKX_PERMISSION_DENIED": "Token 缺少所需权限（广告效果或广告配置只读）。",
        "SPARKX_SCOPE_MISSING": "Token 缺少所需权限（广告效果或广告配置只读）。",
        "SPARKX_RATE_LIMITED": "SparkX 限流，请稍后重试。",
        "SPARKX_TIMEOUT": "SparkX 查询超时，请稍后重试。",
        "SPARKX_UNREACHABLE": "服务器无法连接 mcp.sparkx.cn。",
        "SPARKX_PROVIDER_ERROR": "SparkX 服务暂时异常，请稍后重试。",
    }

    def _sparkx_report_error(self, exc):
        detail = self._SPARKX_REPORT_ERRORS.get(exc.code, f"SparkX 数据未能核验（{exc.code}），未生成内容。")
        return ConsoleError(detail, 409 if exc.status_code == 409 else 502)

    def _sparkx_report_view(self):
        from jx_alert.notifications.recipient_policy import subscriber_entries

        config = sparkx_reports.load_config(self.settings)
        with self.session_factory() as db:
            stores, routing = sparkx_reports.jx_stores(db, self.settings)
            job = db.scalar(select(SyncJob).where(SyncJob.job_type == sparkx_reports.JOB)
                            .order_by(SyncJob.id.desc()).limit(1))
            last = None
            if job is not None:
                meta = job.metadata_json or {}
                last = {"status": job.status, "day": meta.get("day"), "error": meta.get("error"),
                        "queued": len(meta.get("outbox_ids") or []), "findings": meta.get("findings"),
                        "at": job.started_at.isoformat() + "Z" if job.started_at else None}
        binding = routing.get("company_executive_digest") if isinstance(routing, dict) else None
        leads = subscriber_entries(routing) if isinstance(routing, dict) else None
        token, allowed = sparkx_reports._credentials(self.settings)
        return {"revision": config["revision"], "enabled": config["enabled"], "daily_source": config["daily_source"],
                "destinations": config["destinations"], "routes": config["routes"], "updated_at": config["updated_at"],
                "stores": stores, "last_run": last, "sparkx_configured": token is not None,
                "allowed_profile_count": len(allowed),
                "recipients": {"daily_lead": (binding or {}).get("name") if isinstance(binding, dict) else None,
                               "anomaly_lead": leads[0]["name"] if leads and len(leads) == 1 else None}}

    def _sparkx_report_profiles(self):
        try:
            profiles, allowed = sparkx_reports.list_profiles(self.settings)
        except sparkx_reports.SparkXReportError as exc:
            raise self._sparkx_report_error(exc) from None
        with self.session_factory() as db:
            stores, _ = sparkx_reports.jx_stores(db, self.settings)
        suggested = sparkx_reports.suggest_routes(profiles, stores)
        rows = [{"profile_id": pid, "name": row["name"], "short_name": sparkx_reports.short_name(row["name"]),
                 "country": row["country"], "allowed": pid in allowed, "suggested": suggested.get(pid)}
                for pid, row in profiles.items()]
        rows.sort(key=lambda row: (row["country"] != "US", row["country"], row["name"]))
        return {"profiles": rows[:2000]}

    def _sparkx_report_preview(self, kind):
        try:
            value, data = sparkx_reports.preview(self.settings, kind, now=datetime.now(timezone.utc))
        except sparkx_reports.SparkXReportError as exc:
            raise self._sparkx_report_error(exc) from None
        if value is None:
            raise ConsoleError("内容超过飞书卡片上限，请缩小允许的店铺范围。", 409)
        return {"card": value, "report_date": data["report_date"], "window": data["window"],
                "previous_window": data["previous_window"], "profiles": len(data["profiles"]),
                "warnings": sorted(data["errors"])}

    def _company_digest_preview(self):
        """The exact card the 08:30 lane would freeze now; read-only, never queued."""
        from jx_alert.notifications import company_executive

        with self.session_factory() as db:
            try:
                snapshot, value = company_executive.preview_company_digest(
                    db, self.settings, now=datetime.now(timezone.utc))
            except (OSError, ValueError, RuntimeError, TypeError):
                raise ConsoleError("公司日报的店铺范围或时区配置待核实，暂不能预览。", 409) from None
        return {"card": value, "date": snapshot["date"], "mature_date": snapshot["mature_date"],
                "store_count": snapshot["store_count"]}

    def _mapping(self, sid, operation, payload=None):
        self._selected(sid)
        with self.session_factory() as db:
            if db.get(Store, sid) is None:
                raise ConsoleError("该店铺尚未完成同步。", 404)
        if operation == "members":
            return admin_mapping.get_store_members(self.settings, sid)
        return admin_mapping.save_store_mapping(self.settings, sid, payload["open_ids"], payload["revision"])

    def _write_skill_library(self, token, payload):
        # Receiving a streamed body or waiting for a worker can outlive the session.
        # Check again immediately before admitting the library write.
        def authorize():
            if not self._session(token):
                raise ConsoleError("登录已过期，请重新登录。", 401)
        authorize()
        return admin_skill_library.update_skills(self.settings, payload, authorize=authorize)

    async def api(self, request):
        if not self._valid_host(request):
            return _error("请通过管理台的 HTTPS 地址访问。", 400)
        token = request.cookies.get(SESSION, "")
        if not await asyncio.to_thread(self._session, token):
            return _error("登录已过期，请重新登录。", 401)
        path = request.url.path
        sid = request.path_params.get("sid")
        try:
            if request.method == "POST":
                if not self._valid_origin(request):
                    raise ConsoleError("请求来源无效。", 403)
                if path == "/console/logout":
                    if request.headers.get("x-csrf-token"):
                        supplied = request.headers["x-csrf-token"]
                    else:
                        supplied = (await _body(request, "application/x-www-form-urlencoded")).get("csrf", "")
                else:
                    supplied = request.headers.get("x-csrf-token", "")
                if not re.fullmatch(r"[0-9a-f]{64}", supplied) or not hmac.compare_digest(_csrf(token), supplied):
                    raise ConsoleError("页面验证已过期，请刷新。", 403)
                if path == "/console/logout":
                    await asyncio.to_thread(self._session, token, remove=True)
                    response = RedirectResponse("/console/login", 303, headers=_HEADERS)
                    _cookie(response, SESSION, "", 0)
                    return response
                if path == "/console/api/skill-library" and not self._rate_allowed(
                        request, "skill-library-write", per_ip=12, total=20):
                    raise ConsoleError("技能资料库操作太频繁，请稍后重试。", 429)
                body_limit = {"/console/api/data-sources": 16384, "/console/api/skill-library": SKILL_UPLOAD_LIMIT,
                              "/console/api/sparkx-report": 65536}
                ad_source_request = path.endswith(("/advertising-source", "/advertising-source/verify"))
                payload = await _body(request, "application/json", limit=body_limit.get(path, 8192),
                                      strict_json=ad_source_request or path == "/console/api/sparkx-report")
                if path == '/console/api/ad-alert-subscribers':
                    if set(payload) != {'open_ids', 'revision'}:
                        raise ConsoleError('提交内容无效。', 400)
                    if not self._rate_allowed(request, 'ad-alert-subscribers-write', per_ip=8, total=16):
                        raise ConsoleError('私聊接收人设置过于频繁，请稍后重试。', 429)
                    data = await asyncio.to_thread(admin_ad_subscribers.save_ad_subscribers, self.settings, **payload)
                elif ad_source_request:
                    verify = path.endswith("/verify")
                    if not self._rate_allowed(request, "ad-source-verify" if verify else "ad-source-write",
                                              per_ip=3 if verify else 12, total=6 if verify else 24):
                        raise ConsoleError("广告来源操作太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(self._ad_source, sid, "verify" if verify else "save", token, payload)
                elif path == "/console/api/data-sources":
                    if not self._rate_allowed(request, "data-sources-write", per_ip=6, total=12):
                        raise ConsoleError("数据源更新太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(admin_sources.save_data_source, self.settings, payload)
                elif path == "/console/api/deepseek":
                    if not self._rate_allowed(request, "deepseek-write", per_ip=6, total=12):
                        raise ConsoleError("DeepSeek 设置更新太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(deepseek_config.save_configuration, self.settings, payload)
                elif path == "/console/api/deepseek/test":
                    if payload:
                        raise ConsoleError("连接测试不接受额外参数。", 400)
                    if not self._rate_allowed(request, "deepseek-test", per_ip=4, total=8):
                        raise ConsoleError("连接测试太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(deepseek_config.check_connection, self.settings)
                elif path == "/console/api/sparkx":
                    if not self._rate_allowed(request, "sparkx-write", per_ip=6, total=12):
                        raise ConsoleError("SparkX 设置更新太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(sparkx_config.save_configuration, self.settings, payload)
                elif path == "/console/api/sparkx/test":
                    if payload:
                        raise ConsoleError("连接测试不接受额外参数。", 400)
                    if not self._rate_allowed(request, "sparkx-test", per_ip=4, total=8):
                        raise ConsoleError("连接测试太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(sparkx_config.check_connection, self.settings)
                elif path == "/console/api/sparkx-report":
                    if not self._rate_allowed(request, "sparkx-report-write", per_ip=6, total=12):
                        raise ConsoleError("SparkX 日报设置更新太频繁，请稍后重试。", 429)
                    await asyncio.to_thread(sparkx_reports.save_config, self.settings, payload)
                    data = await asyncio.to_thread(self._sparkx_report_view)
                elif path == "/console/api/sparkx-report/profiles":
                    if payload:
                        raise ConsoleError("读取店铺不接受额外参数。", 400)
                    if not self._rate_allowed(request, "sparkx-report-profiles", per_ip=4, total=8):
                        raise ConsoleError("读取 SparkX 店铺太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(self._sparkx_report_profiles)
                elif path == "/console/api/sparkx-report/preview":
                    if set(payload) != {"kind"} or payload["kind"] not in {"daily", "anomaly"}:
                        raise ConsoleError("预览类型无效。", 400)
                    if not self._rate_allowed(request, "sparkx-report-preview", per_ip=3, total=6):
                        raise ConsoleError("预览太频繁，请一分钟后重试。", 429)
                    data = await asyncio.to_thread(self._sparkx_report_preview, payload["kind"])
                elif path == "/console/api/company-digest":
                    if set(payload) != {"open_ids", "revision"}:
                        raise ConsoleError("提交内容无效。", 400)
                    if not self._rate_allowed(request, "company-digest-write", per_ip=8, total=16):
                        raise ConsoleError("公司日报接收人设置过于频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(admin_executive.save_executive, self.settings, **payload)
                elif path == "/console/api/owners/sync-names":
                    if payload:
                        raise ConsoleError("同步姓名不接受额外参数。", 400)
                    if not self._rate_allowed(request, "owner-names", per_ip=3, total=6):
                        raise ConsoleError("同步姓名太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(admin_mapping.sync_owner_names, self.settings)
                elif path == "/console/api/skill-library":
                    data = await asyncio.to_thread(self._write_skill_library, token, payload)
                elif path == "/console/api/review-skills":
                    if not self._rate_allowed(request, "review-skills-write", per_ip=6, total=12):
                        raise ConsoleError("分析技能更新太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(admin_review_skills.install_review_skill, self.settings, payload)
                elif path in {"/console/api/notification-group", "/console/api/notification-group/preview"}:
                    preview = path.endswith("/preview")
                    expected_fields = {"chat_id", "revision"} if preview else {"chat_id", "revision", "confirmed"}
                    if set(payload) != expected_fields:
                        raise ConsoleError("提交内容无效。", 400)
                    if not self._rate_allowed(request, "notification-group-write", per_ip=8, total=16):
                        raise ConsoleError("群设置操作太频繁，请稍后重试。", 429)
                    action = admin_groups.preview_notification_group if preview else admin_groups.save_notification_group
                    data = await asyncio.to_thread(action, self.settings, **payload)
                elif path == '/console/api/collection/all':
                    if payload:
                        raise ConsoleError('批量同步不接受额外日期或店铺参数。', 400)
                    if not self._rate_allowed(request, 'bulk-collection', per_ip=2, total=4):
                        raise ConsoleError('批量同步操作太频繁，请稍后查看已有任务。', 429)
                    data = await asyncio.to_thread(self._bulk_collection, enqueue=True)
                elif path.endswith('/collection'):
                    if payload:
                        raise ConsoleError('同步最新日报不接受额外日期或店铺参数。', 400)
                    if not self._rate_allowed(request, 'collection', per_ip=4, total=12):
                        raise ConsoleError('同步操作太频繁，请稍后查看已有任务。', 429)
                    data = await asyncio.to_thread(self._collection, sid, enqueue=True)
                elif path.endswith("/controls"):
                    if set(payload) != {"collection_enabled", "notifications_enabled", "revision"}:
                        raise ConsoleError("提交内容无效。", 400)
                    if not self._rate_allowed(request, "controls", per_ip=12, total=24):
                        raise ConsoleError("操作太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(self._save_controls, sid, payload)
                else:
                    if set(payload) != {"open_ids", "revision"}:
                        raise ConsoleError("提交内容无效。", 400)
                    if not self._rate_allowed(request, "mapping", per_ip=12, total=24):
                        raise ConsoleError("操作太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(self._mapping, sid, "owners", payload)
            elif path.startswith('/console/api/ad-alert-subscribers'):
                members = path.endswith('/members')
                if members and not self._rate_allowed(request, 'ad-alert-subscribers-members', per_ip=12, total=24):
                    raise ConsoleError('成员列表读取过于频繁，请稍后重试。', 429)
                data = await asyncio.to_thread(admin_ad_subscribers.get_ad_subscribers, self.settings,
                                              include_members=members)
            elif path == "/console/api/data-sources":
                data = await asyncio.to_thread(admin_sources.get_data_sources, self.settings)
            elif path == "/console/api/deepseek":
                data = await asyncio.to_thread(deepseek_config.configuration, self.settings)
            elif path == "/console/api/sparkx":
                data = await asyncio.to_thread(sparkx_config.configuration, self.settings)
            elif path == "/console/api/sparkx-report":
                data = await asyncio.to_thread(self._sparkx_report_view)
            elif path.startswith("/console/api/company-digest"):
                if path.endswith("/preview"):
                    if not self._rate_allowed(request, "company-digest-preview", per_ip=6, total=12):
                        raise ConsoleError("预览太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(self._company_digest_preview)
                else:
                    members = path.endswith("/members")
                    if members and not self._rate_allowed(request, "company-digest-members", per_ip=12, total=24):
                        raise ConsoleError("成员列表读取过于频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(admin_executive.get_executive, self.settings,
                                                  include_members=members)
            elif path == "/console/api/review-skills":
                data = await asyncio.to_thread(admin_review_skills.get_review_skills, self.settings)
            elif path == "/console/api/skill-library":
                data = await asyncio.to_thread(admin_skill_library.list_skills, self.settings)
            elif path.startswith("/console/api/skill-library/"):
                params = request.query_params
                raw = params.get("version")
                if (set(params) - {"version"} or (raw is not None and
                        (len(params.getlist("version")) != 1 or not re.fullmatch(r"[1-9][0-9]?", raw)))):
                    raise ConsoleError("版本查询无效。", 400)
                if path.endswith("/package.zip"):
                    if raw is None:
                        raise ConsoleError("导出时必须指定资料库版本。", 400)
                    name = request.path_params["name"]
                    package = await asyncio.to_thread(admin_skill_library.export_skill, self.settings, name, int(raw))
                    if not await asyncio.to_thread(self._session, token):
                        raise ConsoleError("登录已过期，请重新登录。", 401)
                    return Response(package, media_type="application/zip", headers={**_HEADERS,
                        "Content-Disposition": f'attachment; filename="{name}-v{raw}.zip"'})
                data = await asyncio.to_thread(admin_skill_library.inspect_skill, self.settings,
                                              request.path_params["name"], int(raw) if raw else None)
            elif path.startswith("/console/api/notification-group"):
                if path.endswith("/chats"):
                    if not self._rate_allowed(request, "notification-group-list", per_ip=12, total=24):
                        raise ConsoleError("群列表刷新太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(admin_groups.list_notification_groups, self.settings)
                else:
                    data = await asyncio.to_thread(admin_groups.current_notification_group, self.settings)
            elif path == '/console/api/collection/all':
                data = await asyncio.to_thread(self._bulk_collection)
            elif path.endswith('/advertising-source'):
                data = await asyncio.to_thread(self._ad_source, sid, "read", token)
            elif path.endswith('/collection'):
                data = await asyncio.to_thread(self._collection, sid)
            elif path.endswith("/members"):
                if not self._rate_allowed(request, "members", per_ip=24, total=48):
                    raise ConsoleError("成员加载太频繁，请稍后重试。", 429)
                data = await asyncio.to_thread(self._mapping, sid, "members")
            elif sid:
                data = await asyncio.to_thread(self._read_store, sid, request.query_params.get("date"))
            else:
                data = await asyncio.to_thread(self._stores_data)
                data["csrf"] = _csrf(token)
            if (request.method == "GET" and path.startswith("/console/api/skill-library")
                    and not await asyncio.to_thread(self._session, token)):
                raise ConsoleError("登录已过期，请重新登录。", 401)
            return JSONResponse(data, headers=_HEADERS)
        except ConsoleError as exc:
            return _error(exc.detail, exc.status)
        except (SourceCredentialsError, deepseek_config.DeepSeekConfigError, admin_review_skills.ReviewSkillError,
                admin_skill_library.LibraryError, sparkx_config.SparkXConfigError,
                sparkx_reports.SparkXReportConfigError) as exc:
            return _error(exc.detail, exc.status_code)
        except admin_mapping.MappingError as exc:
            return _error(exc.detail, exc.status_code)
        except (store_controls.StoreControlError, ad_source_controls.AdSourceError) as exc:
            return _error(exc.detail, exc.status_code)
        except Exception as exc:
            _LOG.warning("Console request failed (%s)", type(exc).__name__)
            return _error("暂时无法读取或保存，请稍后重试。", 503)

    def routes(self):
        return [Route("/console", self.home), Route("/console/", self.home),
                Route("/console/login", self.login, methods=["GET", "POST"]),
                Route("/console/assets/{asset}", self.asset), Route("/console/api/stores", self.api),
                Route("/console/api/data-sources", self.api, methods=["GET", "POST"]),
                Route("/console/api/deepseek", self.api, methods=["GET", "POST"]),
                Route("/console/api/deepseek/test", self.api, methods=["POST"]),
                Route("/console/api/sparkx", self.api, methods=["GET", "POST"]),
                Route("/console/api/sparkx/test", self.api, methods=["POST"]),
                Route("/console/api/sparkx-report", self.api, methods=["GET", "POST"]),
                Route("/console/api/sparkx-report/profiles", self.api, methods=["POST"]),
                Route("/console/api/sparkx-report/preview", self.api, methods=["POST"]),
                Route("/console/api/company-digest", self.api, methods=["GET", "POST"]),
                Route("/console/api/company-digest/members", self.api),
                Route("/console/api/company-digest/preview", self.api),
                Route("/console/api/owners/sync-names", self.api, methods=["POST"]),
                Route("/console/api/review-skills", self.api, methods=["GET", "POST"]),
                Route("/console/api/skill-library", self.api, methods=["GET", "POST"]),
                Route("/console/api/skill-library/{name}", self.api),
                Route("/console/api/skill-library/{name}/package.zip", self.api),
                Route('/console/api/ad-alert-subscribers', self.api, methods=['GET', 'POST']),
                Route('/console/api/ad-alert-subscribers/members', self.api),
                Route("/console/api/notification-group", self.api, methods=["GET", "POST"]),
                Route("/console/api/notification-group/chats", self.api),
                Route("/console/api/notification-group/preview", self.api, methods=["POST"]),
                Route("/console/api/stores/{sid}", self.api), Route("/console/api/stores/{sid}/members", self.api),
                Route("/console/api/stores/{sid}/owners", self.api, methods=["POST"]),
                Route("/console/api/stores/{sid}/controls", self.api, methods=["POST"]),
                Route("/console/api/stores/{sid}/advertising-source", self.api, methods=["GET", "POST"]),
                Route("/console/api/stores/{sid}/advertising-source/verify", self.api, methods=["POST"]),
                Route("/console/api/stores/{sid}/collection", self.api, methods=["GET", "POST"]),
                Route("/console/api/collection/all", self.api, methods=["GET", "POST"]),
                Route("/console/logout", self.api, methods=["POST"])]


def create_browser_app():
    """Serve only browser pages. The live MCP process keeps its own connections."""
    from jx_alert.config import get_settings

    settings = get_settings()
    if settings.mcp_auth_mode != "oauth":
        raise RuntimeError("The browser console requires an initialized owner OAuth configuration")
    issuer = settings.mcp_public_url.rstrip("/")
    provider = SqliteOAuthProvider(database_path=settings.mcp_oauth_database_path,
                                   credentials_path=settings.mcp_owner_credentials_file,
                                   issuer_url=issuer, resource_url=issuer + settings.mcp_path)
    routes = [*provider.owner_routes(), *OwnerConsole(settings, provider).routes()]
    return provider.auth_guard(Starlette(routes=routes))


if __name__ == "__main__":
    import uvicorn
    from jx_alert.config import get_settings

    uvicorn.run(create_browser_app(), host="0.0.0.0", port=8002, access_log=False,
                proxy_headers=True, forwarded_allow_ips=get_settings().mcp_forwarded_allow_ips)
