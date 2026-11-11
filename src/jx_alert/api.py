from fastapi import FastAPI
from jx_alert.storage.db import init_db
app = FastAPI(title="JX Alert System", version="0.1.0")

@app.on_event("startup")
def startup():
    init_db()

@app.get("/health")
def health():
    return {"status": "ok", "service": "jx-alert-system", "mode": "dry-run"}
