"""Owner console: SparkX connection and Skills, store directory, recipients, lanes and DeepSeek.

A single owner password (PBKDF2 file) opens a 30-minute browser session. Every
POST needs the same-origin header and the session-bound CSRF token. Secrets
are write-only: tokens and keys are never returned to the browser. Previews run
DeepSeek against live SparkX data in a background job and never queue a message.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import html
from importlib.resources import files
import json
import logging
import re
import secrets
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import or_, select
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from jx_alert import admin_feishu
from jx_alert.ai import deepseek_config
from jx_alert.console_auth import SESSION_TTL, OwnerPassword, SessionStore
from jx_alert.notifications.cards import plain_text
from jx_alert.notifications.outbox import QUESTION_REPLY_PREFIX, NotificationOutbox
from jx_alert.notifications.receipt import unique_json_fields
from jx_alert.ai.deepseek import DeepSeekError
from jx_alert.reports import lanes
from jx_alert.reports import settings as lane_settings
from jx_alert.reports import tasks as lane_tasks
from jx_alert.sparkx import credentials as sparkx_credentials
from jx_alert.sparkx import skills as skill_store
from jx_alert.sparkx import stores as directory_module
from jx_alert.sparkx.client import READ_TOOLS, SparkXError, fetch_roster
from jx_alert.storage.db import SessionLocal
from jx_alert.storage.models import SyncJob

SESSION = "__Host-jx-console-session"
LOGIN_CSRF = "__Host-jx-console-login"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_PID = re.compile(r"[1-9][0-9]{0,19}")
_LOG = logging.getLogger(__name__)
_HEADERS = {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin", "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                               "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}
_ASSETS = {"console.css": "text/css", "console.js": "text/javascript"}
# Paths of the retired JX MCP server. The existing Nginx vhost still proxies them
# (and "/") to the old MCP port, which now reaches this console.
RETIRED_PATHS = ("/mcp", "/authorize", "/token", "/register", "/revoke", "/oauth/owner",
                 "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource",
                 "/.well-known/oauth-protected-resource/mcp")
RETIRED_MESSAGE = ("JX MCP 服务已停用。广告数据请在 ChatGPT/Claude 中直接连接 SparkX MCP"
                   "（https://mcp.sparkx.cn/mcp）；管理台地址为 /console/。")
_ROLE_LABELS = {"group": "通知群", "lead": "负责人", "operator": "运营", "group_question": "群内问答",
                "private_question": "私聊问答"}
_LANE_LABELS = {"daily": "08:30 日报", "review": "异常复盘", "hourly": "实时监控"}


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _csrf(session):
    return _digest("jx-console-csrf:" + session)


def _cookie(response, name, value, ttl):
    response.set_cookie(name, value, max_age=ttl, path="/", secure=True, httponly=True, samesite="strict")


def _error(message, status):
    return JSONResponse({"error": message}, status, headers=_HEADERS)


def _iso(value):
    return value.isoformat() + "Z" if value is not None else None


class ConsoleError(Exception):
    def __init__(self, detail, status):
        super().__init__(detail)
        self.detail, self.status = detail, status


async def _body(request, media, limit=16384):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != media:
        raise ConsoleError("请求格式无效。", 415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise ConsoleError("请求内容过长。", 413)
        body.extend(chunk)
    try:
        if media == "application/json":
            result = json.loads(body or b"{}", object_pairs_hook=unique_json_fields)
            if not isinstance(result, dict):
                raise ValueError
            return result
        values = parse_qsl(body.decode(), keep_blank_values=True, max_num_fields=4, strict_parsing=True)
        if len(dict(values)) != len(values):
            raise ValueError
        return dict(values)
    except (ValueError, UnicodeError, RecursionError):
        raise ConsoleError("请求格式无效。", 400) from None


class PreviewJobs:
    """Background previews: a DeepSeek run takes minutes, longer than a proxy request may stay open.

    One preview runs at a time; finished results are kept for 30 minutes.
    """

    TTL = 30 * 60

    def __init__(self):
        self._jobs, self._lock = {}, threading.Lock()

    def start(self, lane, work):
        with self._lock:
            now = time.monotonic()
            self._jobs = {key: job for key, job in self._jobs.items() if now - job["at"] < self.TTL}
            if any(job["status"] == "running" for job in self._jobs.values()):
                raise ConsoleError("已有一个预览正在生成，请等它完成。", 409)
            key = secrets.token_urlsafe(12)
            self._jobs[key] = {"at": now, "status": "running", "lane": lane, "result": None, "error": None,
                               "started_at": datetime.now(timezone.utc).isoformat()}

        def target():
            try:
                result, status, error = work(), "done", None
            except ConsoleError as exc:
                result, status, error = None, "failed", exc.detail
            except Exception as exc:  # noqa: BLE001 - never echo internal errors to the browser.
                _LOG.warning("Console preview failed (%s)", type(exc).__name__)
                result, status, error = None, "failed", "预览生成失败，请稍后重试。"
            with self._lock:
                if key in self._jobs:
                    self._jobs[key].update(status=status, result=result, error=error,
                                           finished_at=datetime.now(timezone.utc).isoformat())

        threading.Thread(target=target, name="console-preview", daemon=True).start()
        return key

    def get(self, key):
        with self._lock:
            job = self._jobs.get(key) if isinstance(key, str) else None
            if job is None:
                raise ConsoleError("预览已过期，请重新生成。", 404)
            return {name: job.get(name) for name in ("status", "lane", "result", "error", "started_at", "finished_at")}


class OwnerConsole:
    def __init__(self, settings, password, sessions, *, session_factory=SessionLocal, runner=None):
        self.settings = settings
        self.password = password
        self.sessions = sessions
        url = urlsplit(settings.console_public_url)
        if url.scheme != "https" or not url.netloc:
            raise ValueError("CONSOLE_PUBLIC_URL must be the console's https:// origin")
        self.origin = f"{url.scheme}://{url.netloc}"
        self.host = url.netloc.lower()
        self.session_factory = session_factory
        self.runner = runner
        self.previews = PreviewJobs()
        self._limits = {}
        self._limit_lock = threading.Lock()

    # ------------------------------------------------------------ plumbing

    def _rate_allowed(self, request, bucket, *, per_ip, total):
        minute = int(time.time()) // 60
        address = request.client.host if request.client else "unknown"
        keys = ((bucket, "total"), (bucket, address[:100]))
        with self._limit_lock:
            self._limits = {key: v for key, v in self._limits.items() if v[0] == minute}
            if any(self._limits.get(key, (minute, 0))[1] >= maximum for key, maximum in zip(keys, (total, per_ip))):
                return False
            for key in keys:
                self._limits[key] = (minute, self._limits.get(key, (minute, 0))[1] + 1)
        return True

    def _limit(self, request, bucket, per_ip, total, message="操作太频繁，请稍后重试。"):
        if not self._rate_allowed(request, bucket, per_ip=per_ip, total=total):
            raise ConsoleError(message, 429)

    def _valid_host(self, request):
        return request.url.scheme == "https" and request.headers.getlist("host") == [self.host]

    def _valid_origin(self, request):
        return request.headers.getlist("origin") == [self.origin]

    @staticmethod
    def _asset(name):
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
        if not await asyncio.to_thread(self.sessions.valid, token):
            return RedirectResponse("/console/login", 303, headers=_HEADERS)
        return HTMLResponse(self._asset("index.html").replace("{{CSRF}}", _csrf(token)), headers=_HEADERS)

    async def login(self, request):
        if not self._valid_host(request):
            return _error("请通过管理台的 HTTPS 地址访问。", 400)
        if request.method == "GET":
            if await asyncio.to_thread(self.sessions.valid, request.cookies.get(SESSION, "")):
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
        supplied, expected = form.get("csrf", ""), request.cookies.get(LOGIN_CSRF, "")
        if not _TOKEN.fullmatch(supplied) or not hmac.compare_digest(supplied, expected):
            return self._login_page("登录页面已过期，请重试。", 403)
        if not await asyncio.to_thread(self.password.verify, form.get("password", "")):
            return self._login_page("口令不正确，请重试。", 403)
        token = await asyncio.to_thread(self.sessions.new)
        response = RedirectResponse("/console/", 303, headers=_HEADERS)
        _cookie(response, SESSION, token, SESSION_TTL)
        _cookie(response, LOGIN_CSRF, "", 0)
        return response

    async def asset(self, request):
        name = request.path_params["asset"]
        if not self._valid_host(request) or name not in _ASSETS:
            return _error("资源不存在。", 404)
        return Response(self._asset(name), media_type=_ASSETS[name], headers=_HEADERS)

    async def root(self, request):
        return RedirectResponse("/console/", 302, headers=_HEADERS)

    async def retired(self, request):
        # Old ChatGPT connectors get a clear, final answer instead of a 502 from Nginx.
        return JSONResponse({"error": RETIRED_MESSAGE, "sparkx_mcp": "https://mcp.sparkx.cn/mcp"}, 410,
                            headers=_HEADERS)

    # ------------------------------------------------------------ views

    def _last_runs(self, db):
        result = {}
        for lane, job_type in lanes.JOBS.items():
            job = db.scalar(select(SyncJob).where(SyncJob.job_type == job_type).order_by(SyncJob.id.desc()).limit(1))
            if job is not None:
                meta = job.metadata_json or {}
                message = meta.get("message") if isinstance(meta.get("message"), str) else (
                    SparkXError(meta["error"]).message if isinstance(meta.get("error"), str) else None)
                result[lane] = {"status": job.status, "at": _iso(job.started_at), "error": meta.get("error"),
                                "queued": len(meta.get("outbox_ids") or []), "findings": meta.get("findings"),
                                "sparkx_calls": meta.get("sparkx_calls"), "message": message,
                                "reason": meta.get("reason")}
        return result

    def _deliveries(self, db, limit=30):
        rows = db.scalars(select(NotificationOutbox).where(or_(
            NotificationOutbox.idempotency_key.startswith(lanes.PREFIX),
            NotificationOutbox.idempotency_key.startswith(QUESTION_REPLY_PREFIX)))
            .order_by(NotificationOutbox.id.desc()).limit(limit)).all()
        result = []
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            delivery = payload.get("delivery") or {}
            kind = (delivery.get("role") or "").removeprefix("jx_")
            lane, _, who = kind.partition("_") if kind.split("_", 1)[0] in lanes.LANES else ("question", "", kind)
            result.append({"id": row.id, "lane": _LANE_LABELS.get(lane, "问答回复"),
                           "recipient": _ROLE_LABELS.get(who or kind, "—") + (
                               f"：{delivery['name']}" if isinstance(delivery.get("name"), str) else ""),
                           "title": (payload.get("text") or "")[:80], "status": row.status,
                           "created_at": _iso(row.created_at), "delivered_at": _iso(row.delivered_at),
                           "error": (row.last_error or "")[:160] or None})
        return result

    def _overview(self):
        settings = self.settings
        try:
            sparkx = sparkx_credentials.configuration(settings)
        except sparkx_credentials.SparkXConfigError as exc:
            sparkx = {"configured": False, "error": exc.detail}
        try:
            deepseek = deepseek_config.configuration(settings)
        except deepseek_config.DeepSeekConfigError as exc:
            deepseek = {"configured": False, "error": exc.detail}
        try:
            group = admin_feishu.current_group(settings)["name"]
        except admin_feishu.MappingError:
            group = None
        try:
            config = lane_settings.load(settings)["lanes"]
        except lane_settings.SettingsError:
            config = {}
        try:
            directory = directory_module.load(settings)
        except directory_module.StoreDirectoryError:
            directory = {"profiles": {}}
        with self.session_factory() as db:
            roster, roster_at = directory_module.latest_roster(db)
            runs, recent = self._last_runs(db), self._deliveries(db, 10)
        monitored = [key for key in roster if directory_module.monitored(directory, key)]
        index = skill_store.installed(settings)
        try:
            leads = admin_feishu.leads(settings)
        except admin_feishu.MappingError:
            leads = {}
        return {"sparkx": sparkx, "deepseek": deepseek,
                "skills": {"installed": bool(index), "release": (index or {}).get("release"),
                           "count": len((index or {}).get("skills") or [])},
                "feishu": {"enabled": settings.feishu_enabled, "dry_run": settings.app_dry_run,
                           "delivery_enabled": settings.delivery_enabled(), "group": group,
                           "company_lead": bool(leads.get("company")), "ad_lead": bool(leads.get("ad")),
                           "bot_enabled": settings.feishu_bot_enabled, "scheduler_enabled": settings.scheduler_enabled},
                "lanes": {lane: {"enabled": (config.get(lane) or {}).get("enabled", False), "last": runs.get(lane),
                                 **({"start_hour": config["hourly"]["start_hour"], "end_hour": config["hourly"]["end_hour"]}
                                    if lane == "hourly" and "hourly" in config else {})}
                          for lane in lanes.LANES},
                "stores": {"authorized": len(roster), "monitored": len(monitored),
                           "mapped": sum(bool(directory_module.operators(directory, key)) for key in monitored),
                           "roster_at": _iso(roster_at)},
                "recent": recent}

    def _stores(self):
        directory = directory_module.load(self.settings)
        with self.session_factory() as db:
            roster, roster_at = directory_module.latest_roster(db)
        names = directory_module.display_names(roster) if roster else {}
        rows = []
        for key in sorted(set(roster) | set(directory["profiles"]), key=int):
            profile = roster.get(key, {})
            operators = directory_module.operators(directory, key)
            rows.append({"profile_id": key, "name": profile.get("name") or key, "short_name": names.get(key, key),
                         "country": profile.get("country"),
                         "country_label": directory_module.COUNTRY_LABELS.get(profile.get("country"),
                                                                              profile.get("country") or "—"),
                         "store_type": profile.get("store_type"), "authorized": key in roster,
                         "monitored": directory_module.monitored(directory, key),
                         "operators": [{"id": row["open_id"], "name": row["name"]} for row in operators]})
        rows.sort(key=lambda row: (not row["authorized"], row["country"] != "US", row["short_name"]))
        return {"revision": directory["revision"], "roster_at": _iso(roster_at), "rows": rows}

    def _refresh_stores(self):
        try:
            roster = fetch_roster(self.settings)
        except SparkXError as exc:
            raise ConsoleError(exc.message, 409 if exc.status_code == 409 else 502) from None
        with self.session_factory() as db:
            directory_module.record_roster(db, roster, datetime.now(timezone.utc))
            db.commit()
        return self._stores()

    def _save_stores(self, payload):
        if set(payload) != {"revision", "changes"} or not isinstance(payload["changes"], dict):
            raise ConsoleError("提交内容无效。", 400)
        changes = payload["changes"]
        if not 1 <= len(changes) <= 500:
            raise ConsoleError("一次最多保存 500 家店铺。", 400)
        wanted = []
        for key, value in changes.items():
            if (not isinstance(key, str) or not _PID.fullmatch(key) or not isinstance(value, dict)
                    or set(value) != {"monitored", "operator_ids"} or type(value["monitored"]) is not bool
                    or not isinstance(value["operator_ids"], list) or len(value["operator_ids"]) > 10):
                raise ConsoleError("店铺设置无效。", 400)
            wanted += value["operator_ids"]
        people = {}
        for start in range(0, len(set(wanted)), 10):
            chunk = sorted(set(wanted))[start:start + 10]
            people.update({row["open_id"]: row for row in admin_feishu.verified_people(self.settings, chunk)})
        directory_module.save(self.settings, {key: {
            "monitored": value["monitored"],
            "operators": [people[uid] for uid in dict.fromkeys(value["operator_ids"])]}
            for key, value in changes.items()}, payload["revision"])
        return self._stores()

    def _recipients(self):
        result = admin_feishu.leads(self.settings)
        try:
            group = admin_feishu.current_group(self.settings)
        except admin_feishu.MappingError:
            group = None
        return {**result, "group_detail": group}

    def _reports(self):
        data = lane_settings.load(self.settings)
        return {"revision": data["revision"], "updated_at": data.get("updated_at"), "lanes": data["lanes"],
                "defaults": lane_settings.DEFAULTS, "default_prompts": lane_tasks.DEFAULT_PROMPTS,
                "max_prompt": lane_settings.MAX_PROMPT}

    def _preview_config(self, payload):
        """The saved lane settings, or the unsaved form values when the owner previews a draft."""
        if set(payload) - {"lane", "draft"} or payload.get("lane") not in lanes.LANES:
            raise ConsoleError("预览类型无效。", 400)
        lane, saved = payload["lane"], lane_settings.load(self.settings)["lanes"]
        draft = payload.get("draft")
        if draft is None:
            return lane, saved[lane]
        if not isinstance(draft, dict) or set(draft) - set(lane_settings.DEFAULTS[lane]):
            raise ConsoleError("预览参数无效。", 400)
        try:
            checked = lane_settings.validate({**saved, lane: {**saved[lane], **draft}})
        except (ValueError, TypeError, KeyError):
            raise ConsoleError("请先核对阈值和任务说明再预览。", 400) from None
        return lane, checked[lane]

    def _run_preview(self, lane, config):
        if not lanes.sparkx_ready(self.settings):
            raise ConsoleError("请先在「SparkX MCP」保存 Token。", 409)
        if not lanes.deepseek_ready(self.settings) and self.runner is None:
            raise ConsoleError("请先在「DeepSeek」保存 API Key。", 409)
        now = datetime.now(timezone.utc)
        started = time.monotonic()
        with self.session_factory() as db:
            try:
                values, info = lanes.preview(self.settings, lane, now=now, db=db, runner=self.runner, config=config)
            except SparkXError as exc:
                raise ConsoleError(exc.message, 409 if exc.status_code == 409 else 502) from None
            except DeepSeekError as exc:
                raise ConsoleError(f"DeepSeek：{exc}", 502) from None
            except ValueError:
                raise ConsoleError("DeepSeek 的输出不符合约定格式，请重试或调整任务说明。", 502) from None
            db.commit()
        info["seconds"] = round(time.monotonic() - started)
        if not values:
            text = "当前没有需要提醒的广告活动。" if lane == "hourly" else "没有生成内容。"
            return {"cards": [], "text": text, "info": info}
        return {"cards": values, "text": "\n\n".join(plain_text(value) for value in values), "info": info}

    def _skills(self, check=False):
        view = skill_store.status(self.settings, check=check)
        return {**view, "read_tools": sorted(READ_TOOLS), "source": skill_store.REPOSITORY}

    def _install_skills(self):
        try:
            skill_store.install(self.settings)
        except skill_store.SkillsError as exc:
            raise ConsoleError(exc.detail, exc.status_code) from None
        return self._skills()

    # ------------------------------------------------------------ dispatch

    async def api(self, request):
        if not self._valid_host(request):
            return _error("请通过管理台的 HTTPS 地址访问。", 400)
        token = request.cookies.get(SESSION, "")
        if not await asyncio.to_thread(self.sessions.valid, token):
            return _error("登录已过期，请重新登录。", 401)
        path = request.url.path
        try:
            if request.method == "POST":
                data = await self._post(request, token, path)
                if isinstance(data, Response):
                    return data
            else:
                data = await self._get(request, path)
                if path == "/console/api/overview":
                    data["csrf"] = _csrf(token)
            return JSONResponse(data, headers=_HEADERS)
        except ConsoleError as exc:
            return _error(exc.detail, exc.status)
        except (deepseek_config.DeepSeekConfigError, sparkx_credentials.SparkXConfigError,
                directory_module.StoreDirectoryError, lane_settings.SettingsError) as exc:
            return _error(exc.detail, exc.status_code)
        except admin_feishu.MappingError as exc:
            return _error(exc.detail, exc.status_code)
        except Exception as exc:  # noqa: BLE001 - never echo internal errors to the browser.
            _LOG.warning("Console request failed (%s)", type(exc).__name__)
            return _error("暂时无法读取或保存，请稍后重试。", 503)

    async def _get(self, request, path):
        run = asyncio.to_thread
        if path == "/console/api/overview":
            return await run(self._overview)
        if path == "/console/api/sparkx":
            return await run(sparkx_credentials.configuration, self.settings)
        if path == "/console/api/stores":
            return await run(self._stores)
        if path == "/console/api/members":
            self._limit(request, "members", 12, 24, "成员列表读取过于频繁，请稍后重试。")
            return {"members": await run(admin_feishu.group_members, self.settings)}
        if path == "/console/api/recipients":
            return await run(self._recipients)
        if path == "/console/api/groups":
            self._limit(request, "groups", 12, 24, "群列表刷新太频繁，请稍后重试。")
            return await run(admin_feishu.list_groups, self.settings)
        if path == "/console/api/reports":
            return await run(self._reports)
        if path == "/console/api/deepseek":
            return await run(deepseek_config.configuration, self.settings)
        if path == "/console/api/skills":
            return await run(self._skills)
        if path == "/console/api/reports/preview":
            return self.previews.get(request.query_params.get("job"))
        if path == "/console/api/deliveries":
            def deliveries():
                with self.session_factory() as db:
                    return {"rows": self._deliveries(db)}
            return await run(deliveries)
        raise ConsoleError("接口不存在。", 404)

    async def _post(self, request, token, path):
        if not self._valid_origin(request):
            raise ConsoleError("请求来源无效。", 403)
        if path == "/console/logout":
            supplied = request.headers.get("x-csrf-token") or (
                await _body(request, "application/x-www-form-urlencoded")).get("csrf", "")
        else:
            supplied = request.headers.get("x-csrf-token", "")
        if not re.fullmatch(r"[0-9a-f]{64}", supplied) or not hmac.compare_digest(_csrf(token), supplied):
            raise ConsoleError("页面验证已过期，请刷新。", 403)
        if path == "/console/logout":
            await asyncio.to_thread(self.sessions.remove, token)
            response = RedirectResponse("/console/login", 303, headers=_HEADERS)
            _cookie(response, SESSION, "", 0)
            return response
        large = {"/console/api/stores/save", "/console/api/reports", "/console/api/reports/preview"}
        payload = await _body(request, "application/json", limit=98304 if path in large else 16384)
        run = asyncio.to_thread
        if path == "/console/api/sparkx":
            self._limit(request, "sparkx-write", 6, 12)
            return await run(sparkx_credentials.save_configuration, self.settings, payload)
        if path == "/console/api/sparkx/test":
            self._limit(request, "sparkx-test", 4, 8, "连接测试太频繁，请稍后重试。")
            result = await run(sparkx_credentials.check_connection, self.settings)
            if result["status"] == "OK":
                with self.session_factory() as db:
                    directory_module.record_roster(db, {row["profile_id"]: {k: row[k] for k in (
                        "name", "country", "store_type")} for row in result["profiles"]}, datetime.now(timezone.utc))
                    db.commit()
            return result
        if path == "/console/api/stores/refresh":
            self._limit(request, "stores-refresh", 4, 8, "读取 SparkX 店铺太频繁，请稍后重试。")
            return await run(self._refresh_stores)
        if path == "/console/api/stores/save":
            self._limit(request, "stores-save", 12, 24)
            return await run(self._save_stores, payload)
        if path in {"/console/api/recipients/company", "/console/api/recipients/ad"}:
            if set(payload) != {"open_ids", "revision"}:
                raise ConsoleError("提交内容无效。", 400)
            self._limit(request, "recipients", 8, 16)
            return await run(admin_feishu.save_lead, self.settings, path.rsplit("/", 1)[1],
                             payload["open_ids"], payload["revision"])
        if path == "/console/api/groups":
            if set(payload) != {"chat_id", "revision", "confirmed"}:
                raise ConsoleError("提交内容无效。", 400)
            self._limit(request, "groups-write", 6, 12)
            return await run(admin_feishu.save_group, self.settings, payload["chat_id"], payload["revision"],
                             payload["confirmed"])
        if path == "/console/api/members/refresh":
            self._limit(request, "members-refresh", 6, 12)
            return {"members": await run(admin_feishu.group_members, self.settings, refresh=True)}
        if path == "/console/api/reports":
            self._limit(request, "reports-write", 8, 16)
            await run(lane_settings.save, self.settings, payload)
            return await run(self._reports)
        if path == "/console/api/reports/preview":
            lane, config = await run(self._preview_config, payload)
            self._limit(request, "preview", 3, 6, "预览太频繁，请一分钟后重试。")
            return {"job": self.previews.start(lane, lambda: self._run_preview(lane, config)), "lane": lane}
        if path == "/console/api/deepseek":
            self._limit(request, "deepseek-write", 6, 12)
            return await run(deepseek_config.save_configuration, self.settings, payload)
        if path == "/console/api/deepseek/test":
            if payload:
                raise ConsoleError("连接测试不接受额外参数。", 400)
            self._limit(request, "deepseek-test", 4, 8, "连接测试太频繁，请稍后重试。")
            return await run(deepseek_config.check_connection, self.settings)
        if path == "/console/api/skills/install":
            if payload:
                raise ConsoleError("安装不接受额外参数。", 400)
            self._limit(request, "skills-install", 2, 4, "安装太频繁，请一分钟后重试。")
            return await run(self._install_skills)
        if path == "/console/api/skills/check":
            if payload:
                raise ConsoleError("检查不接受额外参数。", 400)
            self._limit(request, "skills-check", 4, 8, "检查太频繁，请稍后重试。")
            return await run(self._skills, True)
        raise ConsoleError("接口不存在。", 404)

    def routes(self):
        api_get = ["overview", "sparkx", "stores", "members", "recipients", "groups", "reports", "reports/preview",
                   "deepseek", "skills", "deliveries"]
        api_post = ["sparkx", "sparkx/test", "stores/refresh", "stores/save", "recipients/company", "recipients/ad",
                    "groups", "members/refresh", "reports", "reports/preview", "deepseek", "deepseek/test",
                    "skills/install", "skills/check"]
        routes = [Route("/", self.root), Route("/console", self.home), Route("/console/", self.home),
                  Route("/console/login", self.login, methods=["GET", "POST"]),
                  Route("/console/assets/{asset}", self.asset),
                  Route("/console/logout", self.api, methods=["POST"])]
        routes += [Route(path, self.retired, methods=["GET", "POST", "DELETE", "OPTIONS"]) for path in RETIRED_PATHS]
        for name in dict.fromkeys(api_get + api_post):
            methods = (["GET"] if name in api_get else []) + (["POST"] if name in api_post else [])
            routes.append(Route(f"/console/api/{name}", self.api, methods=methods))
        return routes


def create_browser_app():
    from jx_alert.config import get_settings

    settings = get_settings()
    console = OwnerConsole(settings, OwnerPassword(settings.console_owner_credentials_file),
                           SessionStore(settings.console_sessions_file))
    return Starlette(routes=console.routes())


if __name__ == "__main__":
    import uvicorn
    from jx_alert.config import get_settings

    uvicorn.run(create_browser_app(), host="0.0.0.0", port=8002, access_log=False,
                proxy_headers=True, forwarded_allow_ips=get_settings().console_forwarded_allow_ips)
