"""Rootless, real Nginx HTTPS tests with an isolated owner console. No live I/O.

With the project installed, opt in using JX_NGINX_ACCEPTANCE=1 and an absolute
JX_NGINX_BIN path to the official executable. Never pass a system config.
"""
import base64
from contextlib import contextmanager
import hashlib
from http.client import HTTPSConnection
from io import BytesIO
import json
import os
from pathlib import Path
import re
import selectors
import socket
import ssl
import subprocess
import sys
import time
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest

from jx_alert.agent_skill_package import MAX_REQUEST


ROOT = Path(__file__).resolve().parents[2]
UPLOAD = "/console/api/skill-library"
ORIGIN = "https://127.0.0.1"
PROBE = "x-jx-upstream-fixture"
pytestmark = pytest.mark.skipif(
    os.environ.get("JX_NGINX_ACCEPTANCE") != "1", reason="requires isolated rootless Nginx acceptance"
)


def _env(home):
    return {"PATH": os.defpath, "HOME": str(home), "PYTHONUTF8": "1"}


@contextmanager
def _running(args, *, cwd, error_log):
    with error_log.open("wb") as log:
        process = subprocess.Popen(args, cwd=cwd, env=_env(cwd), stdout=subprocess.PIPE,
                                   stderr=log, text=True)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


def _edit_once(text, before, after):
    assert text.count(before) == 1, f"Expected one checked-in directive: {before}"
    return text.replace(before, after)


def _nginx_config(directory, certs, *, edge_port, upstream_port):
    # The only change to the checked-in exact-location snippet is its
    # disposable loopback upstream port. Reuse the MCP vhost's 64 KiB default
    # and catch-all as written; replace its deployment/listener coordinates.
    deploy = ROOT / "deploy"
    snippet = (deploy / "nginx-owner-skill-library.location.example").read_text()
    snippet = _edit_once(snippet, "http://127.0.0.1:8002;", f"http://127.0.0.1:{upstream_port};")
    snippet_file = directory / "skill-library.location"
    snippet_file.write_text(snippet)
    vhost = (deploy / "nginx-mcp.conf.example").read_text()
    for before, after in (
        ("listen 443 ssl;", f"listen 127.0.0.1:{edge_port} ssl;"),
        ("server_name mcp.example.com;", "server_name 127.0.0.1;"),
        ("/etc/letsencrypt/live/mcp.example.com/fullchain.pem", f'"{certs / "cert.pem"}"'),
        ("/etc/letsencrypt/live/mcp.example.com/privkey.pem", f'"{certs / "key.pem"}"'),
        ("http://127.0.0.1:8001;", f"http://127.0.0.1:{upstream_port};"),
    ):
        vhost = _edit_once(vhost, before, after)
    assert vhost.rstrip().endswith("}")
    vhost = vhost.rstrip()[:-1] + f'    include "{snippet_file}";\n}}\n'
    # Bypass every compiled-in write location and run without a daemon or
    # privileged master in the temporary test prefix.
    temps = "\n".join(f'{kind}_temp_path "{directory / ("temp-" + kind)}";'
                      for kind in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
    conf = directory / "nginx.conf"
    conf.write_text(f'''daemon off;
master_process off;
pid "{directory / "nginx.pid"}";
error_log stderr warn;
events {{ worker_connections 32; }}
http {{
    access_log off;
    {temps}
    {vhost}
}}
''')
    return conf


@pytest.fixture
def proxy(tmp_path):
    assert hasattr(os, "geteuid") and os.geteuid() != 0, "Do not run this fixture as root"
    source = os.environ.get("JX_NGINX_BIN")
    assert source and Path(source).is_absolute() and os.access(source, os.X_OK), (
        "Pass an absolute path to an official Nginx executable in JX_NGINX_BIN"
    )
    upstream, edge = tmp_path / "upstream", tmp_path / "edge"
    upstream.mkdir()
    edge.mkdir()
    # The owner fixture requires its working directory to start empty.
    upstream_log = tmp_path / "owner-stderr.log"
    nginx_log = tmp_path / "nginx-stderr.log"
    fixture = ROOT / "tests/browser/serve_owner_fixture.py"
    with _running([sys.executable, str(fixture), "--behind-loopback-proxy"], cwd=upstream,
                  error_log=upstream_log) as app:
        with selectors.DefaultSelector() as selector:
            selector.register(app.stdout, selectors.EVENT_READ)
            assert selector.select(10), "Owner console fixture did not report readiness"
            ready = app.stdout.readline()
        assert ready, upstream_log.read_text()
        config = json.loads(ready)
        assert config["origin"] == ORIGIN
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        conf = _nginx_config(edge, upstream, edge_port=port, upstream_port=config["listen_port"])
        args = [source, "-e", "stderr", "-p", str(edge) + "/", "-c", str(conf)]
        parsed = subprocess.run([*args, "-t"], cwd=edge, env=_env(edge),
                                capture_output=True, text=True, timeout=10)
        assert parsed.returncode == 0, parsed.stderr
        with _running(args, cwd=edge, error_log=nginx_log) as nginx:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                assert nginx.poll() is None, nginx_log.read_text()
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                pytest.fail("Nginx did not bind its loopback HTTPS listener")
            verify = ssl.create_default_context(cafile=upstream / "cert.pem")
            yield port, verify


def _over_limit(port, verify, path, size):
    # Nginx rejects a declared oversized body from its headers. Don't send
    # the body after the expected 413 (some clients hit a write error first).
    conn = HTTPSConnection("127.0.0.1", port, context=verify, timeout=3)
    try:
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(size))
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 413 and response.getheader(PROBE) is None
        assert "nginx" in response.getheader("Server", "").lower()
    finally:
        conn.close()


