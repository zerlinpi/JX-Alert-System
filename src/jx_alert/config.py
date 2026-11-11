from functools import lru_cache
from sqlalchemy.engine import URL
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "production"
    app_dry_run: bool = True
    app_timezone: str = "Asia/Shanghai"
    scheduler_enabled: bool = False
    daily_sync_cron: str = "0 8 * * *"
    # Refresh report_date D and the previous seven marketplace-local days daily.
    ad_attribution_refresh_days: int = Field(default=8, ge=1, le=31)
    store_timezones_file: str = "/app/data/store-timezones.json"

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
    lingxing_app_id: str = ""
    lingxing_app_key: str = ""
    lingxing_app_secret: str = ""
    lingxing_access_token: str = ""
    lingxing_refresh_token: str = ""
    lingxing_token_path: str = ""
    lingxing_refresh_path: str = ""
    lingxing_token_request_json: str = "{}"
    lingxing_refresh_request_json: str = "{}"
    lingxing_token_response_path: str = ""
    lingxing_refresh_response_path: str = ""
    lingxing_expires_in_response_path: str = ""
    lingxing_token_store_path: str = "./data/secrets/lingxing-token.json"
    lingxing_token_method: str = "POST"
    lingxing_refresh_method: str = "POST"
    lingxing_timeout: int = 30

    # A Feishu custom bot is a one-way webhook bound to one group.
    feishu_enabled: bool = False
    feishu_webhook_url: str = ""
    feishu_sign_secret: str = ""
    feishu_keyword: str = ""
    feishu_default_at_open_ids: str = ""
    feishu_routing_file: str = "/app/data/feishu-routing.json"
    feishu_timeout: int = 20

    alert_order_change_ratio: float = 0.15
    alert_high_spend_ratio: float = 1.30
    alert_low_spend_ratio: float = 0.50
    alert_budget_ratio: float = 0.90
    alert_min_spend: float = 20.0
    alert_min_clicks: int = 10
    alert_min_orders_baseline: float = 1.0
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
    return Settings()
