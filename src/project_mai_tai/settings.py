from __future__ import annotations

from functools import lru_cache
import json

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
    tradingview_alerts_host: str = "127.0.0.1"
    tradingview_alerts_port: int = 8110
    tradingview_alerts_enabled: bool = False
    tradingview_alerts_auto_sync_enabled: bool = True
    tradingview_alerts_state_path: str = "data/cache/tradingview_alerts_state.json"
    tradingview_alerts_operator: str = "log_only"
    tradingview_alerts_chart_url: str = "https://www.tradingview.com/chart/"
    tradingview_alerts_user_data_dir: str = "data/cache/tradingview_user_data"
    tradingview_alerts_headless: bool = False
    tradingview_alerts_timeout_ms: int = 15_000
    tradingview_alerts_browser_channel: str = "chrome"
    tradingview_alerts_alert_name_prefix: str = "MAI_TAI"
    tradingview_alerts_condition_text: str = "Any alert() function call"
    tradingview_alerts_webhook_url: str | None = None
    tradingview_alerts_webhook_token: str | None = None
    tradingview_alerts_message_template_json: str = ""
    tradingview_alerts_notification_provider: str = "none"
    tradingview_alerts_notification_cooldown_minutes: int = 240
    tradingview_alerts_notification_smtp_host: str | None = None
    tradingview_alerts_notification_smtp_port: int = 587
    tradingview_alerts_notification_smtp_username: str | None = None
    tradingview_alerts_notification_smtp_password: str | None = None
    tradingview_alerts_notification_smtp_from: str = ""
    tradingview_alerts_notification_smtp_to: str = ""
    tradingview_alerts_notification_smtp_starttls: bool = True
    tradingview_alerts_notification_twilio_account_sid: str | None = None
    tradingview_alerts_notification_twilio_auth_token: str | None = None
    tradingview_alerts_notification_twilio_from_number: str = ""
    tradingview_alerts_notification_twilio_to_number: str = ""

    database_url: str = (
        "postgresql+psycopg://mai_tai:change-me@localhost:5432/project_mai_tai"
    )
    redis_url: str = "redis://localhost:6379/0"
    redis_stream_prefix: str = "mai_tai"
    redis_snapshot_batch_stream_maxlen: int = 180
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
    market_data_live_aggregate_stream_enabled: bool = False
    strategy_macd_30s_enabled: bool = True
    strategy_webull_30s_enabled: bool = False
    strategy_schwab_1m_enabled: bool = False
    strategy_macd_30s_live_aggregate_bars_enabled: bool = False
    strategy_macd_30s_live_aggregate_fallback_enabled: bool = True
    strategy_macd_30s_live_aggregate_stale_after_seconds: int = 3
    strategy_webull_30s_live_aggregate_bars_enabled: bool = False
    strategy_webull_30s_live_aggregate_fallback_enabled: bool = True
    strategy_webull_30s_live_aggregate_stale_after_seconds: int = 3
    strategy_macd_30s_massive_indicator_overlay_enabled: bool = True
    strategy_macd_30s_probe_enabled: bool = False
    strategy_macd_30s_reclaim_enabled: bool = False
    strategy_macd_30s_retest_enabled: bool = False
    strategy_macd_30s_default_quantity: int = 100
    strategy_webull_30s_default_quantity: int = 100
    strategy_schwab_1m_default_quantity: int = 100
    strategy_macd_30s_reclaim_excluded_symbols: str = "JEM,CYCN,BFRG,UCAR,BBGI"
    scanner_feed_retention_enabled: bool = True
    scanner_feed_retention_structure_bars: int = 10
    scanner_feed_retention_no_activity_minutes: int = 20
    scanner_feed_retention_cooldown_volume_ratio: float = 0.4
    scanner_feed_retention_cooldown_max_5m_range_pct: float = 1.5
    scanner_feed_retention_resume_hold_bars: int = 3
    scanner_feed_retention_resume_min_5m_range_pct: float = 2.5
    scanner_feed_retention_resume_min_5m_volume_ratio: float = 1.5
    scanner_feed_retention_resume_min_5m_volume_abs: float = 150_000.0
    scanner_feed_retention_drop_cooldown_minutes: int = 30
    scanner_feed_retention_drop_max_5m_range_pct: float = 1.0
    scanner_feed_retention_drop_max_5m_volume_abs: float = 75_000.0
    strategy_macd_1m_enabled: bool = False
    strategy_tos_enabled: bool = False
    strategy_runner_enabled: bool = False
    strategy_macd_1m_massive_indicator_overlay_enabled: bool = False
    strategy_macd_1m_taapi_indicator_source_enabled: bool = False
    strategy_macd_30s_common_config_overrides_json: str = ""
    strategy_macd_30s_config_overrides_json: str = ""
    strategy_webull_30s_config_overrides_json: str = ""
    strategy_schwab_1m_config_overrides_json: str = ""
    strategy_macd_30s_probe_config_overrides_json: str = ""
    strategy_macd_30s_reclaim_config_overrides_json: str = ""
    strategy_macd_30s_retest_config_overrides_json: str = ""
    taapi_secret: str | None = None
    news_enabled: bool = True
    news_session_start_hour_et: int = 16
    news_cache_ttl_minutes: int = 15
    news_request_timeout_seconds: int = 5
    news_max_articles_per_symbol: int = 20
    news_batch_size: int = 5
    news_path_a_min_confidence: float = 0.85
    news_ai_shadow_enabled: bool = False
    news_ai_promote_enabled: bool = False
    news_ai_provider: str = "openai"
    news_ai_api_key: str | None = None
    news_ai_model: str = "gpt-4.1-mini"
    news_ai_base_url: str = "https://api.openai.com/v1"
    news_ai_request_timeout_seconds: int = 8
    news_ai_max_articles: int = 3
    news_ai_max_summary_chars: int = 280
    trade_coach_enabled: bool = False
    trade_coach_shadow_enabled: bool = False
    trade_coach_promote_enabled: bool = False
    trade_coach_provider: str = "openai"
    trade_coach_api_key: str | None = None
    trade_coach_model: str = "gpt-4.1-mini"
    trade_coach_base_url: str = "https://api.openai.com/v1"
    trade_coach_request_timeout_seconds: int = 8
    trade_coach_context_bars: int = 20
    trade_coach_review_bars_after_exit: int = 20
    trade_coach_max_similar_trades: int = 5
    trade_coach_review_poll_seconds: int = 60
    trade_coach_review_limit: int = 25
    trade_coach_completed_trade_lookback_days: int = 0

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
    strategy_macd_30s_broker_provider: str | None = None
    strategy_webull_30s_account_name: str = "live:webull_30s"
    strategy_webull_30s_broker_provider: str | None = "webull"
    strategy_schwab_1m_account_name: str = "live:schwab_1m"
    strategy_schwab_1m_broker_provider: str | None = "schwab"
    strategy_macd_30s_probe_account_name: str = "paper:macd_30s_probe"
    strategy_macd_30s_reclaim_account_name: str = "paper:macd_30s_reclaim"
    strategy_macd_30s_retest_account_name: str = "paper:macd_30s_retest"
    strategy_macd_1m_account_name: str = "paper:macd_1m"
    strategy_tos_default_quantity: int = 100
    strategy_tos_account_name: str = "paper:tos_runner_shared"
    strategy_tos_broker_provider: str | None = None
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
    schwab_schwab_1m_account_hash: str | None = None
    schwab_macd_1m_account_hash: str | None = None
    schwab_tos_runner_account_hash: str | None = None
    schwab_tick_archive_enabled: bool = False
    schwab_tick_archive_root: str = "data/recordings/schwab_ticks"
    schwab_stream_symbol_stale_after_seconds: float = 8.0
    schwab_stream_symbol_stale_after_seconds_without_position: float = 90.0
    schwab_stream_symbol_quote_poll_interval_seconds: float = 2.0
    schwab_stream_symbol_resubscribe_interval_seconds: float = 5.0
    schwab_prewarm_symbol_ttl_seconds: float = 900.0
    webull_base_url: str = "https://api.webull.com"
    webull_region_id: str = "us"
    webull_request_timeout_seconds: int = 10
    webull_app_key: str | None = None
    webull_app_secret: str | None = None
    webull_account_id: str | None = None
    oms_broker_sync_interval_seconds: int = 5
    oms_working_order_refresh_seconds: int = 5

    dashboard_refresh_seconds: int = 5
    dashboard_snapshot_persistence_enabled: bool = True
    dashboard_scanner_history_retention: int = 5_000
    strategy_history_persistence_enabled: bool = True
    service_heartbeat_interval_seconds: int = 15
    reconciliation_interval_seconds: int = 30
    reconciliation_stuck_order_seconds: int = 180
    reconciliation_stuck_intent_seconds: int = 180
    reconciliation_position_quantity_tolerance: float = 0.0001
    reconciliation_average_price_tolerance: float = 0.02
    reconciliation_ignored_position_mismatches: str = ""

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
    def strategy_macd_30s_reclaim_excluded_symbol_list(self) -> list[str]:
        if not self.strategy_macd_30s_reclaim_excluded_symbols.strip():
            return []
        return sorted(
            {
                symbol.strip().upper()
                for symbol in self.strategy_macd_30s_reclaim_excluded_symbols.split(",")
                if symbol.strip()
            }
        )

    @computed_field
    @property
    def reconciliation_ignored_position_mismatch_pairs(self) -> set[tuple[str, str]]:
        raw = self.reconciliation_ignored_position_mismatches.strip()
        if not raw:
            return set()

        ignored: set[tuple[str, str]] = set()
        for entry in raw.split(";"):
            chunk = entry.strip()
            if not chunk:
                continue
            if ":" not in chunk:
                continue
            account_name, symbols_raw = chunk.rsplit(":", 1)
            normalized_account = account_name.strip()
            if not normalized_account:
                continue
            for symbol in symbols_raw.split(","):
                normalized_symbol = symbol.strip().upper()
                if normalized_symbol:
                    ignored.add((normalized_account, normalized_symbol))
        return ignored

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

    @staticmethod
    def _normalize_provider_name(provider: str | None) -> str | None:
        if provider is None:
            return None
        normalized = str(provider).strip().lower()
        if not normalized:
            return None
        if normalized == "alpaca_paper":
            return "alpaca"
        return normalized

    def execution_mode_for_provider(self, provider: str) -> str:
        normalized = self._normalize_provider_name(provider) or self.resolved_broker_provider
        if normalized == "schwab":
            return "live"
        if normalized == "webull":
            return "live"
        if normalized == "alpaca":
            return "paper"
        return "shadow"

    def provider_for_strategy(self, strategy_code: str) -> str:
        normalized_code = str(strategy_code).strip().lower()
        if normalized_code == "macd_30s":
            override = self._normalize_provider_name(self.strategy_macd_30s_broker_provider)
            if override is not None:
                return override
        if normalized_code == "webull_30s":
            override = self._normalize_provider_name(self.strategy_webull_30s_broker_provider)
            if override is not None:
                return override
        if normalized_code == "schwab_1m":
            override = self._normalize_provider_name(self.strategy_schwab_1m_broker_provider)
            if override is not None:
                return override
        if normalized_code == "tos":
            override = self._normalize_provider_name(self.strategy_tos_broker_provider)
            if override is not None:
                return override
        return self.resolved_broker_provider

    def provider_for_account(self, account_name: str) -> str:
        normalized_account = str(account_name).strip()
        if normalized_account == self.strategy_macd_30s_account_name:
            return self.provider_for_strategy("macd_30s")
        if normalized_account == self.strategy_webull_30s_account_name:
            return self.provider_for_strategy("webull_30s")
        if normalized_account == self.strategy_schwab_1m_account_name:
            return self.provider_for_strategy("schwab_1m")
        if normalized_account == self.strategy_tos_account_name:
            return self.provider_for_strategy("tos")
        return self.resolved_broker_provider

    def display_account_name(self, account_name: str) -> str:
        normalized_account = str(account_name).strip()
        if not normalized_account:
            return normalized_account
        provider = self.provider_for_account(normalized_account)
        if provider == "schwab" and normalized_account.startswith("paper:"):
            return f'live:{normalized_account.split(":", 1)[1]}'
        return normalized_account

    @computed_field
    @property
    def active_broker_providers(self) -> list[str]:
        providers = {self.resolved_broker_provider}
        if self.strategy_macd_30s_enabled:
            override = self._normalize_provider_name(self.strategy_macd_30s_broker_provider)
            if override is not None:
                providers.add(override)
        if self.strategy_webull_30s_enabled:
            override = self._normalize_provider_name(self.strategy_webull_30s_broker_provider)
            if override is not None:
                providers.add(override)
        if self.strategy_schwab_1m_enabled:
            override = self._normalize_provider_name(self.strategy_schwab_1m_broker_provider)
            if override is not None:
                providers.add(override)
        if self.strategy_tos_enabled:
            override = self._normalize_provider_name(self.strategy_tos_broker_provider)
            if override is not None:
                providers.add(override)
        return sorted(providers)

    @computed_field
    @property
    def broker_provider_label(self) -> str:
        providers = self.active_broker_providers
        if len(providers) == 1:
            return providers[0]
        return f"mixed ({', '.join(providers)})"

    @computed_field
    @property
    def oms_adapter_label(self) -> str:
        providers = self.active_broker_providers
        if len(providers) == 1:
            return self.oms_adapter
        return f"routing ({', '.join(providers)})"

    def parse_strategy_config_overrides(self, raw_value: str) -> dict[str, object]:
        text = raw_value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid strategy config override JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Strategy config override JSON must decode to an object")
        return dict(parsed)

    @computed_field
    @property
    def tradingview_alerts_notification_smtp_to_list(self) -> list[str]:
        if not self.tradingview_alerts_notification_smtp_to.strip():
            return []
        return [
            item.strip()
            for item in self.tradingview_alerts_notification_smtp_to.split(",")
            if item.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
