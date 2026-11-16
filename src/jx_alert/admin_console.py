"""A separate single-owner browser session for store data and Feishu routing."""
import asyncio
from contextlib import closing
from datetime import date
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

from jx_alert import admin_data, admin_groups, admin_mapping, store_controls
from jx_alert.mcp_oauth import SqliteOAuthProvider, _private_file
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


async def _body(request, media, limit=8192):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != media:
        raise ConsoleError("请求格式无效。", 415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise ConsoleError("请求内容过长。", 413)
        body.extend(chunk)
    try:
        if media == "application/json":
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError
            return result
        values = parse_qsl(body.decode(), keep_blank_values=True, max_num_fields=4, strict_parsing=True)
        if len(dict(values)) != len(values):
            raise ValueError
        return dict(values)
    except (ValueError, UnicodeError):
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
        mime = {"console.css": "text/css", "console.js": "text/javascript"}
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

    def _save_controls(self, sid, payload):
        self._selected(sid)
        with self.session_factory() as db:
            if db.get(Store, sid) is None:
                raise ConsoleError("该店铺尚未完成同步。", 404)
        return store_controls.save_store_controls(self.settings, sid, **payload)

    def _mapping(self, sid, operation, payload=None):
        self._selected(sid)
        with self.session_factory() as db:
            if db.get(Store, sid) is None:
                raise ConsoleError("该店铺尚未完成同步。", 404)
        if operation == "members":
            return admin_mapping.get_store_members(self.settings, sid)
        return admin_mapping.save_store_mapping(self.settings, sid, payload["open_ids"], payload["revision"])

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
                payload = await _body(request, "application/json")
                if path in {"/console/api/notification-group", "/console/api/notification-group/preview"}:
                    preview = path.endswith("/preview")
                    expected_fields = {"chat_id", "revision"} if preview else {"chat_id", "revision", "confirmed"}
                    if set(payload) != expected_fields:
                        raise ConsoleError("提交内容无效。", 400)
                    if not self._rate_allowed(request, "notification-group-write", per_ip=8, total=16):
                        raise ConsoleError("群设置操作太频繁，请稍后重试。", 429)
                    action = admin_groups.preview_notification_group if preview else admin_groups.save_notification_group
                    data = await asyncio.to_thread(action, self.settings, **payload)
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
            elif path.startswith("/console/api/notification-group"):
                if path.endswith("/chats"):
                    if not self._rate_allowed(request, "notification-group-list", per_ip=12, total=24):
                        raise ConsoleError("群列表刷新太频繁，请稍后重试。", 429)
                    data = await asyncio.to_thread(admin_groups.list_notification_groups, self.settings)
                else:
                    data = await asyncio.to_thread(admin_groups.current_notification_group, self.settings)
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
            return JSONResponse(data, headers=_HEADERS)
        except ConsoleError as exc:
            return _error(exc.detail, exc.status)
        except admin_mapping.MappingError as exc:
            return _error(exc.detail, exc.status_code)
        except store_controls.StoreControlError as exc:
            return _error(exc.detail, exc.status_code)
        except Exception as exc:
            _LOG.warning("Console request failed (%s)", type(exc).__name__)
            return _error("暂时无法读取或保存，请稍后重试。", 503)

    def routes(self):
        return [Route("/console", self.home), Route("/console/", self.home),
                Route("/console/login", self.login, methods=["GET", "POST"]),
                Route("/console/assets/{asset}", self.asset), Route("/console/api/stores", self.api),
                Route("/console/api/notification-group", self.api, methods=["GET", "POST"]),
                Route("/console/api/notification-group/chats", self.api),
                Route("/console/api/notification-group/preview", self.api, methods=["POST"]),
                Route("/console/api/stores/{sid}", self.api), Route("/console/api/stores/{sid}/members", self.api),
                Route("/console/api/stores/{sid}/owners", self.api, methods=["POST"]),
                Route("/console/api/stores/{sid}/controls", self.api, methods=["POST"]),
                Route("/console/api/stores/{sid}/collection", self.api, methods=["GET", "POST"]),
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
