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
    # ORB (P6 "OPEN") — opening-range breakout, default OFF / flag-gated / inert.
    # Settled config: ENTRY 5-min OR from 09:30 (close>OR_high, vol>=1.5x, >VWAP,
    # >EMA9, width<12%, cutoff 10:30, one/symbol, ONLY pre-09:25-confirmed names);
    # EXIT TRAIL-8% (ratchets from HWM). Logic in strategy_core/orb_intrabar.py;
    # see docs/orb-intrabar-production-wiring-design.md. With orb_enabled=False the
    # wiring is never reached (backward-compatible by construction; parity EXACT).
    orb_enabled: bool = False
    orb_execution_mode: str = "bar_close"   # "bar_close" (parity) | "intrabar"
    orb_or_minutes: int = 5
    orb_vol_mult: float = 1.5
    orb_width_max_pct: float = 12.0
    orb_width_min_pct: float = 2.0
    orb_cutoff_minutes: int = 60            # last entry = open + 60m = 10:30 ET
    orb_trail_pct: float = 8.0
    orb_universe_lead_minutes: int = 5      # confirmed by open - 5m = 09:25
    orb_broker_account_name: str = "paper:orb"
    orb_quantity: int = 10
    # Broker provider for the ORB account. None -> resolved_broker_provider (default,
    # behaviour-identical to pre-wiring). Set to "webull" + flip orb_broker_account_name
    # to the live account to route ORB to the real Webull account.
    orb_broker_provider: str | None = None
    # --- Intrabar-reclaim live test (cap-off + 3% trail), flag-gated, default OFF ---
    # When True: entry is the intrabar reclaim-of-OR_high (price crosses OR_high and
    # HOLDS for orb_reclaim_hold_secs) placed as a RESTING LIMIT at OR_high; the 12%
    # width cap is removed (any width arms); exit trail = orb_reclaim_trail_pct; size
    # = orb_reclaim_quantity. With it False, ORB is byte-identical to the settled
    # bar-close/TRAIL-8%/12%-cap path above. See docs/orb-reclaim-capoff-trail-design.md.
    orb_intrabar_reclaim_enabled: bool = False
    orb_reclaim_trail_pct: float = 3.0
    orb_reclaim_quantity: int = 5
    orb_reclaim_hold_secs: int = 25
    # --- Running-high breakout mode (operator-validated 2026-06-24), flag-gated, default OFF ---
    # When True (and reclaim OFF): observe from 09:25, reference = running highest 1-min
    # bar-high since 09:25; from 09:30 to (open + orb_running_high_window_minutes), enter when a
    # bar's high breaks the running high, at the breakout level, only if the fill is within
    # orb_running_high_gap_cap_pct of the broken high (else skip = don't chase). Exit = OMS
    # trail orb_reclaim_trail_pct; size = orb_reclaim_quantity. v1 = SINGLE entry per symbol
    # (re-entry is a follow-up needing OMS position-sync). With False, ORB is byte-identical to
    # the bar-close/reclaim paths above. See ORB_RULES.md.
    orb_running_high_enabled: bool = False
    orb_running_high_window_minutes: int = 30   # entries only 09:30 .. open+30 = 10:00 ET
    orb_running_high_gap_cap_pct: float = 1.5

    # OMS-quote-priced ORB entry (Piece 1 of the OMS-pricing port; see
    # docs/orb-oms-quote-priced-entry-design.md). With it False, ORB is byte-identical:
    # the bot ships its signal-time break-level limit and the OMS passes it through.
    # With it True, the bot OMITS limit_price/reference_price (fail-closed: a stale price
    # is structurally unshippable) and the OMS re-prices the entry from its OWN live quote
    # (Polygon NBBO) at placement: limit = min(ask + 1 tick, break_level*(1+gap_cap)); it
    # ABANDONS (no submit) on no-fresh-quote / ask-past-gap-cap / missing-bound. ORB-only,
    # entry-side only; the exit/stop path and other bots are untouched. NOTE: requires BOTH
    # the orb AND oms services restarted together when toggled (cross-process flag).
    orb_oms_quote_priced_entry_enabled: bool = False
    orb_oms_quote_priced_max_age_ms: int = 2000   # tunable: max ask staleness to price off
    # Resting stop-buy entry (2026-07-13 R&D): instead of a reactive limit at the break, ORB
    # emits a RESTING native BUY STOP_LIMIT at the break level (stop=level, limit=level*(1+gap_cap)).
    # It fills AT the break (not the faded ask ~3-14s late — the honest-fill leak). Running-high
    # mode only; supersedes the quote-priced limit when on. Default OFF = current behavior.
    # ⛔ GATE: needs the Webull BUY-stop plumbing validated (validate_buy_stop.py, RTH) before enable.
    orb_resting_entry_enabled: bool = False
    # P0.6 WINDOW FLATTEN (docs: P0.6-eod-flatten-design). ORB trades 09:30-10:00. AFTER 10:00 IT
    # SHOULD BE FLAT -- that is the rule, not a safety net. This enforces it.
    #
    # Holding past the window is not a running winner, it is a BROKEN EXIT: no completed ORB trade
    # has ever lasted more than 5.0 minutes (median <1 min, every entry in the first 8 minutes of
    # the session), while the only three positions that ever survived the window -- ERNA 07-15,
    # AGEN + LGPS 07-13 -- all had FAILED exits and all three were closed by hand. So flattening at
    # 10:00 clips zero winners and catches exactly the broken ones, six hours before the close.
    #
    # 10:00, NOT 15:55: an earlier design flattened before the close to beat the RTH-only native
    # stop expiring. That treated a rule violation as a protection problem. If ORB still holds at
    # 10:00 the exit machinery has failed and we want a loud alarm NOW -- liquid market, six hours
    # of runway -- not a tidy-up at 15:55.
    orb_window_flatten_enabled: bool = False
    orb_window_flatten_hour_et: int = 10
    orb_window_flatten_minute_et: int = 0
    orb_window_flatten_strategies: str = "orb"   # v2 deliberately absent (different window; design 9)
    # v2 overnight flatten (safety only): close every OMS-managed v2 position at 19:55 ET so nothing
    # rides past the 20:00 fillable gate naked (v2 arms zero native stops). Full-qty LIMIT+session close
    # via the existing v2 exit path (EH-fillable; a market order won't fill in AH). OFF => byte-identical.
    oms_v2_overnight_flatten_enabled: bool = False
    oms_v2_overnight_flatten_hour_et: int = 19
    oms_v2_overnight_flatten_minute_et: int = 55
    # v2 EOD OCO transition (Phase A, docs/premarket-eod-exit-design.md; decision A = KEEP MANAGING).
    # At 16:00 ET the native OCO legs (session=NORMAL, duration=DAY) expire with the RTH close and
    # can no longer fill — so for every OMS-managed v2 position still open, release the native-OCO
    # stand-down for the rest of the day and let the software +2%/−5% ladder resume with EH-LIMIT
    # exits (#390 routing; oms_fillable_session_* keeps 16:00–20:00 fillable). The 19:55 overnight
    # flatten stays the backstop. This does NOT liquidate and does NOT cancel/place any broker order
    # (the RTH OCO auto-expires; a NORMAL-session order can't fill in EH so nothing is lost). Idempotent
    # per (session_day, account, symbol). OFF => the transition set stays empty => byte-identical.
    oms_v2_eod_oco_transition_enabled: bool = False
    oms_v2_eod_oco_transition_hour_et: int = 16
    oms_v2_eod_oco_transition_minute_et: int = 0

    strategy_schwab_1m_v2_enabled: bool = False
    strategy_schwab_1m_v2_bar_poll_interval_seconds: float = 15.0
    strategy_schwab_1m_v2_quote_poll_interval_seconds: float = 5.0
    strategy_schwab_1m_v2_max_watchlist_size: int = 25
    strategy_schwab_1m_v2_account_name: str = "paper:schwab_1m_v2"
    # Deliberate paper routing (P1 Phase 1): route v2's orders to the SIMULATED provider so
    # it cannot reach the real Schwab account. Real-Schwab is a deliberate go-live step
    # (rename to live:schwab_1m_v2 + provider=schwab + wire the account hash THEN). Was
    # "schwab" — a latent-live default saved only by an accidental missing account-hash entry.
    strategy_schwab_1m_v2_broker_provider: str | None = "simulated"
    strategy_schwab_1m_v2_default_quantity: int = 100
    # Dual-broker v2 fan-out: when the mirror flag is ON, the OMS CW exit ladder
    # also manages the v2 position on a SECOND (Webull) broker account, so both
    # legs are evaluated per quote. OFF (default) => single-account, byte-identical.
    strategy_schwab_1m_v2_webull_mirror_enabled: bool = False
    # MUST be explicit. Was "live:orb" -- ORB's OWN live account -- which contradicts the
    # 07-10 decision to use a dedicated live:v2_webull (ORB and v2 trade the same watchlist
    # through different exit logic; one account cannot hold two open managed rows for the
    # same symbol). Unset -> _mirror_v2_fill_to_webull no-ops with a warning.
    strategy_schwab_1m_v2_webull_account_name: str = ""
    # Extended-hours Webull mirror (mirror-EH). When ON *and* the mirror flag is ON *and*
    # the primary Schwab v2 fill lands in EXTENDED HOURS, the mirror emits a marketable
    # EH-LIMIT single-leg master (NO native-OCO combo — the broker OCO is RTH-only and 417s
    # in EH) priced off OUR fresh ask, bounded by the P-B1 max-cross cap; the mirrored Webull
    # position is exit-managed by the account-aware software EH-limit CW ladder (#390). A
    # SEPARATE flag (not the primary's oms_v2_eh_entry_enabled) so enabling the isolated Schwab
    # reactive-EH entry does NOT also start writing EH orders to the SHARED live:orb account —
    # the operator enables primary-EH and mirror-EH independently. OFF (default) => in EH the
    # mirror is byte-identical to today (MARKET + combo, which the broker 417s; RTH-only mirror).
    strategy_schwab_1m_v2_webull_mirror_eh_enabled: bool = False
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
    # Track-2 Phase-2 Slice-2: v2 registers its watchlist as a market-data gateway
    # CONSUMER so the OMS quote/trade cache covers v2's symbols. DECOUPLED from the
    # exit flag (`oms_v2_exit_management_enabled`) so coverage can be deployed +
    # verified live BEFORE exits ever arm — the re-probe needs the registration
    # active while exits stay OFF. `_sync_gateway_subscription` registers when THIS
    # flag OR the exit flag is on (exits can never run without coverage). Default
    # OFF → dormant (v2 publishes no subscription; OMS feed unchanged). Live coverage
    # pre-check (2026-06-15) confirmed registration → full v2-watchlist coverage.
    strategy_schwab_1m_v2_gateway_register_enabled: bool = False
    # --- Tick capture (LEVELONE_EQUITIES) for exit replay. Default OFF: ships
    # dormant; no LEVELONE SUBS is sent and CHART_EQUITY behavior is identical.
    # Flip ONLY attended, after-close (it adds a second service subscription on
    # v2's existing streamer session). Capture is a pure observer — never feeds
    # the strategy. See docs/v2-tick-capture-design.md.
    strategy_schwab_1m_v2_tick_capture_enabled: bool = False
    # TIMESALE_EQUITY (true trade-by-trade) capture — ADDITIVE to LEVELONE, capture-only
    # (the SchwabV2TickWriter tee; shares nothing with the strategy/bar feed or execution).
    # Default off -> no TIMESALE subscription, byte-identical to today. See
    # docs/timesale-capture-design.md / /home/trader/timesale-capture-design.md.
    strategy_schwab_1m_v2_timesale_capture_enabled: bool = False
    strategy_schwab_1m_v2_tick_flush_interval_secs: float = 2.0
    strategy_schwab_1m_v2_tick_flush_batch_size: int = 500
    strategy_schwab_1m_v2_tick_max_buffer: int = 50_000

    # --- Central market-data capture (GLOBAL, bot-agnostic; market_capture_app) ---
    # A flag-gated, read-only consumer of the shared `mai_tai:market-data` stream
    # that persists raw Polygon/Massive trades + L1 quotes into market_capture_*
    # for any bot to backtest. Additive/isolated: no trading-path/gateway/bot
    # changes. Default off -> run() returns immediately. Batched off-loop writes
    # (#350 pattern). See docs/market-capture-design.md.
    market_capture_enabled: bool = False
    market_capture_batch_size: int = 1000
    market_capture_flush_secs: float = 2.0
    market_capture_provider_tag: str = "massive"
    # Stats log cadence (loop iterations) for the verify window.
    market_capture_stats_every: int = 30
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
    # --- Track 1: ATR-Flip touch entry (P3-B) — third v2 entry path. Default OFF
    # → ships DORMANT (the indicator state is computed every bar to stay warm, but
    # NO "ATR Flip" intent is emitted until this flag is on). Variant B (intrabar
    # touch of the resting trail) is the validated path; variant A (confirmed BUY
    # flip, entry at close) is kept default-off for live A/B comparison. The
    # liquidity floor (vol_floor) is the ONLY filter — none of the Paths 1/2 gates
    # apply (operator's "just the script"). Indicator math = analysis/atr_flip.py
    # (modified TR, ATRPeriod 5, ATRFactor 3.5, Wilders), ported verbatim. See
    # docs/schwab-1m-v2-atr-flip-entry-design.md.
    strategy_schwab_1m_v2_atr_flip_enabled: bool = False
    # ATR-ONLY go-live mode: hard-disable Paths 1/2 (MACD Cross / VWAP Breakout)
    # so ONLY screened-ATR can emit. P1/P2 take precedence over ATR
    # (schwab_1m_v2.py) and are the 7wk-validated losers — under live credentials
    # they must never trade ahead of ATR. Default False = current behavior
    # (reversible kill: flip back to False + restart restores P1/P2).
    strategy_schwab_1m_v2_atr_only_mode: bool = False
    # Trading window (ET) inside which v2 may OPEN a position. Outside
    # [start, end) — before 7:00 AM, at/after 4:30 PM, weekends, holidays — the emit
    # chokepoint drops "open" intents (2026-07-14 operator rule after a 7:51 PM ET
    # after-hours entry churned unfillable overnight exits). Exits are governed by
    # the OMS fillable-session gate (oms_fillable_session_*) and are NOT narrowed by
    # this window — a position opened at 4:29 PM can still be exited after 4:30 PM,
    # so this can never strand a position. end is exclusive.
    # 2026-07-15 operator rule: entries ended at 16:30 (was 18:00). 2026-07-24 (Phase A of the
    # EH-trading design, docs/premarket-eod-exit-design.md): tightened to 16:00 — NO new entries
    # after the 4:00 PM RTH close, so every open position transitions to the EOD OCO cleanup /
    # EH-limit ladder rather than a fresh 16:00–16:30 entry that fills thin and has to survive AH.
    # Applies to BOTH entry modes (resting + reactive). Rollback = env overrides back to 16/30 or 18/0.
    strategy_schwab_1m_v2_entry_window_start_hour_et: int = 7
    # 2026-07-24 Phase B (EH-trading design R1): start bumped 07:00 -> 07:30 (operator's chosen value)
    # so pre-market entries begin at 07:30 ET, once EH liquidity is meaningful. Both modes. The reactive
    # EH entry is fillable (session=AM limit, restored dc11d5a); this just narrows when it may open.
    strategy_schwab_1m_v2_entry_window_start_minute_et: int = 30
    strategy_schwab_1m_v2_entry_window_end_hour_et: int = 16
    strategy_schwab_1m_v2_entry_window_end_minute_et: int = 0
    # GO-LIVE opt-in: when False (default), the configured_schwab_accounts guard
    # REFUSES to bind a real Schwab hash to the v2 account (structural paper-safety,
    # P1 Phase 1). When True, v2's account registers the real hash so orders route
    # to Schwab. Reversible kill: flip back to False + restart → v2 re-isolated to
    # paper. Pair with broker_provider=schwab + account_name=live:schwab_1m_v2.
    strategy_schwab_1m_v2_go_live_enabled: bool = False
    strategy_schwab_1m_v2_atr_flip_variant: str = "B"          # "A" or "B"
    strategy_schwab_1m_v2_atr_flip_quantity: int = 10          # live-paper size
    strategy_schwab_1m_v2_atr_flip_vol_floor: int = 5000       # the only filter
    strategy_schwab_1m_v2_atr_flip_period: int = 5             # ATRPeriod (parity)
    strategy_schwab_1m_v2_atr_flip_factor: float = 3.5         # ATRFactor (parity)
    # CSV of symbols (or "*") for which `[V2-ATR-PROBE]` logs each evaluated bar's
    # ATR state (tr/loss/trail/state/touch). Diagnostic-only; default empty = off.
    strategy_schwab_1m_v2_atr_flip_probe_symbols: str = ""
    # Track-B fresh-flip qualifier (ATR-Flip ONLY). ATR losers fire LATE in a long
    # short segment (atr_state_age ~16 = dead-cat bounce); winners fire fresh (~2-3).
    # When enabled, screen flips with state_age >= the ceiling. Default OFF =
    # behavior-neutral. 7-week rotating-sample data picked 5 (46%->63% win idealized).
    strategy_schwab_1m_v2_atr_flip_use_max_state_age: bool = False
    strategy_schwab_1m_v2_atr_flip_max_state_age: int = 5
    # ATR-Flip RE-ARM fix (variant-B "burn-the-fake, miss-the-real-flip"). Default OFF =
    # byte-identical to the shipped one-touch-per-short-segment bool. When ON, the segment
    # guard is claimed only when a position OPENS (a fill): a hold-confirm skip / emit-
    # without-fill releases the segment so the subsequent REAL BUY flip is enterable. The
    # emit->fill release is time-based (wall-clock, not bars) and rides the 5s position
    # poll. See docs/schwab-1m-v2-atr-flip-rearm-LIVE-impl-plan.md.
    strategy_schwab_1m_v2_atr_flip_rearm_enabled: bool = False
    strategy_schwab_1m_v2_atr_flip_rearm_timeout_secs: float = 12.0
    # Hold-confirmation (intrabar, ATR variant-B only). After an INTRABAR trail-touch,
    # watch the next N seconds of LEVELONE quotes and emit the entry only if the move
    # holds (net_delta: last quote >= touch +net_delta_bps). Screens false-flip wick
    # touches that revert. Coverage guard: <min_ticks in the window -> fall back to
    # entering (matches the offline backtest's BAR_CLOSE_FALLBACK). Default OFF = inert
    # (on_quote returns None as today; no pending holds; bar-close emit unchanged).
    # Validated offline: net_delta @ N=20s, save:kill ~3.3 (10-day) / ~6.5 (covered
    # names), 82-86% winner-retention; reproduces on the live Schwab LEVELONE feed for
    # subscribed names. See docs/intrabar-hold-confirmation-design.md.
    strategy_schwab_1m_v2_hold_confirm_enabled: bool = False
    strategy_schwab_1m_v2_hold_confirm_n_seconds: int = 20
    strategy_schwab_1m_v2_hold_confirm_net_delta_bps: float = 5.0
    strategy_schwab_1m_v2_hold_confirm_min_ticks: int = 5
    # Confirmed-window entry (ATR-Flip variant "CW"; PR #1 of the confirmed-window
    # ruleset). On a BUY flip, WAIT 3 bars, track the highest high of those 3 bars,
    # then enter when a later bar's HIGH breaks above it (a SELL flip before the break
    # cancels the setup). Entry/reference price = that 3-bar-high break level
    # (idealized, like variant B's touched trail). Bypasses the A/B touch/flip logic
    # in _maybe_atr_emit. Pair with strategy_schwab_1m_v2_atr_only_mode=True so P1/P2
    # never trade ahead of it and every fresh flat bar reaches the ATR emit (so the
    # 3-bar wait counter advances each bar). Default OFF = byte-identical: the branch
    # is skipped, A/B is unchanged. Reversible kill: flip back to False + restart.
    # Entry side ONLY — OMS still owns exits (the +2% target / -5% hard stop /
    # bar-close flip land in PRs #2 and #3). See docs/atr-confirmed-window-forward-test.md.
    strategy_schwab_1m_v2_confirmed_window_enabled: bool = False
    # CW v2 (operator-validated rule refinements; requires confirmed_window_enabled=True).
    # Changes the CW ENTRY to: (5) trigger = max HIGH of the flip bar + next 2 bars (spike bar
    # INCLUDED); (6) enter INTRABAR on the first quote breaking the trigger (not bar-close);
    # (7) above-line filter — at the break require price AND the forming bar's low-so-far to be
    # above the flip level (the short trail crossed at the BUY flip); (9) reclaim up to 2 entries
    # per BUY-flip segment (no cooldown between); plus no entries 09:30-10:00 ET (ORB window).
    # Exit path (+2%/-5%/flip) and the OMS are UNCHANGED. Default OFF = the shipped bar-close CW is
    # byte-identical. Reversible kill: flip back to False + restart. See docs/cw-v2-intrabar-rules-design.md.
    strategy_schwab_1m_v2_cw_v2_enabled: bool = False
    # CW-v2 reclaim (2nd entry per BUY-flip) must wait this many NEW bars after the prior exit.
    # 0 = current behaviour (same-bar reclaim allowed). Backtest 07-09..07-14: 1 was a large
    # improvement (same-bar reclaim re-enters the just-exited micro-spike and bleeds). Read in
    # _cw_v2_quote; OFF (0) is byte-identical. Only consulted when reclaim is ENABLED below.
    strategy_schwab_1m_v2_cw_v2_reclaim_gap_bars: int = 0
    # CW-v2 RECLAIM master switch (2026-07-15 operator rule: "I don't want reclaim but don't
    # remove it — add a flag and keep it off"). ON = the shipped behaviour (up to 2 entries per
    # BUY-flip segment, gated by reclaim_gap_bars). OFF (default) = ONE entry per BUY-flip
    # segment; the reclaim code path is retained and inert, not deleted.
    # NOTE: unlike most flags here, OFF is NOT byte-identical to the previously shipped build —
    # it is a deliberate behaviour change the operator asked for, shipped as the default so the
    # live rule does not depend on an env var being present. Rollback = set this true + restart.
    strategy_schwab_1m_v2_cw_v2_reclaim_enabled: bool = False
    # P1.3 + P1.4 armed-segment safety (ONE flag gates the boot-mark AND the boot-hold; they are one
    # change). ON: reconstructed CW-v2 segments are capped on db-seed so a restart can't re-issue the
    # per-segment entry cap (the CPHI class), CW-v2 entries are held on boot until a self-verify
    # confirms zero reconstructed-uncapped segments, and the armed segments are published for the
    # armed_segments_check cron. OFF => byte-identical (no marking, no hold, no snapshot field).
    strategy_schwab_1m_v2_cw_armed_segment_safety_enabled: bool = False
    # ── CW-v2 ENTRY MODE (two independent flags; see docs/v2-resting-flip-entry-design.md) ──
    # The current REACTIVE entry: ATR flips -> wait 3 bars -> MARKET-buy the break. Default TRUE =
    # today, byte-identical. Turn OFF to silence the reactive emit (e.g. run resting-only).
    strategy_schwab_1m_v2_cw_v2_reactive_entry_enabled: bool = True
    # The new RESTING entry: while SHORT in a CONFIRM window, rest a buy-STOP-LIMIT at the ATR trail
    # line (+band) that fills AT the cross and auto-arms the +2%/-5% OTOCO. Default FALSE = inert
    # (no resting order emitted). ON = the strategy manages a resting order (place/replace-on-ratchet/
    # cancel). Independent of the reactive flag; the OMS one-position-per-symbol rule keeps them from
    # both holding the same name. Kill = flag OFF + restart (cancel any resting order first).
    strategy_schwab_1m_v2_cw_v2_resting_entry_enabled: bool = False
    # The slippage-cap band for the resting buy-stop-limit: limit = line * (1 + band%). 9-day study:
    # 0.5% = best mean / 92% fill (fills the pullback, not the spike). Tunable without code.
    strategy_schwab_1m_v2_cw_v2_resting_entry_band_pct: float = 0.5
    # STABLE-REST cadence (2026-07-23, the NVVE live lesson): re-place the resting order ONLY when the
    # ATR trail moves >= this %, never every 0.2% wiggle. The 0.2% flicker cancelled/re-placed ~every
    # 12s so no order was ever stably "out there", and we missed the cross by ~2% (filled 8.40 vs the
    # 8.22 line). 1.0% keeps one order resting to actually catch the up-cross. Tunable without code.
    strategy_schwab_1m_v2_cw_v2_resting_entry_reprice_pct: float = 1.0
    # SILENCE-ON-FILL grace (secs): once the up-flip fires while a resting order is live, a fill may be
    # settling. Hold this long for `position_qty` to confirm before touching anything -- bridges the
    # position-sync lag that spammed ~30 rejected brackets after the NVVE fill. If still flat after the
    # grace, the flip did not fill us -> retire and re-arm on the next short segment.
    strategy_schwab_1m_v2_cw_v2_resting_entry_flip_grace_secs: float = 30.0
    # LIVE-BAR gate (2026-07-23, the SKYQ lesson): only rest on the CURRENT purple line -- never on a
    # warmup-replayed / stale bar. When a symbol confirms mid-session the bot replays hours of old bars;
    # placing off those rested SKYQ at ~3h-old levels the instant it confirmed. Skip the place unless the
    # bar driving it is within this many seconds of wall-clock (live). Quiet-but-current names still
    # qualify; warmup-replayed (hours-old) bars do not.
    strategy_schwab_1m_v2_cw_v2_resting_entry_max_bar_age_secs: float = 180.0
    # ESTABLISHED-SHORT gate (2026-07-23, SKYQ): only rest once the ATR has been SHORT for >= this many
    # consecutive bars -- a REAL settled downtrend, not a 1-bar short in a whipsaw. Selectivity: skip
    # violent two-sided names (SKYQ ripped +9% then chopped) that flip repeatedly. Tunable without code.
    strategy_schwab_1m_v2_cw_v2_resting_entry_min_short_bars: int = 3
    # LIVE-BAR gate for the REACTIVE entry, EXTENDED HOURS only (2026-07-24 Phase B, #528 mirror). The
    # reactive break fires on a live QUOTE, but the ARM (cw_trigger/segment_high) is built from bar highs
    # in _cw_v2_track, which runs on EVERY bar incl. warmup replays. Pre-market, a warmup-replayed prior-
    # session BUY flip can arm the setup, and the first live quote above that STALE trigger would fire an
    # entry on an hours-old level. In EH, require the driving bar within this many seconds of wall-clock
    # before firing (never a warmup replay). RTH is byte-identical (the guard is skipped in regular hours,
    # where warmup completes by 09:30 and the 09:30-10:00 ORB skip already covers the open). Tunable.
    strategy_schwab_1m_v2_cw_v2_reactive_entry_max_bar_age_secs: float = 180.0
    # ── CW-v2 EH RESTING entry (2026-07-24 Phase B part 2 / P-B2; docs/premarket-eod-exit-design.md) ──
    # A broker buy-STOP-LIMIT trigger is dead in extended hours on BOTH brokers (Schwab RTH-only; Webull
    # stops 417). So when this flag is ON, the RESTING entry is SOFTWARE-EMULATED in EH: the strategy
    # watches live quotes and, on the ATR up-cross (price crossing the resting level UP), emits a
    # MARKETABLE EH-LIMIT buy at min(ask, level*(1+band)) — ABANDONING if the ask has gapped past
    # level*(1+band) (the same no-chase / gap-through-miss semantics the RTH broker stop-limit has). It
    # ALSO opens the resting window to 07:30 (else 09:30, byte-identical). RTH is UNCHANGED (a broker
    # buy-stop-limit rests as today). No OCO in EH (RTH-only); the software EH-limit exit ladder manages
    # the position. Read by BOTH the strategy (window + emulation) and the OMS (the band-cap re-price),
    # exactly like `confirmed_window_enabled`. OFF (default) => byte-identical: the resting window stays
    # 09:30 and no EH emulation runs. ⚠ Not yet attended-tested live; enable only after an after-hours
    # validation of the Webull/Schwab EH fill.
    strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled: bool = False
    # Fallback slippage-cap band for the EH resting entry, used only when the intent carries no
    # `resting_band_pct` (the strategy passes its own band in metadata as the single source of truth; this
    # is the belt so the OMS can never over-pay). % of the resting level.
    oms_v2_eh_resting_entry_band_pct: float = 0.5
    # Max ask staleness (ms) the EH resting entry will price off. No fresh ask within this window ->
    # ABANDON (never submit a blind limit). Mirrors the EH reactive entry's 2000ms default.
    oms_v2_eh_resting_entry_quote_max_age_ms: int = 2000
    strategy_macd_30s_reclaim_excluded_symbols: str = "JEM,CYCN,BFRG,UCAR,BBGI"
    # Maximum age (seconds) for the `scanner_confirmed_last_nonempty` snapshot
    # to be eligible for startup restore. Older snapshots are skipped, so
    # after-active-hours restarts (e.g. 20:43 ET) don't carry yesterday's
    # confirmed candidates and bot handoff into the next session. Set to 0 to
    # disable the age check.
    strategy_seeded_snapshot_max_age_seconds: float = 3600.0
    scanner_feed_retention_enabled: bool = True
    scanner_confirmed_capture_enabled: bool = False
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
    # Default to PAPER + simulated execution (mirrors orb/schwab_1m_v2). The bot
    # was historically wired live:polygon_30s + webull but Webull has no API
    # credentials, so it only ever shadow-rejected; paper:+simulated makes
    # trades actually execute in simulation and removes the live-wired-but-
    # uncredentialed footgun. (Live env still overrides via env vars.)
    strategy_polygon_30s_account_name: str = _legacy_strategy_alias_field(
        "paper:polygon_30s",
        "strategy_polygon_30s_account_name",
        "strategy_webull_30s_account_name",
    )
    strategy_polygon_30s_broker_provider: str | None = _legacy_strategy_alias_field(
        "simulated",
        "strategy_polygon_30s_broker_provider",
        "strategy_webull_30s_broker_provider",
    )
    # SAFE DEFAULT (2026-07-17): was "live:schwab_1m" — a LIVE, real-money account as the fallback
    # for a dropped env var. A default is what happens when configuration FAILS; it must never fail
    # toward real money. `paper:` also matches the convention of every neighbouring account default
    # (e.g. strategy_macd_30s_probe_account_name below). Live-inert: all 6 units set this explicitly,
    # and strategy_schwab_1m_enabled already defaults False — this is defense in depth, not the only
    # gate. (Considered: make it REQUIRED (no default, raise if unset) — strictly better, since an
    # account name is not something to guess. Deferred only because it makes every bare `Settings()`
    # raise, incl. across the test suite; that is a separate, wider change. See the PR.)
    strategy_schwab_1m_account_name: str = "paper:schwab_1m"
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
    # When False the OMS adapter is a PURE READER (on expiry it reloads the refresher's
    # token from disk instead of running its own refresh grant).
    # SAFE DEFAULT (2026-07-17): flipped True -> False. The old comment read "Default True
    # preserves current behavior; flip False at deploy AFTER the refresher is confirmed
    # refreshing (no-gap cutover)" — that was a MIGRATION SCAFFOLD for #274, and the cutover
    # COMPLETED weeks ago (live env has been `false` since; the control-plane refresher is the
    # sole owner of token freshness). The True default outlived its reason: it silently held the
    # PRE-#274 behavior as the fallback, so a dropped env var would resurrect the adapter-side
    # refresh grant — i.e. the shared-token SPOF whose failure caused the 2026-06-03..06-05
    # ~2.6-day fleet outage. A default is what happens when configuration FAILS; it must never
    # fail toward the exact SPOF a P0 removed. Live-inert: all services set this explicitly.
    # (Open question raised in the PR, not acted on: whether the True PATH should be DELETED
    # outright — an expired scaffold's endgame is deletion, not a safer default.)
    schwab_adapter_token_refresh_enabled: bool = False
    # Native OCO bracket (TRIGGER -> OCO exit pair). OFF until STEP-1 passes on this broker:
    # with it False the adapter's single-leg payload is byte-identical to pre-bracket main.
    schwab_native_bracket_enabled: bool = False
    # Webull native OCO combo bracket (v3 MASTER + STOP_PROFIT + STOP_LOSS = the same E5 dissolve
    # as Schwab's TRIGGER->OCO). OFF until Webull STEP-1 passes attended qty-1 on live:orb: with it
    # False the adapter's single-leg v1 path is byte-identical to pre-bracket main. Webull's MASTER
    # must be LIMIT/MARKET (a buy-STOP master rejects) -- the builder enforces it.
    webull_native_bracket_enabled: bool = False
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
    # Map the OMS's broker-neutral order_type tokens to Webull's OpenAPI stop enums
    # (STOP -> STOP_LOSS, STOP_LIMIT -> STOP_LOSS_LIMIT). Webull rejects the literal
    # "STOP" with ILLEGAL_PARAMETER (417), so the native broker-resident stop guard
    # never rests. Default OFF = byte-identical to today (still sends "STOP" -> 417);
    # enabling switches ORB's RTH stop-exit from the in-memory trail to the broker
    # STOP_LOSS market order. See docs/webull-native-stop-order-type-fix-design.md.
    webull_native_stop_order_type_map_enabled: bool = False
    oms_broker_sync_interval_seconds: int = 5
    oms_working_order_refresh_seconds: int = 5
    # Fillable-session window (ET, whole-hour): the OMS places/refreshes exit orders
    # only while an order can actually fill (default 7 AM–8 PM ET = Schwab pre-market
    # fills open ~7 AM, after-hours end ~8 PM). Outside it a working order (open or
    # close) is abandoned (MARKET_CLOSED) instead of endlessly cancel/re-placed — the
    # 2026-07-13 AGEN/SOBR overnight churn. end is exclusive; native stop-guard orders
    # are exempt (they are the resting overnight protection net).
    oms_fillable_session_start_hour_et: int = 7
    oms_fillable_session_end_hour_et: int = 20
    # --- OMS DB-timeout hardening (SPOF cure) ---
    # Bounds EVERY OMS DB call so a stalled connection RAISES within seconds
    # instead of hanging the asyncio event loop forever (the 2026-07-01/02 zombie:
    # a sync `session.flush()` in sync_account_positions hung on `psycopg wait`
    # with no timeout and froze the whole loop). OMS-scoped on purpose — other
    # services have legit slow queries and keep the untimed engine. Set
    # `MAI_TAI_OMS_DB_TIMEOUTS_ENABLED=false` to revert to the untimed engine.
    oms_db_timeouts_enabled: bool = True
    oms_db_statement_timeout_ms: int = 5000  # per-statement; every OMS query is sub-second normally (100x+ headroom)
    oms_db_lock_timeout_ms: int = 3000
    oms_db_connect_timeout_s: int = 5
    oms_db_pool_timeout_s: int = 5  # bounds waiting for a free pooled connection (both tasks share the pool)
    oms_db_pool_recycle_s: int = 1800
    # PR-E: roll #391's DB-timeout treatment fleet-wide to the NON-OMS services (they still
    # used the untimed factory -> a stalled DB connection could hang them unbounded, the same
    # latent class the OMS had). Timeouts ONLY (bound hangs) — no off-loop restructuring.
    service_db_timeouts_enabled: bool = True  # fleet master flag (rollback lever: false = all untimed)
    # Per-service rollback: comma-separated service names to EXCLUDE (leave untimed) even when the
    # fleet flag is on — e.g. "reconciler,control". Env: MAI_TAI_SERVICE_DB_TIMEOUTS_DISABLED_SERVICES.
    service_db_timeouts_disabled_services: str = ""
    # FAST profile — latency-critical asyncio services with small/indexed queries (the live-money
    # bots): a stalled connection must free their loop quickly. Mirrors the OMS #391 bound.
    service_db_fast_statement_timeout_ms: int = 5000
    service_db_fast_lock_timeout_ms: int = 3000
    service_db_fast_pool_timeout_s: int = 5
    # SLOW profile — services with legitimately long queries (reconciler scans, strategy-engine
    # bar-history/scanner-snapshot bulk, market-capture bulk inserts, the ~5.4s control /api/overview):
    # generous enough to NEVER cut a legit query, still finite so a dead connection can't hang forever.
    service_db_slow_statement_timeout_ms: int = 60000
    service_db_slow_lock_timeout_ms: int = 10000
    service_db_slow_pool_timeout_s: int = 10
    # Shared by both profiles.
    service_db_connect_timeout_s: int = 5
    service_db_pool_recycle_s: int = 1800
    # Track-2 Phase-2: OMS-side managed exits for schwab_1m_v2 positions. The
    # SINGLE flag across all Phase-2 slices. Default OFF → ships DORMANT: the OMS
    # does NOT create/update `oms_managed_positions` rows for v2 fills and emits
    # no v2 exit orders; behavior is identical to today (v2 positions stay
    # unmanaged). Slice 1 (this) = position-state plumbing only, no sells. The
    # sell-emitting risk legs (slice 3) gate additionally on the paper-isolation
    # re-proof. See docs/v2-exit-phase2-slice1-position-state-design.md.
    oms_v2_exit_management_enabled: bool = False
    # #6 (CLRO desync fix): mark a v2 managed position closed on the confirmed FILL, not on
    # exit-order SUBMIT. Default TRUE = fill-gated (current_quantity decrements only on
    # confirmed fills; status->closed only at qty 0; a submitted-but-unfilled exit leaves the
    # row open+monitored+broker-consistent). FALSE = legacy close-on-submit (rollback lever).
    oms_v2_exit_close_on_fill_enabled: bool = True
    # F2 (restart-while-holding): persist the in-memory `_armed_hard_stops` registry to the
    # durable `oms_armed_stops` table (mirror on arm/ratchet/decrement/close), rehydrate it on
    # boot, and reconcile OMS-owned positions BEFORE serving ticks. Fixes the pre-F2 gap where
    # an ORB position went NAKED across an OMS restart (in-memory-only stop, no boot rebuild).
    # Default TRUE = protection persists + rehydrates. FALSE = pre-F2 in-memory-only behaviour
    # (no mirror writes, empty rehydrate, no boot reconcile; table ignored) — the rollback lever.
    oms_armed_stop_persistence_enabled: bool = True
    # Slice-3: max age (ms) of the cached quote that the v2 exit ladder will act on.
    # A staler quote is skipped so a gap never mis-triggers an exit. 5s tolerates
    # normal gateway quote cadence while still skipping real gaps. Hard stop runs on
    # ANY fresh quote (NOT RTH-gated) — v2's edge is pre/after-market.
    oms_v2_exit_quote_max_age_ms: int = 5000
    # Stand down the software exit ladder while a broker-native OCO bracket is armed.
    # OFF until STEP-1 passes: with it False the ladder runs exactly as it does today.
    oms_native_oco_stand_down_enabled: bool = False
    # Fail-open dwell: how stale a broker confirmation may be before the ladder resumes.
    # 30s = 6 missed 5s syncs. Lower is safer (resumes sooner); it must never be raised
    # to the point where a dead sync loop can hold the ladder down indefinitely.
    oms_native_oco_confirmation_max_age_seconds: int = 30
    # After an OCO clears (a leg filled and closed the position), keep the software ladder
    # deferred this long as a BACKSTOP so a position genuinely still held after the grace (the
    # rare manual-OCO-cancel case) resumes the ladder rather than deferring forever. The COMMON
    # resolved-by-fill case is now closed proactively from a positive broker-flat read
    # (oms_native_oco_resolve_flat_reconcile_enabled) rather than by this timer. NOTE 2026-07-22:
    # the grace ALONE never eliminated the reject noise -- Schwab's OCO->account_positions
    # propagation was measured at ~6min live (LABT: stand-down cleared 19:39, broker flat 19:45),
    # far longer than the 90s here, so the ladder always resumed and fired ~3 rejected closes
    # before self-healing. The flat-reconcile below is the real fix; this stays a safety backstop.
    oms_native_oco_resolve_grace_seconds: int = 90
    # ⭐ 2026-07-22 fix for "3 rejected closes on every OCO resolution". A broker-native OCO fill
    # CLOSES the position but never decrements our managed row (the OMS never placed that sell),
    # so the row's only other close-path is the reject-driven _v2_close_reconcile_flat -- i.e. the
    # exit ladder must resume and churn ~3 rejected closes before the phantom clears. When ON, the
    # ~5s off-loop sync closes the phantom row DIRECTLY for any symbol whose OCO resolved BY A FILL,
    # detected from the broker's OWN execution record (fetch_oco_resolved_by_fill_symbols: a
    # recently-FILLED child SELL leg), so the ladder never resumes to fire the rejects. Keyed on the
    # fill record, NOT a positions-endpoint read -- authoritative, with none of the FLAT_INFERRED
    # ambiguity behind the 07-15 ERNA: a bracket that resolved by expiry/cancel (position still
    # HELD, e.g. an OCO that timed out at the close) has no filled leg, is skipped, and the ladder
    # manages it. FAIL-OPEN: any fetch error / adapter without the capability keeps the row for the
    # grace backstop + reject self-heal. Default False ships inert (byte-identical to today's reject
    # self-heal); flip via env after validation.
    oms_native_oco_resolve_flat_reconcile_enabled: bool = False
    # Emit a native OCO bracket on the v2 entry (entry order carries bracket metadata so the
    # Schwab adapter places TRIGGER->OCO). OFF until STEP-1 item 4 passes attended: with it
    # False the v2 entry is the unchanged single-leg order. Requires schwab_native_bracket_enabled
    # on the adapter AND oms_native_oco_stand_down_enabled (else the software ladder collides).
    oms_v2_emit_native_oco_bracket_enabled: bool = False
    # Extended-hours exit routing (2026-07-05, CLRO/CELZ stuck-exit fix). In
    # regular trading hours v2 exits stay MARKET/NORMAL (byte-identical). In
    # extended hours (AM/PM) they route as a LIMIT with session=AM|PM so they can
    # actually fill (a MARKET order cannot route in EH). Protective legs (hard
    # stop + floor) price a MARKETABLE limit buffered below the live bid so they
    # reliably cross the spread even against a slightly stale snapshot bid; the
    # buffer is a disaster-floor (fills AT the bid, not at the buffer). Scale
    # partials price at the bid (zero buffer) — patient profit-taking, harmless
    # if it doesn't fill this quote. See docs/v2-eh-exit-routing-fix-design.md.
    oms_v2_exit_eh_protective_limit_buffer_pct: float = 0.5
    # Extended-hours REACTIVE entry — marketable-limit ENHANCEMENT (2026-07-24 Phase B / P-B1). The bot
    # already routes a v2 EH open to a session=AM/PM LIMIT at the live ask (dc11d5a, restored 2026-06-23),
    # so the reactive entry is fillable pre-market TODAY. This flag layers the design's thin-EH slippage
    # protection (docs/premarket-eod-exit-design.md risk #3) ON TOP of that routing: a marketable limit
    # priced off the OMS's OWN fresh Polygon ask (`_latest_quotes_by_symbol`, NOT the broker's — Webull
    # has no market-data entitlement), buffered above the ask so it crosses reliably, and BOUNDED by a
    # max-cross cap vs the strategy's signal price — beyond the cap it prefers NO fill (skip + log) over a
    # bad thin-pre-market fill. OFF (default) = byte-identical: the bot's plain limit-at-ask stands. ⚠ Not
    # yet attended-tested live; enable only after an after-hours validation of the Webull/Schwab EH fill.
    oms_v2_eh_entry_enabled: bool = False
    # Marketable buffer above the live ask for the EH reactive entry limit (% of ask). Small so it crosses
    # the spread without overpaying; the max-cross cap below is the real bad-fill guard.
    oms_v2_eh_entry_limit_buffer_pct: float = 0.3
    # Max-cross cap for the EH reactive entry: the limit may never exceed the strategy's signal price
    # (metadata entry_price, the break level) by more than this %. If the live ask is already past the cap
    # the market has run away from the signal -> ABANDON (skip submit), preferring no fill to a bad one.
    oms_v2_eh_entry_max_cross_pct: float = 1.0
    # Max ask staleness (ms) the EH reactive entry will price off. No fresh ask within this window ->
    # ABANDON (never submit a blind limit). Mirrors the ORB quote-priced entry's 2000ms default.
    oms_v2_eh_entry_quote_max_age_ms: int = 2000
    # Confirmed-window (variant CW) OMS exit [PR #2/3]. When
    # strategy_schwab_1m_v2_confirmed_window_enabled is on (the SAME single switch the
    # CW entry reads, so entry+exit can never diverge), the v2 managed-exit runs the CW
    # exit INSTEAD of the scale/floor/stoch ladder: a FULL close at +target% (resting-
    # limit-equivalent; triggers on bid>=entry*(1+target)) OR at -stop% (market/limit at
    # the breach bid). No scales, no floor ratchet — the bar-close-confirmed flip is the
    # trend exit (PR #3). Requires oms_v2_exit_management_enabled=True (as live v2
    # already runs). OFF => the ladder path is byte-identical. Tunable without a code
    # change. See docs/atr-confirmed-window-forward-test.md.
    oms_v2_cw_target_pct: float = 2.0
    oms_v2_cw_hard_stop_pct: float = 5.0
    # CW-v2 floor exit: when True, instead of a HARD close at +target% the OMS arms a floor at
    # +floor_pct% once the bid reaches +target% and RIDES; it closes when the bid falls back to the
    # floor (or -hard_stop% before arming, or a bar-close flip). Lets winners run past +2% instead
    # of capping there. Backtest 07-09..07-14: floor@+2% + 1-bar reclaim gap + keep -5% was best
    # (+win-rate, +net). OFF (default) = byte-identical hard-target close. floor_pct defaults to the
    # target (+2%). Shared decision: exit_logic/cw_exit.py (same code path as the backtest).
    oms_v2_cw_floor_exit_enabled: bool = False
    oms_v2_cw_floor_pct: float = 2.0
    # Stuck-intent cancellation (2026-05-18 incident: pre-market intents
    # for AUUD/QNCX/SBFM kept retrying for 4.5 hours and 400+ attempts
    # each because the OMS had no max-age cap, no quote-drift sanity, and
    # no setup re-validation on retry).
    oms_intent_max_age_seconds: int = 30
    # FALSE-FLAT guard (2026-07-15 ERNA naked position, docs/false-flat-reconcile-design.md).
    # True  = a reconcile clears protection ONLY on a positively-confirmed flat read; an
    #         empty/None/failed read is UNKNOWN and never deletes an armed stop or a managed
    #         row. False = pre-fix semantics (absent/empty read counted as flat) -- rollback
    #         lever only; it re-opens the naked-position path.
    oms_reconcile_require_positive_flat: bool = True
    # Refuse a "flat" read within this many seconds of the fill that established the position
    # (a broker positions endpoint can lag a fresh fill -- ERNA's stop triggered 61s after the
    # fill and the read said flat while we held 2 shares). 0 disables the grace.
    oms_reconcile_fresh_fill_grace_secs: int = 120
    # P0.2 settlement probe: read-only, rides the existing 5s position poll (no extra broker
    # calls). Measures, PER BROKER, how long after our own fill the positions endpoint shows
    # it, and the SHAPE of each read until then. This is what turns the 120s grace above from
    # a guess into a number -- it needs no fault, unlike [RECONCILE-READ] which only fires
    # after 3 failed closes (i.e. only once the bug is already biting).
    oms_settlement_probe_enabled: bool = True
    oms_settlement_probe_timeout_secs: int = 300
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
    # When True, strategy-engine bar persistence (strategy_bar_history writes)
    # is buffered in-memory and flushed off the event loop via
    # asyncio.to_thread BEFORE the corresponding intents are published, instead
    # of running the SELECT+upsert+commit synchronously inside the event loop.
    # Default OFF: behaviour is byte-identical to the synchronous inline path.
    strategy_persist_offload_enabled: bool = False
    # Debounce the dashboard/scanner snapshot DB persist (the per-message
    # _replace_dashboard_snapshot encode+commit that saturated the loop at the
    # close — #350). 0.0 = OFF = persist on every call (byte-identical to today).
    # When > 0, coalesce per snapshot_type to at most one persist per this many
    # seconds, trailing-edge (the LATEST snapshot is always written; intermediates
    # are dropped; force-flushed on shutdown and day-roll). Live Redis state
    # publishing is unaffected — only the Postgres persist is throttled.
    snapshot_persist_throttle_secs: float = 0.0
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
        if normalized_code == "orb":
            override = self._normalize_provider_name(self.orb_broker_provider)
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
        if normalized_account == self.orb_broker_account_name:
            return self.provider_for_strategy("orb")
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
