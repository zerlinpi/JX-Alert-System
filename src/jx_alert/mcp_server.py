"""Read-only MCP tools for persisted JX Alert data.

No tool in this module contacts Lingxing or sends Feishu messages.
"""
from datetime import date
import argparse
import hmac
import re

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select, text

from jx_alert.config import get_settings
from jx_alert.storage.db import SessionLocal, engine, init_db
from jx_alert.storage.models import (
    AnomalyEvent,
    Campaign,
    DailyMetric,
    DataCoverage,
    Store,
    SyncJob,
)

_MCP_SETTINGS = get_settings()


def _transport_security(settings):
    allowed_hosts = settings.mcp_allowed_host_list()
    allowed_origins = settings.mcp_allowed_origin_list()
    if not allowed_hosts:
        # Secure local-only default. Add the exact public hostname before using
        # a TLS reverse proxy or any non-local MCP client.
        allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        allowed_origins = [
            "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"
        ]
    return TransportSecuritySettings(
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    ), allowed_origins


_MCP_TRANSPORT_SECURITY, _MCP_ALLOWED_ORIGINS = _transport_security(_MCP_SETTINGS)
mcp = FastMCP(
    "JX Alert System",
    host=_MCP_SETTINGS.mcp_host,
    port=_MCP_SETTINGS.mcp_port,
    streamable_http_path=_MCP_SETTINGS.mcp_path,
    transport_security=_MCP_TRANSPORT_SECURITY,
)


class BearerTokenMiddleware:
    """Protect the remote MCP endpoint with a constant-time Bearer token check."""

    def __init__(self, app: ASGIApp, token: str, path: str = "/mcp"):
        if not token or len(token) < 32:
            raise ValueError("MCP_BEARER_TOKEN must contain at least 32 characters")
        if not path.startswith("/") or path == "/":
            raise ValueError("MCP_PATH must be an absolute non-root path")
        try:
            self.token = token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("MCP_BEARER_TOKEN must be ASCII") from exc
        self.app = app
        self.protected_paths = {path, path.rstrip("/") + "/"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if (scope["type"] == "http" and scope.get("method") != "OPTIONS"
                and scope.get("path") in self.protected_paths):
            authorization = [
                value for name, value in scope.get("headers", [])
                if name.lower() == b"authorization"
            ]
            valid = False
            if len(authorization) == 1:
                value = authorization[0].decode("latin-1")
                scheme, separator, supplied = value.partition(" ")
                valid = (
                    separator == " "
                    and scheme.lower() == "bearer"
                    and hmac.compare_digest(supplied.encode("latin-1"), self.token)
                )
            if not valid:
                body = b'{"error":"unauthorized"}'
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"www-authenticate", b"Bearer"),
                        (b"cache-control", b"no-store"),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def create_http_app(settings=None):
    """Build the authenticated Streamable HTTP ASGI app for remote MCP clients."""
    settings = settings or get_settings()
    if not settings.mcp_bearer_token:
        raise RuntimeError(
            "MCP_BEARER_TOKEN is required before starting the remote MCP service"
        )
    if settings.mcp_path != _MCP_SETTINGS.mcp_path:
        raise RuntimeError(
            "MCP_PATH is loaded when the MCP server module is imported; restart "
            "the process after changing MCP_PATH."
        )

    app = mcp.streamable_http_app()
    if _MCP_ALLOWED_ORIGINS:
        origin_pattern = "|".join(
            re.escape(origin).replace(r"\*", ".*").replace(r"\?", ".")
            for origin in _MCP_ALLOWED_ORIGINS
        )
        app = CORSMiddleware(
            app,
            allow_origins=[],
            allow_origin_regex=f"^(?:{origin_pattern})$",
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=[
                "Authorization", "Content-Type", "Mcp-Protocol-Version",
                "Mcp-Session-Id", "Last-Event-ID",
            ],
            expose_headers=["Mcp-Session-Id", "Mcp-Protocol-Version"],
        )
    return BearerTokenMiddleware(app, settings.mcp_bearer_token, settings.mcp_path)

def _limit(value: int, maximum: int = 500) -> int:
    return min(max(int(value), 1), maximum)


@mcp.tool()
def system_health() -> dict:
    """Return application/database configuration status without exposing secrets."""
    settings = get_settings()
    try:
        init_db()
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        database_status = "ok"
    except Exception:
        database_status = "unavailable"

    if settings.lingxing_access_token:
        token_mode = "static_token"
    elif settings.lingxing_token_path and settings.lingxing_app_id and settings.lingxing_app_secret:
        token_mode = "managed_token"
    else:
        token_mode = "not_configured"

    return {
        "service": "JX Alert System",
        "database": database_status,
        "dry_run": settings.app_dry_run,
        "scheduler_enabled": settings.scheduler_enabled,
        "lingxing_credentials_configured": bool(
            settings.lingxing_app_id and settings.lingxing_app_secret
        ),
        "lingxing_token_mode": token_mode,
        "feishu_enabled": settings.feishu_enabled,
        "feishu_webhook_configured": bool(settings.feishu_webhook_url),
        "remote_mcp_bearer_configured": bool(settings.mcp_bearer_token),
        "remote_mcp_path": settings.mcp_path,
        "remote_mcp_port": settings.mcp_port,
    }


@mcp.tool()
def get_stores() -> list[dict]:
    """List synchronized Lingxing stores and their configured marketplace timezone."""
    init_db()
    with SessionLocal() as db:
        rows = db.scalars(select(Store).order_by(Store.name)).all()
        return [{
            "sid": row.sid,
            "name": row.name,
            "country": row.country,
            "region": row.region,
            "marketplace_id": row.marketplace_id,
            "status_code": row.status,
            "has_ads_setting": bool(row.has_ads_setting),
            "timezone": row.timezone,
            "currency_code": row.currency_code,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        } for row in rows]


