"""Run only OwnerConsole against disposable synthetic data on loopback."""
import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.applications import Starlette
from starlette.middleware import Middleware
import uvicorn


def _no_outbound(event, _args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname"}:
        raise RuntimeError("The browser smoke fixture cannot call any external service")


def _tls_files(root):
    # Ephemeral self-signed certificate, valid for this loopback IP only.
    key = ec.generate_private_key(ec.SECP256R1())
    identity = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "JX isolated test fixture")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(identity).issuer_name(identity)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False).sign(key, hashes.SHA256()))
    cert_path, key_path = root / "cert.pem", root / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return cert_path, key_path


class _ProxyProbe:
    """Tag responses made by the app so tests can identify edge-only rejection."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def stamped(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message["headers"], (b"x-jx-upstream-fixture", b"1")]}
            await send(message)

        await self.app(scope, receive, stamped)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--behind-loopback-proxy", action="store_true")
    behind = parser.parse_args().behind_loopback_proxy
    root = Path.cwd()
    if any(root.iterdir()):
        raise SystemExit("Provide an empty temporary working directory for the owner fixture")
    os.umask(0o077)
    sys.addaudithook(_no_outbound)

    # Override even the import-time settings lookup; never read a checkout's .env,
    # mounted app config, or the default production warehouse.
    from jx_alert import config

    paths = {name: str(root / name) for name in (
        "runtime_settings_file", "mcp_auth_file", "collection_scope_file", "store_timezones_file",
        "store_controls_file", "feishu_routing_file", "lingxing_sources_file",
        "lingxing_business_credentials_file", "deepseek_credentials_file", "sparkx_credentials_file",
        "ad_review_skills_file", "agent_skill_library_dir", "advertising_source_controls_file",
    )}
    settings = config.Settings(_env_file=None, database_url="sqlite://", app_dry_run=True,
                               scheduler_enabled=False, feishu_enabled=False, **paths)
    config.get_settings = lambda: settings

    from jx_alert.admin_console import OwnerConsole
    from jx_alert.storage.db import Base
    from jx_alert.storage.models import Store

    Path(settings.collection_scope_file).write_text('{"store_ids": [11]}', encoding="utf-8")
    Path(settings.store_timezones_file).write_text(
        '{"stores": {"11": {"timezone": "America/Los_Angeles", "currency_code": "USD"}}}',
        encoding="utf-8",
    )
    engine = create_engine(f"sqlite:///{root / 'demo.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as db:
        db.add(Store(sid="11", name="离线示例店铺", status=1, has_ads_setting=1,
                     timezone="America/Los_Angeles", currency_code="USD"))

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        # The checked-in proxy forwards $host (no port). Model the production
        # canonical HTTPS origin independently of disposable listener ports.
        origin = "https://127.0.0.1" if behind else f"https://127.0.0.1:{port}"
        provider = SimpleNamespace(
            issuer_url=origin + "/", database_path=root / "oauth_unused.sqlite",
            _password_valid=lambda value: value == "test-fixture-only-password",
        )
        app = Starlette(routes=OwnerConsole(settings, provider, session_factory=sessions).routes(),
                        middleware=[Middleware(_ProxyProbe)] if behind else [])
        cert, key = _tls_files(root)

        class FixtureServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                if self.started:
                    print(json.dumps({"origin": origin, "listen_port": port}), flush=True)

        tls = {} if behind else {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        server = FixtureServer(uvicorn.Config(app, **tls, access_log=False, proxy_headers=behind,
                                              forwarded_allow_ips="127.0.0.1", log_level="warning"))
        server.run(sockets=[listener])
    engine.dispose()


if __name__ == "__main__":
    main()
