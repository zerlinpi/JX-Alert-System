from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    app_dry_run: bool = True
    app_timezone: str = "Asia/Shanghai"
    database_url: str = "sqlite:///./jxalert.db"
    lingxing_base_url: str = "https://openapi.lingxing.com"
    lingxing_app_id: str = ""
    lingxing_app_key: str = ""
    lingxing_app_secret: str = ""
    lingxing_access_token: str = ""
    lingxing_token_path: str = ""
    lingxing_refresh_path: str = ""
    lingxing_token_request_json: str = "{}"
    lingxing_refresh_request_json: str = "{}"
    lingxing_timeout: int = 30
    feishu_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_base_url: str = "https://open.feishu.cn"
    feishu_default_chat_id: str = ""
    alert_order_change_ratio: float = 0.15
    alert_high_spend_ratio: float = 1.30
    alert_low_spend_ratio: float = 0.50
    alert_budget_ratio: float = 0.90
    alert_min_spend: float = 20.0
    alert_min_clicks: int = 10
    alert_min_orders_baseline: float = 1.0
    report_dir: str = "./data/reports"
    raw_dir: str = "./data/raw"

@lru_cache
def get_settings() -> Settings:
    return Settings()