@mcp.tool()
def get_all_anomalies(limit: int = 100, status: str | None = None,
                      severity: str | None = None,
                      store_id: str | None = None) -> list[dict]:
    """Query persisted anomaly events; supports status, severity and store filters."""
    init_db()
    with SessionLocal() as db:
        query = select(AnomalyEvent)
        if status:
            query = query.where(AnomalyEvent.status == status.upper())
        if severity:
            query = query.where(AnomalyEvent.severity == severity.upper())
        if store_id:
            query = query.where(AnomalyEvent.store_id == str(store_id))
        rows = db.scalars(
            query.order_by(AnomalyEvent.last_seen.desc()).limit(_limit(limit))
        ).all()
        return [{
            "id": row.id,
            "store_id": row.store_id,
            "entity_id": row.entity_id,
            "rule": row.rule,
            "severity": row.severity,
            "status": row.status,
            "evaluation_scope": row.evaluation_scope,
            "first_seen": row.first_seen.isoformat() if row.first_seen else None,
            "last_seen": row.last_seen.isoformat() if row.last_seen else None,
            "recovered_at": row.recovered_at.isoformat() if row.recovered_at else None,
            "reason": row.reason,
            "metrics": row.metrics,
        } for row in rows]


@mcp.tool()
def get_daily_metrics(store_id: str, report_date: str,
                      dimension: str | None = None,
                      entity_id: str | None = None,
                      limit: int = 500) -> list[dict]:
    """Query stored daily metrics for a store/date; does not trigger collection."""
    try:
        target_date = date.fromisoformat(report_date)
    except ValueError as exc:
        raise ValueError("report_date must be YYYY-MM-DD") from exc
    init_db()
    with SessionLocal() as db:
        query = select(DailyMetric).where(
            DailyMetric.store_id == str(store_id),
            DailyMetric.metric_date == target_date,
        )
        if dimension:
            query = query.where(DailyMetric.dimension == dimension)
        if entity_id:
            query = query.where(DailyMetric.entity_id == str(entity_id))
        rows = db.scalars(
            query.order_by(DailyMetric.dimension, DailyMetric.entity_id,
                           DailyMetric.metric_name).limit(_limit(limit, 5000))
        ).all()
        return [{
            "store_id": row.store_id,
            "date": row.metric_date.isoformat(),
            "dimension": row.dimension,
            "entity_id": row.entity_id,
            "metric_name": row.metric_name,
            "value": row.value,
            "currency": row.currency,
            "source": row.source,
        } for row in rows]


@mcp.tool()
def get_campaigns(store_id: str | None = None,
                  campaign_type: str | None = None,
                  limit: int = 300) -> list[dict]:
    """Query synchronized SP/SB/SD campaign names, states and budgets."""
    init_db()
    with SessionLocal() as db:
        query = select(Campaign)
        if store_id:
            query = query.where(Campaign.store_id == str(store_id))
        if campaign_type:
            normalized = campaign_type.lower()
            if normalized not in {"sp", "sb", "sd"}:
                raise ValueError("campaign_type must be sp, sb or sd")
            query = query.where(Campaign.campaign_type == normalized)
        rows = db.scalars(
            query.order_by(Campaign.store_id, Campaign.campaign_type, Campaign.name)
            .limit(_limit(limit))
        ).all()
        return [{
            "store_id": row.store_id,
            "campaign_type": row.campaign_type,
            "campaign_id": row.campaign_id,
            "name": row.name,
            "state": row.state,
            "serving_status": row.serving_status,
            "budget": row.budget,
            "budget_type": row.budget_type,
            "last_over_budget_at": row.last_over_budget_at,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        } for row in rows]


@mcp.tool()
def get_data_coverage(store_id: str, start_date: str, end_date: str,
                      limit: int = 500) -> list[dict]:
    """Inspect which store/date/dimension partitions were completely collected."""
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("start_date and end_date must use YYYY-MM-DD") from exc
    if end < start:
        raise ValueError("end_date must not be earlier than start_date")
    init_db()
    with SessionLocal() as db:
        rows = db.scalars(select(DataCoverage).where(
            DataCoverage.store_id == str(store_id),
            DataCoverage.metric_date >= start,
            DataCoverage.metric_date <= end,
        ).order_by(DataCoverage.metric_date, DataCoverage.dimension).limit(_limit(limit, 5000))).all()
        return [{
            "store_id": row.store_id,
            "date": row.metric_date.isoformat(),
            "dimension": row.dimension,
            "status": row.status,
            "row_count": row.row_count,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        } for row in rows]


@mcp.tool()
def get_sync_jobs(limit: int = 50) -> list[dict]:
    """Inspect recent daily collection jobs, including partial failures."""
    init_db()
    with SessionLocal() as db:
        rows = db.scalars(
            select(SyncJob).order_by(SyncJob.started_at.desc()).limit(_limit(limit, 200))
        ).all()
        return [{
            "id": row.id,
            "job_type": row.job_type,
            "status": row.status,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "store_count": row.store_count,
            "record_count": row.record_count,
            "error_message": row.error_message,
            "metadata": row.metadata_json,
        } for row in rows]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JX Alert System MCP server")
    parser.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="stdio",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run()
    else:
        import uvicorn
        from jx_alert.config import get_settings

        settings = get_settings()
        http_app = create_http_app(settings)
        uvicorn.run(
            http_app,
            host=settings.mcp_host,
            port=settings.mcp_port,
            log_level="info",
            proxy_headers=True,
        )
