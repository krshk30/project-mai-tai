from __future__ import annotations

from functools import lru_cache
import json

from pydantic import AliasChoices, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _legacy_strategy_alias_field(default: object, primary_name: str, legacy_name: str) -> object:
    return Field(
        default=default,
        validation_alias=AliasChoices(
            primary_name,
            legacy_name,
            f"MAI_TAI_{primary_name.upper()}",
            f"MAI_TAI_{legacy_name.upper()}",
        ),
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MAI_TAI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
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
    redis_market_data_stream_maxlen: int = 100_000
    redis_market_data_subscription_stream_maxlen: int = 250
    redis_strategy_intent_stream_maxlen: int = 2_000
    redis_order_event_stream_maxlen: int = 2_000
    redis_strategy_state_stream_maxlen: int = 250
    redis_strategy_state_isolated_stream_maxlen: int = 50
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
    strategy_polygon_30s_enabled: bool = _legacy_strategy_alias_field(
        False,
        "strategy_polygon_30s_enabled",
        "strategy_webull_30s_enabled",
    )
    strategy_schwab_1m_enabled: bool = False
    strategy_macd_30s_live_aggregate_bars_enabled: bool = False
    strategy_macd_30s_live_aggregate_fallback_enabled: bool = True
    strategy_macd_30s_live_aggregate_stale_after_seconds: int = 3
    strategy_macd_30s_tick_bar_close_grace_seconds: float = 7.5
    strategy_macd_30s_trade_stream_service: str = "LEVELONE_EQUITIES"
    strategy_polygon_30s_live_aggregate_bars_enabled: bool = _legacy_strategy_alias_field(
        False,
        "strategy_polygon_30s_live_aggregate_bars_enabled",
        "strategy_webull_30s_live_aggregate_bars_enabled",
    )
    strategy_polygon_30s_force_tick_built_mode: bool = _legacy_strategy_alias_field(
        False,
        "strategy_polygon_30s_force_tick_built_mode",
        "strategy_webull_30s_force_tick_built_mode",
    )
    strategy_polygon_30s_live_aggregate_fallback_enabled: bool = _legacy_strategy_alias_field(
        False,
        "strategy_polygon_30s_live_aggregate_fallback_enabled",
        "strategy_webull_30s_live_aggregate_fallback_enabled",
    )
    strategy_polygon_30s_force_live_bar_only_mode: bool = _legacy_strategy_alias_field(
        False,
        "strategy_polygon_30s_force_live_bar_only_mode",
        "strategy_webull_30s_force_live_bar_only_mode",
    )
    strategy_polygon_30s_live_aggregate_stale_after_seconds: int = _legacy_strategy_alias_field(
        3,
        "strategy_polygon_30s_live_aggregate_stale_after_seconds",
        "strategy_webull_30s_live_aggregate_stale_after_seconds",
    )
    strategy_polygon_30s_tick_bar_close_grace_seconds: float = _legacy_strategy_alias_field(
        2.0,
        "strategy_polygon_30s_tick_bar_close_grace_seconds",
        "strategy_webull_30s_tick_bar_close_grace_seconds",
    )
    strategy_polygon_30s_trade_stream_service: str = _legacy_strategy_alias_field(
        "TIMESALE_EQUITY",
        "strategy_polygon_30s_trade_stream_service",
        "strategy_webull_30s_trade_stream_service",
    )
    strategy_macd_30s_massive_indicator_overlay_enabled: bool = True
    strategy_macd_30s_probe_enabled: bool = False
    strategy_macd_30s_reclaim_enabled: bool = False
    strategy_macd_30s_retest_enabled: bool = False
    strategy_macd_30s_default_quantity: int = 100
    strategy_polygon_30s_default_quantity: int = _legacy_strategy_alias_field(
        100,
        "strategy_polygon_30s_default_quantity",
        "strategy_webull_30s_default_quantity",
    )
    strategy_schwab_1m_default_quantity: int = 100
    # schwab_1m_v2: isolated parallel 1m bot. Shares the existing Schwab
    # OAuth token but has a dedicated REST-poll client, bar builder, strategy
    # body, and service process. Strategy body is a placeholder until the
    # operator's spec arrives.
    strategy_schwab_1m_v2_enabled: bool = False
    strategy_schwab_1m_v2_bar_poll_interval_seconds: float = 15.0
    strategy_schwab_1m_v2_quote_poll_interval_seconds: float = 5.0
    strategy_schwab_1m_v2_max_watchlist_size: int = 25
    strategy_schwab_1m_v2_account_name: str = "paper:schwab_1m_v2"
    strategy_schwab_1m_v2_broker_provider: str | None = "schwab"
    strategy_schwab_1m_v2_default_quantity: int = 100
    # Cold-start warmup lookback (calendar days). The first poll per symbol
    # (since=0) requests this many days back so the indicator-seed batch
    # always reaches the last completed trading session even across a
    # multi-day market closure (weekend + holiday). A fixed 24h window
    # returns an EMPTY candle array after e.g. a Fri->Tue Memorial-Day gap,
    # which silently starves the strategy of warmup data. 7 days covers
    # that gap with buffer. Incremental polls (since>0) use a 24h window.
    strategy_schwab_1m_v2_warmup_lookback_days: int = 7
    # schwab_1m_v2 streamer: dedicated WebSocket bar feed (CHART_EQUITY) in
    # `market_data/schwab_v2_streamer.py`. Default OFF — the streamer shares
    # the same OAuth token as the existing schwab_streamer.py session, and
    # Schwab's streamer may limit one concurrent WS per OAuth user. Flip
    # ONLY during an evening test window with eyes on the existing
    # schwab_1m / macd_30s logs for collision symptoms. Rollback = flip
    # back to false + restart project-mai-tai-schwab-1m-v2.service. REST
    # poller keeps running concurrently for cold-start warmup + reconnect
    # gap-fill (both feed `_handle_bar`; strategy + persist are idempotent).
    strategy_schwab_1m_v2_streamer_enabled: bool = False
    strategy_schwab_1m_v2_streamer_reconnect_base_secs: float = 1.0
    strategy_schwab_1m_v2_streamer_reconnect_max_secs: float = 30.0
    # --- SPOF Workstream A (v2 follow-up): loop-resilience knobs ---
    # See docs/schwab-1m-v2-loop-resilience-design.md. Per-task backstop so an
    # unanticipated exception can't silently kill a v2 task loop.
    strategy_schwab_1m_v2_loop_error_backoff_seconds: float = 1.0
    strategy_schwab_1m_v2_loop_persistent_failure_threshold: int = 3
    # Cadence of the run() task-liveness supervisor (detects a task that ended
    # unexpectedly while the heartbeat task keeps running — v2's silent-death risk).
    strategy_schwab_1m_v2_task_liveness_check_interval_seconds: float = 15.0
    # Controlled fault-injection for the post-deploy survival test (default 0 = OFF).
    # When > 0, the next N _handle_bar_from_rest calls (the E1 callback path — v2's
    # real remaining escape) raise a synthetic RuntimeError so an operator can prove
    # the bar loop survives + escalates in a safe window. Self-clears after N.
    strategy_schwab_1m_v2_loop_fault_injection_count: int = 0
    # CSV of symbols (or "*" for all watchlist symbols) for which
    # `_evaluate_completed_bar` emits a `[V2-MACD-PROBE]` INFO log per
    # evaluated bar, dumping every input needed to cross-check the bot's
    # MACD/EMA/VWAP/stoch against TOS for the same minute. Diagnostic-only
    # — never changes strategy behavior. Default empty = no probe.
    strategy_schwab_1m_v2_macd_probe_symbols: str = ""
    strategy_macd_30s_reclaim_excluded_symbols: str = "JEM,CYCN,BFRG,UCAR,BBGI"
    # Maximum age (seconds) for the `scanner_confirmed_last_nonempty` snapshot
    # to be eligible for startup restore. Older snapshots are skipped, so
    # after-active-hours restarts (e.g. 20:43 ET) don't carry yesterday's
    # confirmed candidates and bot handoff into the next session. Set to 0 to
    # disable the age check.
    strategy_seeded_snapshot_max_age_seconds: float = 3600.0
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
    market_data_archive_retention_enabled: bool = True
    market_data_archive_retention_minutes: int = 120
    market_data_archive_retention_max_symbols: int = 50
    strategy_macd_1m_enabled: bool = False
    strategy_tos_enabled: bool = False
    strategy_runner_enabled: bool = False
    strategy_macd_1m_massive_indicator_overlay_enabled: bool = False
    strategy_macd_1m_taapi_indicator_source_enabled: bool = False
    strategy_macd_30s_common_config_overrides_json: str = ""
    strategy_macd_30s_config_overrides_json: str = ""
    strategy_polygon_30s_config_overrides_json: str = _legacy_strategy_alias_field(
        "",
        "strategy_polygon_30s_config_overrides_json",
        "strategy_webull_30s_config_overrides_json",
    )
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
    strategy_polygon_30s_account_name: str = _legacy_strategy_alias_field(
        "live:polygon_30s",
        "strategy_polygon_30s_account_name",
        "strategy_webull_30s_account_name",
    )
    strategy_polygon_30s_broker_provider: str | None = _legacy_strategy_alias_field(
        "webull",
        "strategy_polygon_30s_broker_provider",
        "strategy_webull_30s_broker_provider",
    )
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
    # Dedicated token refresher (P0, runs in the control service). Owns keeping the
    # on-disk access_token fresh, independent of any bot/broker-sync/account hash.
    schwab_token_refresher_enabled: bool = True
    schwab_token_refresher_check_interval_seconds: int = 60
    schwab_token_refresher_dead_token_backoff_seconds: int = 30
    schwab_token_refresher_max_dead_token_retries: int = 5
    # Single-writer invariant: once the dedicated refresher owns token freshness,
    # set this False so the OMS adapter becomes a PURE READER (on expiry it reloads
    # the refresher's token from disk instead of running its own refresh grant).
    # Default True preserves current behavior; flip False at deploy AFTER the
    # refresher is confirmed refreshing (no-gap cutover).
    schwab_adapter_token_refresh_enabled: bool = True
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
    schwab_emergency_close_rest_rescue_enabled: bool = True
    schwab_prewarm_symbol_ttl_seconds: float = 900.0
    # CHART_EQUITY subscription grace window (fix v3, 2026-06-01). After a
    # SUBS/ADD/UNSUBS confirmation, suppress the case-2 path 2 short-circuit
    # in SchwabStreamerClient._should_force_reconnect_for_chart_inactivity
    # for this many seconds, so CHART has a chance to deliver its first bar
    # after subscription. 0 = use computed default (CHART_BAR_INTERVAL_SECONDS
    # + max(30, schwab_stream_symbol_stale_after_seconds * 4) = 92s with the
    # default base=8s — matches PR #228's interval-aware deadline knob).
    # See docs/schwab-chart-grace-window-design.md for the full reasoning.
    schwab_chart_subscription_grace_seconds: float = 0.0
    # --- SPOF Workstream A: strategy-engine main-loop resilience knobs ---
    # See docs/strategy-engine-main-loop-resilience-design.md. The main loop must
    # survive any exception from a Schwab-touching step (dead-token RuntimeError,
    # streamer-side RuntimeError) instead of zombifying the process.
    # Backoff after an outer-backstop catch, to avoid a hot spin on persistent failure.
    strategy_main_loop_error_backoff_seconds: float = 1.0
    # Consecutive same-step failures before main_loop_health escalates to
    # "degraded-persistent" (loud + dashboard-visible). Single transients stay quiet.
    strategy_main_loop_persistent_failure_threshold: int = 3
    # Generous per-step timeout for the wrapped Schwab REST history-refresh calls,
    # so a network hang is contained as a step failure rather than stalling the loop.
    strategy_main_loop_step_timeout_seconds: float = 30.0
    # Controlled fault-injection for the post-deploy survival test (default 0 = OFF).
    # When > 0, the next N _refresh_stale_schwab_1m_history calls raise a synthetic
    # RuntimeError so an operator can prove the loop survives + escalates in a safe
    # window without waiting for a real Schwab token death. Self-clears after N.
    strategy_main_loop_fault_injection_count: int = 0
    protected_symbols: str = ""
    webull_base_url: str = "https://api.webull.com"
    webull_region_id: str = "us"
    webull_request_timeout_seconds: int = 10
    webull_app_key: str | None = None
    webull_app_secret: str | None = None
    webull_account_id: str | None = None
    oms_broker_sync_interval_seconds: int = 5
    oms_working_order_refresh_seconds: int = 5
    # Stuck-intent cancellation (2026-05-18 incident: pre-market intents
    # for AUUD/QNCX/SBFM kept retrying for 4.5 hours and 400+ attempts
    # each because the OMS had no max-age cap, no quote-drift sanity, and
    # no setup re-validation on retry).
    oms_intent_max_age_seconds: int = 30
    oms_quote_drift_cancel_tolerance_cents: float = 1.0
    oms_intent_setup_revalidation_enabled: bool = True
    oms_stop_guard_refresh_stage_1_seconds: float = 1.0
    oms_stop_guard_refresh_stage_2_seconds: float = 2.0
    oms_stop_guard_refresh_stage_3_seconds: float = 3.0
    oms_stop_guard_refresh_stage_1_buffer_pct: float = 3.0
    oms_stop_guard_refresh_stage_2_buffer_pct: float = 5.0
    oms_after_hours_stop_guard_quote_max_age_ms: int = 1000
    oms_after_hours_stop_guard_initial_panic_buffer_pct: float = 1.0
    oms_after_hours_stop_guard_catastrophic_gap_pct: float = 1.5
    oms_after_hours_stop_guard_catastrophic_panic_buffer_pct: float = 8.0

    dashboard_refresh_seconds: int = 5
    dashboard_snapshot_persistence_enabled: bool = True
    dashboard_scanner_history_retention: int = 5_000
    dashboard_trade_forensics_enabled: bool = False
    dashboard_trade_forensics_lookback_days: int = 2
    dashboard_trade_forensics_cache_ttl_seconds: float = 30.0
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
    def protected_symbol_set(self) -> frozenset[str]:
        if not self.protected_symbols.strip():
            return frozenset()
        return frozenset(
            symbol.strip().upper()
            for symbol in self.protected_symbols.split(",")
            if symbol.strip()
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
    def strategy_polygon_30s_runtime_uses_live_aggregate_bars(self) -> bool:
        return bool(self.strategy_polygon_30s_live_aggregate_bars_enabled) and not bool(
            self.strategy_polygon_30s_force_tick_built_mode
        )

    @computed_field
    @property
    def strategy_polygon_30s_runtime_live_aggregate_fallback_enabled(self) -> bool:
        # Polygon's canonical 1s aggregate feed can go patchy while raw trade
        # ticks keep flowing. Keep live bars as the primary path, but default to
        # allowing trade-tick recovery unless we explicitly force live-bar-only
        # mode for diagnostics.
        return not bool(self.strategy_polygon_30s_force_live_bar_only_mode)

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
        if normalized_code in {"polygon_30s", "webull_30s"}:
            override = self._normalize_provider_name(self.strategy_polygon_30s_broker_provider)
            if override is not None:
                return override
        if normalized_code == "schwab_1m":
            override = self._normalize_provider_name(self.strategy_schwab_1m_broker_provider)
            if override is not None:
                return override
        if normalized_code == "schwab_1m_v2":
            override = self._normalize_provider_name(self.strategy_schwab_1m_v2_broker_provider)
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
        if normalized_account == self.strategy_polygon_30s_account_name:
            return self.provider_for_strategy("polygon_30s")
        if normalized_account == self.strategy_schwab_1m_account_name:
            return self.provider_for_strategy("schwab_1m")
        if normalized_account == self.strategy_schwab_1m_v2_account_name:
            return self.provider_for_strategy("schwab_1m_v2")
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
        if self.strategy_polygon_30s_enabled:
            override = self._normalize_provider_name(self.strategy_polygon_30s_broker_provider)
            if override is not None:
                providers.add(override)
        if self.strategy_schwab_1m_enabled:
            override = self._normalize_provider_name(self.strategy_schwab_1m_broker_provider)
            if override is not None:
                providers.add(override)
        if self.strategy_schwab_1m_v2_enabled:
            override = self._normalize_provider_name(self.strategy_schwab_1m_v2_broker_provider)
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

    def market_data_provider_for_strategy(self, strategy_code: str) -> str:
        normalized_code = str(strategy_code).strip().lower()
        if normalized_code in {
            "macd_30s",
            "macd_30s_probe",
            "macd_30s_reclaim",
            "macd_30s_retest",
            "schwab_1m",
            "schwab_1m_v2",
        }:
            return "schwab"
        if normalized_code in {"polygon_30s", "webull_30s"}:
            return "polygon"
        if normalized_code == "tos" and self.provider_for_strategy("tos") == "schwab":
            return "schwab"
        return "polygon"

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
