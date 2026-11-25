import json
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from jx_alert.config import Settings
from jx_alert.notifications import outbox  # noqa: F401 - register the outbox table
from jx_alert.storage import models  # noqa: F401 - register all ORM tables
from jx_alert.storage.base import Base

APP_ID = "cli_synthetic_app"
GROUP_CHAT = "oc_reports"
LEAD = "ou_lead"
AD_LEAD = "ou_adlead"
GENERATION = "a" * 32


def write_private(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)


@pytest.fixture(autouse=True)
def fresh_tool_catalog():
    """The SparkX tool catalog is cached per token for 30 minutes; every test starts without it."""
    from jx_alert.ai import agent

    agent._catalog.clear()
    yield
    agent._catalog.clear()


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    factory.engine = engine
    return factory


@pytest.fixture
def routing():
    return {"groups": {"reports": {"chat_id": GROUP_CHAT, "display_name": "日报群", "admin_open_ids": [],
                                   "verified_member_open_ids": [LEAD, AD_LEAD, "ou_op1", "ou_op2"]}},
            "company_executive_digest": {"open_id": LEAD, "name": "老板", "app_id": APP_ID, "generation": GENERATION},
            "ad_alert_subscribers": {"recipients": [{"open_id": AD_LEAD, "name": "广告主管", "generation": "b" * 32}]}}


@pytest.fixture
def settings(tmp_path, routing):
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    write_private(tmp_path / "feishu-routing.json", routing)
    write_private(secrets / "feishu-app.json", {"app_id": APP_ID, "app_secret": "synthetic-secret"})
    return Settings(_env_file=None, app_dry_run=False, feishu_enabled=True, feishu_transport="app",
                    feishu_daily_report_chat_id="reports", feishu_routing_file=str(tmp_path / "feishu-routing.json"),
                    feishu_app_credentials_file=str(secrets / "feishu-app.json"),
                    sparkx_credentials_file=str(secrets / "sparkx.json"),
                    sparkx_stores_file=str(tmp_path / "sparkx-stores.json"),
                    notification_settings_file=str(tmp_path / "jx-notifications.json"),
                    deepseek_credentials_file=str(secrets / "deepseek.json"),
                    runtime_settings_file=str(tmp_path / "runtime-settings.json"),
                    sparkx_skills_dir=str(tmp_path / "sparkx-skills"),
                    report_archive_dir=str(tmp_path / "reports"),
                    sparkx_ai_token="synthetic-sparkx-token", deepseek_api_key="sk-synthetic-deepseek")
