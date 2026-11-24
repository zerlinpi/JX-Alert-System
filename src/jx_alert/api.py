from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from jx_alert.config import get_settings
from jx_alert.storage.db import engine, init_db


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="JX Alert System", version="0.4.0", lifespan=lifespan)


@app.get("/health")
def health():
    from jx_alert.ai.deepseek_config import DeepSeekConfigError, client_settings
    from jx_alert.sparkx.credentials import effective_token

    settings = get_settings()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    try:
        deepseek = bool(client_settings(settings)[0])
    except DeepSeekConfigError:
        deepseek = False
    from jx_alert.sparkx.skills import installed

    index = installed(settings)
    return {"status": "ok", "service": "jx-alert-system", "data_source": "sparkx", "analysis": "deepseek_agent",
            "dry_run": settings.app_dry_run, "scheduler_enabled": settings.scheduler_enabled,
            "feishu_delivery_enabled": settings.delivery_enabled(),
            "sparkx_configured": bool(effective_token(settings)), "deepseek_configured": deepseek,
            "sparkx_skills": (index or {}).get("release") if index else None}
