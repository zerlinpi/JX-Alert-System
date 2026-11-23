"""Process settings: environment (.env) plus the owner's runtime JSON overrides.

SparkX MCP is the only business data source. DeepSeek writes narrative text and
answers questions; Feishu (enterprise app) delivers. Retired Lingxing/MCP-server
keys in an existing runtime file are tolerated and ignored, never applied.
"""
from functools import lru_cache
import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

_LOG = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    app_env: str = "production"
    app_dry_run: bool = True
    app_timezone: str = "Asia/Shanghai"
    scheduler_enabled: bool = False
    runtime_settings_file: str = "/app/data/runtime-settings.json"

    # SparkX MCP (read-only). The owner console token file takes precedence over the env.
    sparkx_ai_token: SecretStr = SecretStr("")
    sparkx_credentials_file: str = "/app/data/secrets/sparkx.json"
    # Owner choices per SparkX profile: monitoring switch and the store's operators (0600).
    sparkx_stores_file: str = "/app/data/sparkx-stores.json"
    sparkx_timeout: float = Field(default=30.0, ge=5, le=120)
    # Owner switches, recipients and thresholds of the notification lanes (0600).
    notification_settings_file: str = "/app/data/jx-notifications.json"

    # DeepSeek ("dp"): report narrative and the Feishu question bot.
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_credentials_file: str = "/app/data/secrets/deepseek.json"
    deepseek_model: str = "deepseek-flash"
    deepseek_request_timeout: float = Field(default=45.0, ge=1, le=120)

    # Feishu enterprise application (the only delivery transport).
    feishu_enabled: bool = False
    feishu_transport: Literal["webhook", "app"] = "app"
    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = SecretStr("")
    feishu_app_credentials_file: str = "/app/data/secrets/feishu-app.json"
    feishu_routing_file: str = "/app/data/feishu-routing.json"
    # Alias of the report group inside the routing file's "groups" section.
    feishu_daily_report_chat_id: str = ""
    feishu_timeout: int = 20
    feishu_bot_enabled: bool = False
    feishu_bot_open_id: str = ""
    feishu_bot_tenant_key: str = ""
    feishu_bot_max_pending: int = Field(default=50, ge=1, le=200)
    feishu_bot_question_max_age_minutes: int = Field(default=30, ge=1, le=120)

    # Owner console (single owner password, separate session store).
    console_public_url: str = Field(default="", validation_alias=AliasChoices(
        "console_public_url", "mcp_public_url"))
    console_owner_credentials_file: str = Field(
        default="/app/data/secrets/mcp-owner.json",
        validation_alias=AliasChoices("console_owner_credentials_file", "mcp_owner_credentials_file"))
    console_sessions_file: str = "/app/data/secrets/console-sessions.sqlite3"
    console_forwarded_allow_ips: str = Field(default="127.0.0.1", validation_alias=AliasChoices(
        "console_forwarded_allow_ips", "mcp_forwarded_allow_ips"))

    # Prefer DATABASE_URL when explicitly supplied; otherwise compose a URL safely
    # from DB_* fields so passwords containing URL-reserved characters are escaped.
    database_url: str | None = None
    db_host: str = "db"
    db_port: int = 3306
    db_name: str = "jxalert"
    db_user: str = "jxalert"
    db_password: str = ""

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        if self.db_password:
            return URL.create(
                "mysql+pymysql", username=self.db_user, password=self.db_password,
                host=self.db_host, port=self.db_port, database=self.db_name,
                query={"charset": "utf8mb4"},
            ).render_as_string(hide_password=False)
        return "sqlite:///./jxalert.db"

    def delivery_enabled(self) -> bool:
        """Outbound Feishu messages need an explicit, non-dry-run app transport."""
        return bool(self.feishu_enabled and not self.app_dry_run and self.feishu_transport == "app")


RUNTIME_KEYS = frozenset({
    "app_dry_run", "scheduler_enabled", "feishu_enabled", "feishu_transport", "feishu_routing_file",
    "feishu_daily_report_chat_id", "feishu_bot_enabled", "feishu_bot_open_id", "feishu_bot_max_pending",
    "feishu_bot_question_max_age_minutes", "deepseek_credentials_file", "deepseek_model",
    "deepseek_request_timeout", "sparkx_credentials_file", "sparkx_stores_file", "sparkx_timeout",
    "notification_settings_file", "console_public_url", "console_owner_credentials_file",
    "console_sessions_file", "console_forwarded_allow_ips",
})
# Keys written by the retired Lingxing/MCP-server releases. They stay in the file
# (owner data is never rewritten) but no longer change behaviour.
_RENAMED = {"mcp_public_url": "console_public_url",
            "mcp_owner_credentials_file": "console_owner_credentials_file",
            "mcp_forwarded_allow_ips": "console_forwarded_allow_ips"}
LEGACY_RUNTIME_KEYS = frozenset({
    "daily_sync_cron", "hourly_ad_monitor_enabled", "hourly_ad_monitor_cron", "hourly_mode_daily_sync_cron",
    "hourly_ad_store_routing_enabled", "hourly_ad_store_routing_app_id", "feishu_daily_report_cron",
    "feishu_daily_report_start_date", "feishu_company_executive_digest_enabled",
    "feishu_company_executive_digest_start_date", "feishu_daily_report_group_digest",
    "sb_sd_detail_reports_enabled", "business_alert_delivery_mode", "excel_archive_enabled",
    "excel_archive_dir", "excel_daily_snapshot_enabled", "excel_daily_snapshot_cron",
    "alert_kpi_change_ratio", "alert_kpi_min_impressions", "alert_materiality_enabled",
    "allow_unconfigured_timezone_history", "collection_scope_file", "store_controls_file",
    "advertising_source_controls_file", "sp_collection_enabled", "sp_validation_targets_file",
    "store_timezones_file", "sp_refresh_days", "mcp_operations_enabled", "mcp_notifications_enabled",
    "feishu_group_fallback_enabled", "feishu_manager_open_ids", "feishu_daily_report_enabled",
    "feishu_morning_focus_enabled", "feishu_quiet_delivery_enabled", "lingxing_performance_enabled",
    "lingxing_performance_source_timezone", "lingxing_performance_observation_only",
    "lingxing_business_credentials_file", "lingxing_sources_file", "mcp_allowed_hosts",
    "mcp_allowed_origins", "mcp_auth_mode", "mcp_oauth_database_path", *_RENAMED,
})


def _unique(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("duplicate JSON field")
        values[key] = value
    return values


def load_runtime_settings() -> Settings:
    """Read the effective process configuration, including the current runtime JSON."""
    settings = Settings()
    path = Path(settings.runtime_settings_file)
    if not path.is_file():
        return settings
    runtime = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique)
    if not isinstance(runtime, dict) or set(runtime) - RUNTIME_KEYS - LEGACY_RUNTIME_KEYS:
        raise ValueError("Runtime settings contain unsupported fields")
    applied = {key: value for key, value in runtime.items() if key in RUNTIME_KEYS}
    for old, new in _RENAMED.items():
        if old in runtime and new not in runtime:
            applied[new] = runtime[old]
    ignored = sorted(set(runtime) & LEGACY_RUNTIME_KEYS - set(_RENAMED))
    if ignored:
        _LOG.info("Ignoring %d retired runtime setting(s)", len(ignored))
    return Settings(_env_file=None, **{**settings.model_dump(), **applied})


@lru_cache
def get_settings() -> Settings:
    return load_runtime_settings()
