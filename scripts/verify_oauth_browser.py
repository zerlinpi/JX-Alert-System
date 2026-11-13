"""Isolated native-Chromium OAuth form regression; never uses production state."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import ipaddress
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Route
import uvicorn
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import jx_alert.mcp_oauth as oauth  # noqa: E402 - load this checkout after adding its src path.


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def certificate(folder):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    spki = key.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)
    return cert_path, key_path, base64.b64encode(hashlib.sha256(spki).digest()).decode()


class Devtools:
    def __init__(self, ws, deadline):
        self.ws, self.counter, self.events, self.deadline = ws, 0, [], deadline

    def call(self, method, params=None):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Isolated browser verification deadline exceeded")
        self.counter += 1
        request_id = self.counter
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        stop = min(time.monotonic() + 3, self.deadline)
        while time.monotonic() < stop:
            message = json.loads(self.ws.recv(timeout=max(0.01, min(3, self.deadline - time.monotonic()))))
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError("Devtools command failed")
                return message.get("result", {})
            self.events.append(message)
        raise TimeoutError("Devtools response timeout")

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if result.get("exceptionDetails"):
            raise RuntimeError("Browser expression failed")
        return result.get("result", {}).get("value")

    def wait(self, expression, seconds=5):
        stop = min(time.monotonic() + seconds, self.deadline)
        while time.monotonic() < stop:
            if self.evaluate(expression):
                return
            time.sleep(0.08)
        raise TimeoutError("Browser condition timeout")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-blocked", action="store_true")
    parser.add_argument("--browser", help="Path to an installed Chrome or Chromium executable")
    parser.add_argument("--output", type=Path, default=ROOT / "work" / "oauth-browser-verification.json")
    args = parser.parse_args()
    installed = args.browser or shutil.which("chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not installed and os.name == "nt":
        fallback = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe"
        installed = str(fallback) if fallback.is_file() else None
    if not installed or not Path(shutil.which(installed) or installed).is_file():
        parser.error("An installed Chrome/Chromium is required; pass --browser explicitly")
    browser_path = shutil.which(installed) or installed
    started = time.monotonic()
    deadline = started + 32
    stage = "prepare"
    chrome = None
    servers = []
    timed_out = threading.Event()

    def expire():
        timed_out.set()
        for server in servers:
            server.should_exit = True
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()

    watchdog = threading.Timer(32, expire)
    watchdog.daemon = True
    watchdog.start()
    summary = {"mode": "original_csp_negative" if args.expect_blocked else "fixed_csp_positive",
               "browser": "isolated_headless_chromium", "production_accessed": False,
               "web_security_disabled": False, "certificate_trust": "temporary_spki_pin_only"}
    try:
        with tempfile.TemporaryDirectory(prefix="jx-oauth-browser-", ignore_cleanup_errors=True) as name:
            folder = Path(name)
            cert, key, pin = certificate(folder)
            auth_port, callback_port = port(), port()
            while callback_port == auth_port:
                callback_port = port()
            origin = f"https://127.0.0.1:{auth_port}"
            callback = f"https://localhost:{callback_port}/callback"
            resource = origin + "/mcp"
            password = secrets.token_urlsafe(32)
            owner = folder / "owner.json"
            oauth.create_owner_credentials(owner, password)
            provider = oauth.SqliteOAuthProvider(database_path=folder / "oauth.sqlite", credentials_path=owner,
                                                issuer_url=origin, resource_url=resource)
            if args.expect_blocked:
                def original_csp(_uri):
                    value = dict(oauth._OWNER_PAGE_HEADERS)
                    value["Content-Security-Policy"] = oauth._SECURE_HEADERS["Content-Security-Policy"]
                    return value
                oauth._consent_headers = original_csp
            arrived = {}

            async def receive_callback(request):
                arrived.update(dict(request.query_params))
                return HTMLResponse("Callback received", headers={"Cache-Control": "no-store"})

            app = provider.auth_guard(Starlette(routes=create_auth_routes(
                provider, AnyHttpUrl(origin), client_registration_options=ClientRegistrationOptions(
                    enabled=True, valid_scopes=list(oauth.SCOPES), default_scopes=["jx:read"]),
                revocation_options=RevocationOptions(enabled=True)) + provider.owner_routes()))
            for app_instance, selected_port in [(app, auth_port),
                    (Starlette(routes=[Route("/callback", receive_callback)]), callback_port)]:
                server = uvicorn.Server(uvicorn.Config(app_instance, host="127.0.0.1", port=selected_port,
                    ssl_certfile=str(cert), ssl_keyfile=str(key), log_level="critical", access_log=False))
                servers.append(server)
                threading.Thread(target=server.run, daemon=True).start()
            with httpx.Client(verify=str(cert), trust_env=False, timeout=3, follow_redirects=False) as http:
                stage = "local_tls_readiness"
                stop = time.monotonic() + 5
                while True:
                    try:
                        response = http.get(origin + "/.well-known/oauth-authorization-server")
                        summary["local_tls_responding"] = True
                        summary["oauth_metadata_ready"] = response.status_code == 200
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError as exc:
                        summary["local_tls_error_type"] = type(exc).__name__
                        summary["certificate_verification_failed"] = "CERTIFICATE_VERIFY_FAILED" in str(exc)
                    if time.monotonic() > stop:
                        raise TimeoutError("Local TLS service readiness timeout")
                    time.sleep(0.1)
                stage = "register"
                registration = http.post(origin + "/register", json={
                    "redirect_uris": [callback], "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"], "scope": "jx:read"})
                if registration.status_code != 201:
                    raise RuntimeError("Synthetic registration failed")
                client_id = registration.json()["client_id"]
                verifier = secrets.token_urlsafe(48)
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                state = secrets.token_urlsafe(24)
                authorization_url = origin + "/authorize?" + urlencode({
                    "client_id": client_id, "redirect_uri": callback, "response_type": "code",
                    "code_challenge": challenge, "code_challenge_method": "S256",
                    "resource": resource, "scope": "jx:read", "state": state})
                stage = "launch_browser"
                profile = folder / "chrome-profile"
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                chrome = subprocess.Popen([browser_path,
                    "--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                    "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                    "--user-data-dir=" + str(profile),
                    "--ignore-certificate-errors-spki-list=" + pin, "about:blank"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
                active_port = profile / "DevToolsActivePort"
                stop = time.monotonic() + 7
                while not active_port.is_file():
                    if chrome.poll() is not None or time.monotonic() > stop:
                        raise RuntimeError("Isolated browser startup failed")
                    time.sleep(0.1)
                debug_port = int(active_port.read_text().splitlines()[0])
                with httpx.Client(trust_env=False, timeout=3) as dev_http:
                    target = dev_http.put(f"http://127.0.0.1:{debug_port}/json/new?about:blank").json()
                with connect(target["webSocketDebuggerUrl"], open_timeout=3) as ws:
                    browser = Devtools(ws, deadline)
                    browser.call("Page.enable")
                    browser.call("Runtime.enable")
                    browser.call("Log.enable")
                    browser.call("Page.navigate", {"url": authorization_url})
                    stage = "native_login"
                    browser.wait("Boolean(document.querySelector('input[type=password]'))")
                    browser.evaluate("document.querySelector('input[type=password]').value=" + json.dumps(password))
                    browser.evaluate("document.querySelector('button[value=login]').click()")
                    browser.wait("Boolean(document.querySelector('button[value=approve]'))")
                    summary["native_password_form_login_succeeded"] = True
                    stage = "native_consent"
                    browser.evaluate("document.querySelector('button[value=approve]').click()")
                    stop = time.monotonic() + (2 if args.expect_blocked else 5)
                    while time.monotonic() < stop and not arrived:
                        browser.evaluate("document.readyState")
                        time.sleep(0.1)
                    with provider._db() as db:
                        code_count = db.execute("SELECT COUNT(*) FROM codes").fetchone()[0]
                        pending_count = db.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
                    summary.update(callback_received=bool(arrived), authorization_code_created=code_count == 1,
                                   authorization_request_consumed=pending_count == 0)
                    summary["csp_violation_observed"] = any(
                        "form-action" in json.dumps(event) and event.get("method") == "Log.entryAdded"
                        for event in browser.events)
                    if args.expect_blocked:
                        if arrived or code_count != 1 or pending_count != 0 or not summary["csp_violation_observed"]:
                            raise AssertionError("Original CSP did not reproduce the expected callback block")
                        summary["pkce_token_exchange_succeeded"] = False
                    else:
                        if not arrived or arrived.get("state") != state or arrived.get("iss") != origin + "/":
                            raise AssertionError("Native callback did not complete with bound state and issuer")
                        stage = "pkce_exchange"
                        token = http.post(origin + "/token", data={"grant_type": "authorization_code",
                            "client_id": client_id, "code": arrived["code"], "redirect_uri": callback,
                            "code_verifier": verifier, "resource": resource})
                        if token.status_code != 200 or not token.json().get("access_token"):
                            raise AssertionError("PKCE token exchange failed")
                        summary["pkce_token_exchange_succeeded"] = True
                    summary["passed"] = True
                    try:
                        browser.call("Browser.close")
                    except ConnectionClosed:
                        pass
                    chrome.wait(timeout=3)
                    summary["isolated_browser_closed"] = True
    except Exception as exc:
        summary.update(passed=False, failure_stage=stage, error_type=type(exc).__name__)
    finally:
        watchdog.cancel()
        if timed_out.is_set():
            summary.update(passed=False, failure_stage=stage, error_type="TimeoutError")
        for server in servers:
            server.should_exit = True
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try:
                chrome.wait(timeout=3)
            except subprocess.TimeoutExpired:
                chrome.kill()
        summary["elapsed_seconds"] = round(time.monotonic() - started, 2)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        previous = json.loads(args.output.read_text(encoding="utf-8")) if args.output.exists() else {}
        previous[summary["mode"]] = summary
        args.output.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=True))
    return 0 if summary.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
