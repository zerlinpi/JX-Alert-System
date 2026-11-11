"""MCP tools for querying persisted JX Alert data."""
from mcp.server.fastmcp import FastMCP
from jx_alert.config import get_settings
from jx_alert.storage.db import SessionLocal
from jx_alert.storage.models import AnomalyEvent

mcp = FastMCP("JX Alert System")

@mcp.tool()
def system_health() -> dict:
    s = get_settings()
    return {"service": "JX Alert System", "dry_run": s.app_dry_run,
            "lingxing_configured": bool(s.lingxing_app_id and s.lingxing_access_token),
            "feishu_enabled": s.feishu_enabled}

@mcp.tool()
def get_all_anomalies(limit: int = 100, status: str | None = None) -> list[dict]:
    """Query stored anomaly events; does not contact seller accounts."""
    with SessionLocal() as db:
        q = db.query(AnomalyEvent)
        if status:
            q = q.filter(AnomalyEvent.status == status)
        return [{"id": x.id, "store_id": x.store_id, "rule": x.rule,
                 "severity": x.severity, "status": x.status,
                 "reason": x.reason, "metrics": x.metrics}
                for x in q.order_by(AnomalyEvent.last_seen.desc())
                         .limit(min(max(limit, 1), 500)).all()]

if __name__ == "__main__":
    mcp.run()