def test_owner_skill_upload_https_limit_is_scoped_to_exact_path(proxy):
    port, verify = proxy
    fixture_files = {
        "proxy-fixture/SKILL.md": b"---\nname: proxy-fixture\ndescription: Isolated test.\n---\n# Fixture\n",
        # Bounded deterministic synthetic text: even after Deflate + base64 the HTTP body exceeds 64 KiB.
        "proxy-fixture/references/long.txt": "".join(hashlib.sha256(str(i).encode()).hexdigest()
                                                      for i in range(2000)).encode(),
    }
    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as z:
        for name, content in fixture_files.items():
            z.writestr(name, content)
    body = json.dumps({"action": "upload_package", "revision": 0,
                       "content_base64": base64.b64encode(archive.getvalue()).decode()}).encode()
    assert 65536 < len(body) < MAX_REQUEST
    common = {"content-type": "application/json", "origin": ORIGIN}
    download = f"{UPLOAD}/proxy-fixture/package.zip?version=1"
    with httpx.Client(base_url=f"{ORIGIN}:{port}", verify=verify, trust_env=False, timeout=5) as client:
        denied = client.post(UPLOAD, content=body, headers=common)
        assert denied.status_code == 401 and denied.headers[PROBE] == "1"
        login = client.get("/console/login")
        assert login.status_code == 200 and login.headers[PROBE] == "1"
        token = re.search(r'name="csrf"\s+value="([A-Za-z0-9_-]+)"', login.text)[1]
        success = client.post("/console/login", data={"password": "test-fixture-only-password", "csrf": token},
                              headers={"origin": ORIGIN})
        assert success.status_code == 303
        csrf = client.get("/console/api/stores").json()["csrf"]
        no_csrf = client.post(UPLOAD, content=body, headers=common)
        assert no_csrf.status_code == 403 and no_csrf.headers[PROBE] == "1"
        wrong_origin = client.post(UPLOAD, content=body, headers={
            **common, "x-csrf-token": csrf, "origin": "https://example.invalid",
        })
        assert wrong_origin.status_code == 403 and wrong_origin.headers[PROBE] == "1"
        assert client.get(UPLOAD).json()["skills"] == []
        valid = client.post(UPLOAD, content=body, headers={**common, "x-csrf-token": csrf})
        assert valid.status_code == 200 and valid.headers[PROBE] == "1"
        assert valid.json()["skills"][0]["enabled"] is False
        received = client.get(download)
        assert received.status_code == 200 and received.headers[PROBE] == "1"
        with ZipFile(BytesIO(received.content)) as z:
            assert {name: z.read(name) for name in z.namelist()} == fixture_files
        for unrelated in ("/mcp", UPLOAD + "/not-upload"):
            boundary = b" " * (65536 - 2) + b"{}"
            assert client.post(unrelated, content=boundary, headers=common).headers[PROBE] == "1"
            _over_limit(port, verify, unrelated, 65537)
        # At the exact cap the request reaches the application, which rejects
        # the intentionally incomplete JSON operation, not the transport size.
        boundary = b" " * (MAX_REQUEST - 2) + b"{}"
        malformed = client.post(UPLOAD, content=boundary, headers={**common, "x-csrf-token": csrf})
        assert malformed.status_code == 400 and malformed.headers[PROBE] == "1"
        _over_limit(port, verify, UPLOAD, MAX_REQUEST + 1)
        assert client.get(UPLOAD).json()["revision"] == 1
        assert client.post("/console/logout", headers={"origin": ORIGIN, "x-csrf-token": csrf}).status_code == 303
        assert client.get(download).status_code == 401
