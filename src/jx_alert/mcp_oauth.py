"""Optional single-owner OAuth provider for the MCP SDK (no business DB access).

The SDK owns DCR, client authentication, PKCE and OAuth protocol responses.
This module supplies persistence and an explicit owner login/consent screen.
On POSIX, the credential and SQLite files must be owner-owned, private (0600),
and in a directory other users cannot modify. No default password is provided.
Access, refresh, authorization, browser-session and CSRF tokens are stored hashed.
DCR client secrets remain in the private DB because the SDK authenticator needs
the original secret. Do not log this database or protocol request bodies.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
from ipaddress import ip_address
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizeError,
    OAuthAuthorizationServerProvider, RefreshToken, RegistrationError, TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

SCOPES = ("jx:read", "jx:collect", "jx:notify")
COOKIE = "__Host-jx-oauth-owner"
_SECURE_HEADERS = {
    "Cache-Control": "no-store", "Pragma": "no-cache", "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
}
_OWNER_PAGE_HEADERS = {**_SECURE_HEADERS, "Referrer-Policy": "same-origin"}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _callback_origin(value: str) -> str:
    """Build a CSP source from a registered URI without copying path/query/header syntax."""
    parts = urlsplit(str(AnyHttpUrl(value)))
    host = parts.hostname or ""
    if parts.username or parts.password or parts.fragment:
        raise ValueError("Invalid callback URI")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("Invalid callback scheme")
    try:
        address = ip_address(host)
    except ValueError:
        if not host or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                           for label in host.rstrip(".").split(".")):
            raise ValueError("Invalid callback hostname") from None
        authority = host.lower()
    else:
        authority = f"[{address.compressed}]" if address.version == 6 else str(address)
    port = parts.port
    if port is not None:
        authority += f":{port}"
    return f"{parts.scheme}://{authority}"


def _consent_headers(redirect_uri: str) -> dict[str, str]:
    # Chromium also applies form-action to the POST's redirect destination.
    # Only the authenticated consent page permits this registered callback origin;
    # owner passwords are still submitted under the self-only login policy.
    origin = _callback_origin(redirect_uri)
    return {**_SECURE_HEADERS, "Content-Security-Policy": _SECURE_HEADERS["Content-Security-Policy"].replace(
        "form-action 'self';", f"form-action 'self' {origin};", 1)}


def _https_url(value: str) -> str:
    try:
        url = AnyHttpUrl(value)
        parts = urlsplit(str(url))
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError
        if parts.query or parts.fragment:
            raise ValueError
        return str(url)
    except (ValueError, TypeError):
        raise ValueError("OAuth issuer and resource must be absolute HTTPS URLs without credentials or query") from None


def _private_file(path: Path, *, create: bool = False) -> None:
    if not path.is_absolute():
        raise ValueError("OAuth private file paths must be absolute")
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    try:
        info = path.lstat()
        parent = path.parent.stat()
    except OSError:
        raise ValueError("OAuth private file is unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or path.parent.is_symlink():
        raise ValueError("OAuth private files must be regular files, not symbolic links")
    if os.name == "posix" and (
        info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
        or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise ValueError("OAuth private files require owner ownership and mode 0600 in a protected directory")


def create_owner_credentials(path: str | Path, password: str) -> None:
    """Write a new PBKDF2 hash once. The caller must obtain the password securely."""
    if not isinstance(password, str) or not 16 <= len(password) <= 1024:
        raise ValueError("OAuth owner password must contain 16 to 1024 characters")
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("OAuth private file paths must be absolute")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    salt = secrets.token_hex(32)
    rounds = 600_000
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), rounds).hex()
    payload = json.dumps({"algorithm": "pbkdf2_sha256", "rounds": rounds, "salt": salt, "hash": derived})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload)
    _private_file(path)


class _Connection:
    """Close connections without contextlib mutating frozen SDK exceptions."""
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, error_type, error, traceback):
        try:
            if error_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
        return False


class SqliteOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, *, database_path: str | Path, credentials_path: str | Path,
                 issuer_url: str, resource_url: str, access_token_ttl: int = 900,
                 refresh_token_ttl: int = 2_592_000):
        self.issuer_url = _https_url(issuer_url)
        self.resource_url = _https_url(resource_url)
        if urlsplit(self.issuer_url).path not in ("", "/"):
            raise ValueError("OAuth issuer must be an HTTPS origin")
        if not 60 <= access_token_ttl <= 3600 or not 300 <= refresh_token_ttl <= 2_592_000:
            raise ValueError("OAuth token lifetime is outside the permitted range")
        self.access_token_ttl, self.refresh_token_ttl = access_token_ttl, refresh_token_ttl
        self.database_path = Path(database_path)
        credential_path = Path(credentials_path)
        _private_file(credential_path)
        try:
            credential = json.loads(credential_path.read_text(encoding="utf-8"))
            if (credential["algorithm"] != "pbkdf2_sha256"
                    or type(credential["rounds"]) is not int
                    or not 600_000 <= credential["rounds"] <= 2_000_000
                    or not re.fullmatch(r"[0-9a-f]{64}", credential["salt"])
                    or not re.fullmatch(r"[0-9a-f]{64}", credential["hash"])):
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError):
            raise ValueError("OAuth owner credential file is invalid") from None
        self._credential = credential
        _private_file(self.database_path, create=True)
        self._rates: dict[tuple[str, str], tuple[int, int]] = {}
        self._rate_lock = threading.Lock()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY, data TEXT NOT NULL,
                    expires REAL NOT NULL, browser TEXT);
                CREATE TABLE IF NOT EXISTS codes(id TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tokens(id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    family TEXT NOT NULL, data TEXT NOT NULL, expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS tokens_family ON tokens(family);
                CREATE TABLE IF NOT EXISTS used_refresh(id TEXT PRIMARY KEY, family TEXT NOT NULL,
                    client_id TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS browsers(id TEXT PRIMARY KEY, csrf TEXT NOT NULL,
                    authenticated INTEGER NOT NULL, expires REAL NOT NULL);
            """)
            for key, value in (("issuer", self.issuer_url), ("resource", self.resource_url)):
                saved = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
                if saved and saved[0] != value:
                    raise ValueError("OAuth database belongs to a different issuer or resource")
                db.execute("INSERT OR IGNORE INTO config VALUES(?,?)", (key, value))

    def _db(self):
        _private_file(self.database_path)
        db = sqlite3.connect(self.database_path, timeout=5)
        db.row_factory = sqlite3.Row
        return _Connection(db)

    @staticmethod
    def _prune(db):
        for table in ("pending", "codes", "tokens", "browsers", "used_refresh"):
            db.execute(f"DELETE FROM {table} WHERE expires<=?", (time.time(),))

    def _rate_allowed(self, ip: str, path: str) -> bool:
        minute = int(time.time()) // 60
        limits = {"/oauth/owner": (12, 120), "/register": (5, 60)}
        local_limit, global_limit = limits.get(path, (60, 600))
        with self._rate_lock:
            self._rates = {key: value for key, value in self._rates.items() if value[0] == minute}
            keys = ((path, "*"), (path, ip[:100]))
            if len(self._rates) >= 1024 and keys[1] not in self._rates:
                return False
            for key, limit in zip(keys, (global_limit, local_limit)):
                if self._rates.get(key, (minute, 0))[1] >= limit:
                    return False
            for key in keys:
                self._rates[key] = (minute, self._rates.get(key, (minute, 0))[1] + 1)
        return True

    def auth_guard(self, app):
        """Wrap the final app: bounded auth requests and strict resource/PKCE checks.

        SDK 1.30's token handler checks PKCE but does not compare a token request's
        resource parameter. Metadata is derived from the SDK with public-client
        support and issuer identification explicitly advertised.
        """
        provider = self

        async def guarded(scope, receive, send):
            path = scope.get("path", "")
            if scope["type"] != "http" or path not in {
                "/authorize", "/token", "/register", "/revoke", "/oauth/owner",
                "/.well-known/oauth-authorization-server",
            }:
                return await app(scope, receive, send)
            request = Request(scope, receive)
            if scope.get("scheme") != "https":
                return await JSONResponse({"error": "HTTPS required"}, 400, headers=_SECURE_HEADERS)(scope, receive, send)
            if path == "/.well-known/oauth-authorization-server" and request.method == "GET":
                metadata = build_metadata(
                    AnyHttpUrl(provider.issuer_url), None,
                    ClientRegistrationOptions(enabled=True, valid_scopes=list(SCOPES), default_scopes=["jx:read"]),
                    RevocationOptions(enabled=True),
                ).model_dump(mode="json", exclude_none=True)
                metadata["token_endpoint_auth_methods_supported"] = ["none", "client_secret_post", "client_secret_basic"]
                metadata["authorization_response_iss_parameter_supported"] = True
                return await JSONResponse(metadata, headers=_SECURE_HEADERS)(scope, receive, send)
            ip = request.client.host if request.client else "unknown"
            if request.method != "OPTIONS" and not provider._rate_allowed(ip, path):
                return await JSONResponse({"error": "Too many authentication requests"}, 429,
                                          headers={**_SECURE_HEADERS, "Retry-After": "60"})(scope, receive, send)
            body = b""
            if request.method == "POST":
                try:
                    async for chunk in request.stream():
                        body += chunk
                        if len(body) > 16_384:
                            return await JSONResponse({"error": "Request too large"}, 413,
                                                      headers=_SECURE_HEADERS)(scope, receive, send)
                except Exception:
                    return await JSONResponse({"error": "Invalid request"}, 400, headers=_SECURE_HEADERS)(scope, receive, send)
            try:
                encoded = scope.get("query_string", b"") if request.method == "GET" else body
                if len(encoded) > 16_384:
                    raise ValueError
                if path == "/register" and request.method == "POST":
                    if not isinstance(json.loads(body), dict):
                        raise ValueError
                pairs = parse_qsl(encoded.decode("utf-8"), keep_blank_values=True, max_num_fields=30)
                params = dict(pairs)
                if path != "/register" and len(params) != len(pairs):
                    raise ValueError
                if path == "/revoke" and request.method == "POST" and "client_secret" not in params:
                    # SDK 1.30's revocation model has an Optional field without
                    # a default. An empty field permits public/Basic clients;
                    # the SDK still performs its normal client authentication.
                    body += b"&client_secret="
                if request.method != "OPTIONS" and path == "/authorize" and params.get("code_challenge_method") != "S256":
                    raise ValueError
                if request.method != "OPTIONS" and path in {"/authorize", "/token"} and params.get("resource") != provider.resource_url:
                    return await JSONResponse({
                        "error": "invalid_target",
                        "error_description": (
                            "连接地址应填写 MCP 接口 " + provider.resource_url
                            + "。授权信息页不是 MCP 接口；请在连接设置中更正地址后重新发起授权，"
                            "不要修改或复用旧授权链接。"
                        ),
                        "expected_resource": provider.resource_url,
                    }, 400, headers=_SECURE_HEADERS)(scope, receive, send)
            except (ValueError, UnicodeError):
                return await JSONResponse({"error": "invalid_request"}, 400, headers=_SECURE_HEADERS)(scope, receive, send)
            consumed = False

            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            async def issuer_response(message):
                # SDK-generated authorization error redirects also need RFC9207
                # issuer identification when advertised in our metadata.
                if path == "/authorize" and message["type"] == "http.response.start":
                    headers = []
                    for name, value in message.get("headers", []):
                        if name.lower() == b"location":
                            target = value.decode("latin-1")
                            if not target.startswith(provider.issuer_url.rstrip("/") + "/oauth/owner?"):
                                value = construct_redirect_uri(target, iss=provider.issuer_url).encode("latin-1")
                        headers.append((name, value))
                    message = {**message, "headers": headers}
                await send(message)

            return await app(scope, replay, issuer_response)
        return guarded

    async def register_client(self, client_info):
        try:
            if client_info.token_endpoint_auth_method not in {"none", "client_secret_post", "client_secret_basic"}:
                raise ValueError
            scopes = set((client_info.scope or "jx:read").split())
            if not scopes or not scopes <= set(SCOPES) or "jx:read" not in scopes:
                raise ValueError
            if set(client_info.grant_types) != {"authorization_code", "refresh_token"}:
                raise ValueError
            if client_info.response_types != ["code"] or not client_info.client_id:
                raise ValueError
            for uri in client_info.redirect_uris or []:
                parts = urlsplit(str(uri))
                loopback = parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}
                if not (parts.scheme == "https" or loopback) or parts.username or parts.password or parts.fragment:
                    raise ValueError
                _callback_origin(str(uri))
            if not client_info.redirect_uris or len(client_info.redirect_uris) > 10:
                raise ValueError
            if client_info.token_endpoint_auth_method != "none" and not client_info.client_secret:
                raise ValueError
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT COUNT(*) FROM clients").fetchone()[0] >= 1000:
                    raise ValueError
                db.execute("INSERT INTO clients VALUES(?,?)", (client_info.client_id, client_info.model_dump_json()))
        except (ValueError, sqlite3.IntegrityError):
            raise RegistrationError("invalid_client_metadata", "Client registration was rejected") from None

    async def get_client(self, client_id):
        with self._db() as db:
            row = db.execute("SELECT data FROM clients WHERE id=?", (client_id,)).fetchone()
        if not row:
            return None
        client = OAuthClientInformationFull.model_validate_json(row[0])
        # ChatGPT initially registers this connection for read access. Permit
        # a subsequent authorization request for notifications on the same
        # client. This is client eligibility, not a grant: the owner must
        # approve the new scope and refresh still uses the original token scopes.
        redirects = [str(uri) for uri in client.redirect_uris or []]
        if client.scope == "jx:read" and redirects and all(
            re.fullmatch(r"https://chatgpt\.com/(?:connector_platform_oauth_redirect|connector/oauth/[A-Za-z0-9_-]+)", uri)
            for uri in redirects
        ):
            client = client.model_copy(update={"scope": "jx:read jx:notify"})
        return client

    def _scopes(self, scopes, client):
        wanted = list(dict.fromkeys(scopes or ["jx:read"]))
        allowed = set((client.scope or "jx:read").split())
        if "jx:read" not in wanted or not set(wanted) <= set(SCOPES) & allowed:
            raise ValueError("Invalid scope")
        return wanted

    async def authorize(self, client, params):
        try:
            if params.resource != self.resource_url or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
                raise ValueError
            client.validate_redirect_uri(params.redirect_uri)
            scopes = self._scopes(params.scopes, client)
        except ValueError:
            raise AuthorizeError("invalid_request", "Authorization request was rejected") from None
        pending_id = secrets.token_urlsafe(32)
        data = {**params.model_dump(mode="json"), "client_id": client.client_id, "scopes": scopes}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune(db)
            if db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] >= 512:
                raise AuthorizeError("temporarily_unavailable", "Authorization is busy")
            db.execute("INSERT INTO pending VALUES(?,?,?,NULL)",
                       (_digest(pending_id), json.dumps(data), time.time() + 600))
        return self.issuer_url.rstrip("/") + "/oauth/owner?request=" + pending_id

    async def load_authorization_code(self, client, authorization_code):
        with self._db() as db:
            row = db.execute("SELECT data,expires FROM codes WHERE id=?", (_digest(authorization_code),)).fetchone()
        if not row or row["expires"] <= time.time():
            return None
        data = json.loads(row["data"])
        if data["client_id"] != client.client_id or data["resource"] != self.resource_url:
            return None
        return AuthorizationCode(code=authorization_code, expires_at=row["expires"], **data)

    def _issue(self, db, *, client_id, scopes, family=None):
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        family = family or secrets.token_hex(24)
        now = int(time.time())
        data = json.dumps({"client_id": client_id, "scopes": scopes,
                           "resource": self.resource_url, "subject": "owner"})
        for token, kind, ttl in ((access, "access", self.access_token_ttl),
                                 (refresh, "refresh", self.refresh_token_ttl)):
            db.execute("INSERT INTO tokens VALUES(?,?,?,?,?)", (_digest(token), kind, family, data, now + ttl))
        return OAuthToken(access_token=access, refresh_token=refresh, expires_in=self.access_token_ttl,
                          scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data,expires FROM codes WHERE id=?", (_digest(authorization_code.code),)).fetchone()
            if not row or row["expires"] <= time.time():
                raise TokenError("invalid_grant", "Authorization code is unavailable")
            data = json.loads(row["data"])
            if data["client_id"] != client.client_id or data["resource"] != self.resource_url:
                raise TokenError("invalid_grant", "Authorization code is unavailable")
            db.execute("DELETE FROM codes WHERE id=?", (_digest(authorization_code.code),))
            try:
                scopes = self._scopes(data["scopes"], client)
            except ValueError:
                raise TokenError("invalid_scope", "Scope is not granted") from None
            return self._issue(db, client_id=client.client_id, scopes=scopes)

    def _load_token(self, raw, kind):
        with self._db() as db:
            row = db.execute("SELECT data,expires FROM tokens WHERE id=? AND kind=?", (_digest(raw), kind)).fetchone()
        if not row or row["expires"] <= time.time():
            return None
        data = json.loads(row["data"])
        if data["resource"] != self.resource_url or not set(data["scopes"]) <= set(SCOPES):
            return None
        cls = AccessToken if kind == "access" else RefreshToken
        return cls(token=raw, expires_at=int(row["expires"]), **data)

    async def load_access_token(self, token):
        return self._load_token(token, "access")

    async def load_refresh_token(self, client, refresh_token):
        token = self._load_token(refresh_token, "refresh")
        if token is None:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                self._revoke_refresh_replay(db, client, refresh_token)
        return token if token and token.client_id == client.client_id else None

    @staticmethod
    def _revoke_refresh_replay(db, client, raw):
        used = db.execute("SELECT family FROM used_refresh WHERE id=? AND client_id=? AND expires>?",
                          (_digest(raw), client.client_id, time.time())).fetchone()
        if used:
            db.execute("DELETE FROM tokens WHERE family=?", (used["family"],))

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune(db)
            row = db.execute("SELECT * FROM tokens WHERE id=? AND kind='refresh'", (_digest(refresh_token.token),)).fetchone()
            if not row or row["expires"] <= time.time():
                self._revoke_refresh_replay(db, client, refresh_token.token)
            else:
                data = json.loads(row["data"])
                if data["client_id"] != client.client_id or data["resource"] != self.resource_url:
                    raise TokenError("invalid_grant", "Refresh token is unavailable")
                try:
                    scopes = self._scopes(scopes, client)
                    if not set(scopes) <= set(data["scopes"]):
                        raise ValueError
                except ValueError:
                    raise TokenError("invalid_scope", "Scope is not granted") from None
                db.execute("INSERT INTO used_refresh VALUES(?,?,?,?)",
                           (_digest(refresh_token.token), row["family"], client.client_id, row["expires"]))
                db.execute("DELETE FROM tokens WHERE family=?", (row["family"],))
                return self._issue(db, client_id=client.client_id, scopes=scopes, family=row["family"])
        # Commit replay-family revocation before returning the protocol error.
        raise TokenError("invalid_grant", "Refresh token is unavailable")

    async def revoke_token(self, token):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT family FROM tokens WHERE id=?", (_digest(token.token),)).fetchone()
            if row:
                db.execute("DELETE FROM tokens WHERE family=?", (row["family"],))

    def owner_routes(self) -> list[Route]:
        return [Route("/oauth/owner", self._owner, methods=["GET", "POST"])]

    def _password_valid(self, password):
        value = self._credential
        derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(value["salt"]), value["rounds"]).hex()
        return hmac.compare_digest(derived, value["hash"])

    @staticmethod
    def _page(*, pending_id, csrf, authenticated, data, error=False):
        def esc(value):
            return html.escape(str(value), quote=True)
        fields = f'<input type="hidden" name="request" value="{esc(pending_id)}"><input type="hidden" name="csrf" value="{esc(csrf)}">'
        if authenticated:
            scopes = "、".join({"jx:read": "读取已采集的数据", "jx:collect": "请求采集与分析任务",
                               "jx:notify": "将有数据证据的中文异常分析发送至已配置飞书群"}[s] for s in data["scopes"])
            content = f'<p>客户端：{esc(data["client_id"])}</p><p>回调：{esc(data["redirect_uri"])}</p><p>权限：{esc(scopes)}</p><button name="action" value="approve">同意</button><button name="action" value="deny">拒绝</button>'
        else:
            content = '<label>所有者口令 <input type="password" name="password" autocomplete="current-password" required maxlength="1024"></label><button name="action" value="login">登录</button>'
        message = "<p>登录未成功，请重试。</p>" if error else ""
        # no-referrer makes a native form POST send Origin: null in browsers,
        # which our strict same-origin CSRF check must reject. same-origin keeps
        # the legitimate Origin and still suppresses referrers to other sites.
        headers = ({**_consent_headers(data["redirect_uri"]), "Referrer-Policy": "same-origin"}
                   if authenticated else _OWNER_PAGE_HEADERS)
        return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>JX Alert 授权</title><h1>JX Alert 授权</h1>' + message + '<form method="post" action="/oauth/owner">' + fields + content + '</form></html>', headers=headers)

    async def _owner(self, request: Request):
        def failure():
            return HTMLResponse("授权请求无效或已过期，请重新连接。", 403, headers=_SECURE_HEADERS)
        if request.method == "POST":
            origin = request.headers.get("origin", "")
            expected = urlsplit(self.issuer_url)
            if origin != f"{expected.scheme}://{expected.netloc}":
                return failure()
            form = await request.form()
            pending_id, csrf = str(form.get("request", "")), str(form.get("csrf", ""))
        else:
            form = {}
            pending_id, csrf = request.query_params.get("request", ""), ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", pending_id):
            return failure()
        browser = request.cookies.get(COOKIE, "")
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune(db)
            pending = db.execute("SELECT * FROM pending WHERE id=?", (_digest(pending_id),)).fetchone()
            if not pending:
                return failure()
            data = json.loads(pending["data"])
            session = db.execute("SELECT * FROM browsers WHERE id=?", (_digest(browser),)).fetchone() if browser else None
            if request.method == "POST":
                if (not session or pending["browser"] != _digest(browser)
                        or not hmac.compare_digest(session["csrf"], _digest(csrf))):
                    return failure()
                authenticated = bool(session["authenticated"])
            else:
                if not session:
                    if db.execute("SELECT COUNT(*) FROM browsers").fetchone()[0] >= 512:
                        return failure()
                    browser = secrets.token_urlsafe(48)
                    session = {"authenticated": 0}
                    db.execute("INSERT INTO browsers VALUES(?,?,0,?)", (_digest(browser), "", now + 600))
                if pending["browser"] not in (None, _digest(browser)):
                    return failure()
                authenticated = bool(session["authenticated"])
                db.execute("UPDATE pending SET browser=? WHERE id=?", (_digest(browser), _digest(pending_id)))
        if request.method == "POST" and form.get("action") == "login":
            password = form.get("password", "")
            if not isinstance(password, str) or len(password) > 1024:
                return failure()
            valid = await asyncio.to_thread(self._password_valid, password)
            if valid:
                new_browser = secrets.token_urlsafe(48)
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("DELETE FROM browsers WHERE id=?", (_digest(browser),))
                    db.execute("INSERT INTO browsers VALUES(?,?,1,?)", (_digest(new_browser), "", now + 600))
                    db.execute("UPDATE pending SET browser=? WHERE id=? AND browser=?", (_digest(new_browser), _digest(pending_id), _digest(browser)))
                browser, authenticated = new_browser, True
            csrf = secrets.token_urlsafe(32)
            with self._db() as db:
                db.execute("UPDATE browsers SET csrf=? WHERE id=?", (_digest(csrf), _digest(browser)))
            response = self._page(pending_id=pending_id, csrf=csrf, authenticated=authenticated, data=data, error=not valid)
        elif request.method == "POST":
            if not authenticated or form.get("action") not in {"approve", "deny"}:
                return failure()
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                session = db.execute("SELECT * FROM browsers WHERE id=? AND authenticated=1 AND expires>?",
                                     (_digest(browser), time.time())).fetchone()
                if not session or not hmac.compare_digest(session["csrf"], _digest(csrf)):
                    return failure()
                used = db.execute("DELETE FROM pending WHERE id=? AND browser=? AND expires>?", (_digest(pending_id), _digest(browser), time.time()))
                if used.rowcount != 1:
                    return failure()
                if form["action"] == "approve":
                    code = secrets.token_urlsafe(48)
                    code_data = {key: value for key, value in data.items() if key != "state"}
                    code_data["subject"] = "owner"
                    db.execute("INSERT INTO codes VALUES(?,?,?)", (_digest(code), json.dumps(code_data), time.time() + 120))
                    target = construct_redirect_uri(data["redirect_uri"], code=code, state=data.get("state"), iss=self.issuer_url)
                else:
                    target = construct_redirect_uri(data["redirect_uri"], error="access_denied", state=data.get("state"), iss=self.issuer_url)
            response = RedirectResponse(target, 302, headers=_consent_headers(data["redirect_uri"]))
        else:
            csrf = secrets.token_urlsafe(32)
            with self._db() as db:
                db.execute("UPDATE browsers SET csrf=? WHERE id=?", (_digest(csrf), _digest(browser)))
            response = self._page(pending_id=pending_id, csrf=csrf, authenticated=authenticated, data=data)
        response.set_cookie(COOKIE, browser, secure=True, httponly=True, samesite="lax", max_age=600, path="/")
        return response
