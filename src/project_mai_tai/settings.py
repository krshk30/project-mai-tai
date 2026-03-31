from __future__ import annotations

from functools import lru_cache

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MAI_TAI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "project-mai-tai"
    environment: str = "development"
    log_level: str = "INFO"

    control_plane_host: str = "127.0.0.1"
    control_plane_port: int = 8100

    database_url: str = (
        "postgresql+psycopg://mai_tai:change-me@localhost:5432/project_mai_tai"
    )
    redis_url: str = "redis://localhost:6379/0"
    redis_stream_prefix: str = "mai_tai"
    redis_snapshot_batch_stream_maxlen: int = 4
    redis_market_data_stream_maxlen: int = 10_000
    redis_market_data_subscription_stream_maxlen: int = 250
    redis_strategy_intent_stream_maxlen: int = 2_000
    redis_order_event_stream_maxlen: int = 2_000
    redis_strategy_state_stream_maxlen: int = 250
    redis_heartbeat_stream_maxlen: int = 1_000

    legacy_api_base_url: str | None = None
    legacy_api_timeout_seconds: int = 3
    legacy_api_cache_ttl_seconds: int = 5

    massive_api_key: str | None = None
    market_data_snapshot_interval_seconds: int = 5
    market_data_reference_cache_path: str = "data/cache/reference_data.json"
    market_data_reference_cache_max_age_hours: int = 24
    market_data_reference_lookback_days: int = 20
    market_data_scan_min_price: float = 1.0
    market_data_scan_max_price: float = 10.0
    market_data_static_symbols: str = ""
    market_data_warmup_enabled: bool = True
    market_data_warmup_lookback_days: int = 14
    market_data_warmup_bar_limit: int = 50_000
    news_enabled: bool = True
    news_session_start_hour_et: int = 16
    news_cache_ttl_minutes: int = 15
    news_request_timeout_seconds: int = 5
    news_max_articles_per_symbol: int = 20
    news_batch_size: int = 5
    news_path_a_min_confidence: float = 0.85

    broker_default_provider: str = "alpaca"
    oms_adapter: str = "simulated"
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_request_timeout_seconds: int = 10
    alpaca_order_fill_timeout_seconds: int = 10
    alpaca_order_poll_interval_seconds: float = 0.5
    alpaca_cancel_unfilled_after_timeout: bool = True
    alpaca_paper_order_fill_timeout_seconds: int = 10
    alpaca_paper_cancel_unfilled_after_timeout: bool = True
    alpaca_cancel_confirm_timeout_seconds: float = 5.0
    strategy_macd_30s_account_name: str = "paper:macd_30s"
    strategy_macd_1m_account_name: str = "paper:macd_1m"
    strategy_tos_account_name: str = "paper:tos_runner_shared"
    strategy_runner_account_name: str = "paper:tos_runner_shared"
    alpaca_macd_30s_api_key: str | None = None
    alpaca_macd_30s_secret_key: str | None = None
    alpaca_macd_1m_api_key: str | None = None
    alpaca_macd_1m_secret_key: str | None = None
    alpaca_tos_runner_api_key: str | None = None
    alpaca_tos_runner_secret_key: str | None = None
    schwab_base_url: str = "https://api.schwabapi.com"
    schwab_token_url: str = "https://api.schwabapi.com/v1/oauth/token"
    schwab_request_timeout_seconds: int = 10
    schwab_order_fill_timeout_seconds: int = 10
    schwab_order_poll_interval_seconds: float = 0.5
    schwab_token_refresh_margin_seconds: int = 60
    schwab_access_token: str | None = None
    schwab_access_token_expires_at: str | None = None
    schwab_refresh_token: str | None = None
    schwab_client_id: str | None = None
    schwab_client_secret: str | None = None
    schwab_token_store_path: str | None = None
    schwab_account_hash: str | None = None
    schwab_macd_30s_account_hash: str | None = None
    schwab_macd_1m_account_hash: str | None = None
    schwab_tos_runner_account_hash: str | None = None
    oms_broker_sync_interval_seconds: int = 15

    dashboard_refresh_seconds: int = 5
    dashboard_snapshot_persistence_enabled: bool = True
    service_heartbeat_interval_seconds: int = 15
    reconciliation_interval_seconds: int = 30
    reconciliation_stuck_order_seconds: int = 180
    reconciliation_stuck_intent_seconds: int = 180
    reconciliation_position_quantity_tolerance: float = 0.0001
    reconciliation_average_price_tolerance: float = 0.02

    @computed_field
    @property
    def control_plane_base_url(self) -> str:
        return f"http://{self.control_plane_host}:{self.control_plane_port}"

    @computed_field
    @property
    def market_data_static_symbol_list(self) -> list[str]:
        if not self.market_data_static_symbols.strip():
            return []
        return sorted(
            {
                symbol.strip().upper()
                for symbol in self.market_data_static_symbols.split(",")
                if symbol.strip()
            }
        )

    @computed_field
    @property
    def resolved_broker_provider(self) -> str:
        if self.oms_adapter == "alpaca_paper":
            return "alpaca"
        if self.oms_adapter == "schwab":
            return "schwab"
        return self.broker_default_provider

    @computed_field
    @property
    def resolved_execution_mode(self) -> str:
        if self.oms_adapter == "alpaca_paper":
            return "paper"
        if self.oms_adapter == "schwab":
            return "live"
        return "shadow"


@lru_cache
def get_settings() -> Settings:
    return Settings()
