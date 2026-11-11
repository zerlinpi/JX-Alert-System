from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from jx_alert.config import get_settings
from jx_alert.storage.db import engine, init_db

app = FastAPI(title="JX Alert System", version="0.2.0")


@app.on_event("startup")
def startup():
    init_db()


@app.get("/health")
def health():
    settings = get_settings()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {
        "status": "ok",
        "service": "jx-alert-system",
        "dry_run": settings.app_dry_run,
        "scheduler_enabled": settings.scheduler_enabled,
        "feishu_enabled": settings.feishu_enabled,
        "lingxing_credentials_configured": bool(
            settings.lingxing_app_id and settings.lingxing_app_secret
        ),
    }
