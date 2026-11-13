from functools import lru_cache
import json
from pathlib import Path
from typing import Literal
from sqlalchemy.engine import URL
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "production"
    app_dry_run: bool = True
    app_timezone: str = "Asia/Shanghai"
    scheduler_enabled: bool = False
    daily_sync_cron: str = "0 7 * * *"
    feishu_daily_report_cron: str = "0 8 * * *"
    feishu_daily_report_start_date: str = ""
    feishu_daily_report_group_digest: bool = False
    # Refresh enough marketplace-local dates for the 14-day attribution window plus its 7-day baseline.
    ad_attribution_refresh_days: int = Field(default=22, ge=1, le=31)
    store_timezones_file: str = "/app/data/store-timezones.json"
    runtime_settings_file: str = "/app/data/runtime-settings.json"
    collection_scope_file: str = "/app/data/collection-scope.json"
    # Opt-in historical collection without inventing a store's report timezone.
    # Uses a globally completed calendar day; intraday interpretation stays blocked.
    allow_unconfigured_timezone_history: bool = False
    sp_collection_enabled: bool = False
    sp_validation_targets_file: str = "/app/data/sp-validation-targets.json"
    sp_refresh_days: int = Field(default=7, ge=1, le=30)
    mcp_operations_enabled: bool = False
    mcp_notifications_enabled: bool = False
    business_alert_delivery_mode: Literal["rules", "chat"] = "rules"
    mcp_auth_file: str = "/app/data/secrets/mcp-auth.json"
    mcp_auth_mode: Literal["bearer", "oauth"] = "bearer"
    mcp_public_url: str = ""
    mcp_oauth_database_path: str = "/app/data/secrets/mcp-oauth.sqlite3"
    mcp_owner_credentials_file: str = "/app/data/secrets/mcp-owner.json"
    mcp_forwarded_allow_ips: str = "127.0.0.1"

    # Remote MCP uses Streamable HTTP and is published on a loopback-only host port.
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8001
    mcp_path: str = "/mcp"
    mcp_bearer_token: str = ""
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""

    def mcp_allowed_host_list(self) -> list[str]:
        return [item.strip() for item in self.mcp_allowed_hosts.split(",") if item.strip()]

    def mcp_allowed_origin_list(self) -> list[str]:
        return [item.strip() for item in self.mcp_allowed_origins.split(",") if item.strip()]

    # Prefer DATABASE_URL when explicitly supplied; otherwise compose a URL safely
    # from DB_* fields so passwords containing URL-reserved characters are escaped.
    database_url: str | None = None
    db_host: str = "db"
    db_port: int = 3306
    db_name: str = "jxalert"
    db_user: str = "jxalert"
    db_password: str = ""

    lingxing_base_url: str = "https://openapi.lingxing.com"
    # Optional official Lingxing MCP for performance/notice tools not exposed
    # through the public OpenAPI contract used by the daily collector.
    lingxing_mcp_url: str = ""
    lingxing_mcp_key: SecretStr = SecretStr("")
    lingxing_app_id: str = ""
    lingxing_app_key: str = ""
    lingxing_app_secret: str = ""
    lingxing_access_token: str = ""
    lingxing_refresh_token: str = ""
    lingxing_token_path: str = ""
    lingxing_refresh_path: str = ""
    lingxing_token_request_json: str = "{}"
    lingxing_refresh_request_json: str = "{}"
    lingxing_token_request_format: str = "json"
    lingxing_refresh_request_format: str = "json"
    lingxing_token_response_path: str = ""
    lingxing_refresh_response_path: str = ""
    lingxing_expires_in_response_path: str = ""
    lingxing_token_store_path: str = "./data/secrets/lingxing-token.json"
    lingxing_token_method: str = "POST"
    lingxing_refresh_method: str = "POST"
    lingxing_timeout: int = 30

    # Performance source timestamps have no UTC offset; configure their real IANA timezone.
    lingxing_performance_enabled: bool = False
    lingxing_performance_observation_only: bool = True
    lingxing_performance_source_timezone: str = ""
    lingxing_performance_max_age_hours: int = Field(default=8, ge=1, le=168)
    lingxing_performance_page_size: int = Field(default=200, ge=1, le=200)
    lingxing_performance_max_pages: int = Field(default=1000, ge=1, le=10000)

    # A Feishu custom bot is a one-way webhook bound to one group.
    feishu_enabled: bool = False
    feishu_webhook_url: str = ""
    feishu_sign_secret: str = ""
    feishu_keyword: str = ""
    feishu_default_at_open_ids: str = ""
    feishu_routing_file: str = "/app/data/feishu-routing.json"
    feishu_timeout: int = 20
    feishu_transport: Literal["webhook", "app"] = "webhook"
    # Explicit policy: missing role mappings use the verified report group without mentions.
    feishu_group_fallback_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = SecretStr("")
    feishu_app_credentials_file: str = "/app/data/secrets/feishu-app.json"
    feishu_manager_open_ids: str = ""
    feishu_daily_report_enabled: bool = False
    feishu_daily_report_chat_id: str = ""

    alert_order_change_ratio: float = 0.15
    alert_high_spend_ratio: float = 1.30
    alert_low_spend_ratio: float = 0.50
    alert_budget_ratio: float = 0.90
    alert_min_spend: float = 20.0
    alert_min_clicks: int = 10
    alert_min_orders_baseline: float = 1.0
    alert_escalate_after_days: int = Field(default=2, ge=1, le=30)
    report_dir: str = "./data/reports"
    raw_dir: str = "./data/raw"

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        if self.db_password:
            return URL.create(
                "mysql+pymysql",
                username=self.db_user,
                password=self.db_password,
                host=self.db_host,
                port=self.db_port,
                database=self.db_name,
                query={"charset": "utf8mb4"},
            ).render_as_string(hide_password=False)
        return "sqlite:///./jxalert.db"

    def feishu_at_ids(self) -> list[str]:
        ids = [item.strip() for item in self.feishu_default_at_open_ids.split(",") if item.strip()]
        if any(not item.startswith("ou_") for item in ids):
            raise ValueError("FEISHU_DEFAULT_AT_OPEN_IDS must contain Feishu Open IDs (ou_...)")
        return ids


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    path = Path(settings.runtime_settings_file)
    if path.is_file():
        runtime = json.loads(path.read_text(encoding="utf-8"))
        allowed = {
            "app_dry_run", "scheduler_enabled", "daily_sync_cron",
            "feishu_daily_report_cron", "feishu_daily_report_start_date",
            "feishu_daily_report_group_digest",
            "business_alert_delivery_mode",
            "allow_unconfigured_timezone_history",
            "collection_scope_file", "sp_collection_enabled", "sp_validation_targets_file",
            "store_timezones_file", "feishu_routing_file",
            "sp_refresh_days", "mcp_operations_enabled", "mcp_notifications_enabled", "feishu_enabled", "feishu_transport",
            "feishu_group_fallback_enabled",
            "feishu_manager_open_ids", "feishu_daily_report_enabled", "feishu_daily_report_chat_id",
            "lingxing_performance_enabled", "lingxing_performance_source_timezone",
            "lingxing_performance_observation_only",
            "mcp_allowed_hosts", "mcp_allowed_origins",
            "mcp_auth_mode", "mcp_public_url", "mcp_oauth_database_path", "mcp_owner_credentials_file",
            "mcp_forwarded_allow_ips",
        }
        if not isinstance(runtime, dict) or set(runtime) - allowed:
            raise ValueError("Runtime settings contain unsupported fields")
        settings = Settings(_env_file=None, **{**settings.model_dump(), **runtime})
    if not settings.mcp_bearer_token and Path(settings.mcp_auth_file).is_file():
        auth = json.loads(Path(settings.mcp_auth_file).read_text(encoding="utf-8"))
        token = auth.get("bearer_token") if isinstance(auth, dict) else None
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("MCP authentication file is invalid")
        settings = settings.model_copy(update={"mcp_bearer_token": token})
    return settings
