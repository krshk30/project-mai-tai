from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class TradingConfig:
    dry_run: bool = True

    default_quantity: int = 100
    max_positions: int = 10
    max_daily_loss: float = -500.0

    stop_loss_cents: float = 0.02
    stop_loss_pct: float = 1.5
    profit_floor_lock_at_1pct_peak_pct: float = 0.0
    profit_floor_lock_at_2pct_peak_pct: float = 0.5
    profit_floor_lock_at_3pct_peak_pct: float = 1.5
    profit_floor_trail_buffer_over_4pct_pct: float = 1.5

    trading_start_hour: int = 4
    trading_end_hour: int = 20
    dead_zone_start: str = "00:00"
    dead_zone_end: str = "00:00"

    confirm_bars: int = 1
    min_score: int = 4
    surge_rate: float = 0.001
    p3_histogram_floor: float = 0.001
    vol_min: int = 10_000
    stoch_entry_cap: float = 90.0
    ema9_max_distance_gate_enabled: bool = False
    ema9_max_distance_pct: float = 0.08
    cooldown_bars: int = 5
    use_ema_gate: bool = True
    p1_require_below_3bars: bool = True
    entry_vwap_mode: str = "regular"
    entry_logic_mode: str = "standard"
    require_vwap_filter: bool = False
    allow_vwap_cross_entry: bool = True
    ticker_loss_pause_streak_limit: int = 0
    ticker_loss_pause_minutes: int = 30
    ticker_loss_pause_only_on_cold_losses: bool = False
    ticker_loss_pause_cold_peak_profit_pct: float = 1.0
    max_entries_per_symbol_per_session: int = 0
    hard_stop_pause_streak_limit: int = 2
    hard_stop_pause_minutes: int = 60
    confirmation_hold_tolerance_pct: float = 0.003
    entry_intrabar_enabled: bool = False
    pretrigger_lookback_compression_bars: int = 4
    pretrigger_max_compression_range_atr: float = 1.20
    pretrigger_min_higher_lows_count: int = 2
    pretrigger_pressure_min_upper_half_closes: int = 2
    pretrigger_pressure_min_body_near_resistance_bars: int = 2
    pretrigger_pressure_close_pos_pct: float = 0.55
    pretrigger_body_near_resistance_atr_factor: float = 0.20
    pretrigger_max_pullback_to_ema9_pct: float = 0.025
    pretrigger_max_pullback_below_ema9_pct: float = 0.01
    pretrigger_max_pullback_to_vwap_pct: float = 0.020
    pretrigger_min_bar_rel_vol: float = 1.25
    pretrigger_min_bar_rel_vol_breakout: float = 1.50
    pretrigger_volume_avg_bars: int = 20
    pretrigger_min_close_pos_pct: float = 0.70
    pretrigger_max_upper_wick_pct: float = 0.25
    pretrigger_min_body_pct: float = 0.40
    pretrigger_score_threshold: int = 2
    pretrigger_entry_size_factor: float = 0.25
    pretrigger_confirm_entry_size_factor: float = 1.00
    pretrigger_failed_break_lookahead_bars: int = 3
    pretrigger_fail_hold_buf_atr: float = 0.15
    pretrigger_fail_fast_on_macd_below_signal: bool = True
    pretrigger_fail_fast_on_price_below_ema9: bool = True
    pretrigger_fail_cooldown_bars: int = 4
    pretrigger_atr_floor_pct: float = 0.002
    pretrigger_macd_near_signal_atr_factor: float = 0.12
    pretrigger_add_max_distance_to_ema9_pct: float = 0.03
    pretrigger_compression_trim_extremes: int = 1
    pretrigger_compression_min_bars: int = 3
    pretrigger_price_near_resistance_atr_factor: float = 0.15
    pretrigger_reclaim_lookback_bars: int = 8
    pretrigger_reclaim_touch_lookback_bars: int = 3
    pretrigger_reclaim_require_pullback: bool = True
    pretrigger_reclaim_min_pullback_from_high_pct: float = 0.03
    pretrigger_reclaim_max_pullback_from_high_pct: float = 0.20
    pretrigger_reclaim_use_leg_retrace_gate: bool = True
    pretrigger_reclaim_min_retrace_fraction_of_leg: float = 0.20
    pretrigger_reclaim_max_retrace_fraction_of_leg: float = 0.80
    pretrigger_reclaim_touch_tolerance_pct: float = 0.01
    pretrigger_reclaim_require_touch: bool = True
    pretrigger_reclaim_max_extension_above_ema9_pct: float = 0.03
    pretrigger_reclaim_max_extension_above_vwap_pct: float = 0.04
    pretrigger_reclaim_allow_touch_recovery_location: bool = False
    pretrigger_reclaim_touch_recovery_max_below_ema9_pct: float = 0.015
    pretrigger_reclaim_touch_recovery_max_below_vwap_pct: float = 0.015
    pretrigger_reclaim_allow_single_anchor_location: bool = False
    pretrigger_reclaim_single_anchor_other_max_gap_pct: float = 0.01
    pretrigger_reclaim_single_anchor_min_bar_rel_vol: float = 0.80
    pretrigger_reclaim_single_anchor_min_close_pos_pct: float = 0.60
    pretrigger_reclaim_single_anchor_max_upper_wick_pct: float = 0.30
    pretrigger_reclaim_single_anchor_min_body_pct: float = 0.30
    pretrigger_reclaim_require_dual_anchor_for_starter: bool = False
    pretrigger_reclaim_require_location: bool = True
    pretrigger_reclaim_min_bar_rel_vol: float = 1.10
    pretrigger_reclaim_require_volume: bool = True
    pretrigger_reclaim_min_close_pos_pct: float = 0.60
    pretrigger_reclaim_max_upper_wick_pct: float = 0.30
    pretrigger_reclaim_min_body_pct: float = 0.30
    pretrigger_reclaim_soft_min_close_pos_pct: float = 0.45
    pretrigger_reclaim_soft_max_upper_wick_pct: float = 0.45
    pretrigger_reclaim_soft_min_body_pct: float = 0.15
    pretrigger_reclaim_require_candle: bool = True
    pretrigger_reclaim_require_momentum: bool = True
    pretrigger_reclaim_require_trend: bool = True
    pretrigger_reclaim_require_stoch: bool = True
    pretrigger_reclaim_score_threshold: int = 2
    pretrigger_reclaim_require_stoch_for_min_score: bool = False
    pretrigger_reclaim_allow_current_bar_touch: bool = True
    pretrigger_reclaim_arm_break_lookahead_bars: int = 1
    pretrigger_reclaim_fail_fast_on_macd_below_signal: bool = False
    pretrigger_reclaim_fail_fast_on_price_below_ema9: bool = False
    pretrigger_reclaim_require_higher_low: bool = True
    pretrigger_reclaim_min_pullback_low_above_prespike_pct: float = 0.02
    pretrigger_reclaim_require_pullback_absorption: bool = True
    pretrigger_reclaim_pullback_volume_max_spike_ratio: float = 0.60
    pretrigger_reclaim_require_held_move: bool = True
    pretrigger_reclaim_confirm_add_min_peak_profit_pct: float = 0.0
    pretrigger_reclaim_min_held_spike_gain_ratio: float = 0.50
    pretrigger_reclaim_require_reentry_reset: bool = False
    pretrigger_reclaim_reentry_min_reset_from_high_pct: float = 0.01
    pretrigger_reclaim_reentry_touch_lookback_bars: int = 8
    pretrigger_retest_lookback_bars: int = 12
    pretrigger_retest_breakout_window_bars: int = 4
    pretrigger_retest_min_breakout_pct: float = 0.004
    pretrigger_retest_breakout_close_tolerance_pct: float = 0.0015
    pretrigger_retest_breakout_min_close_pos_pct: float = 0.60
    pretrigger_retest_breakout_min_range_expansion: float = 1.00
    pretrigger_retest_max_pullback_from_breakout_pct: float = 0.03
    pretrigger_retest_level_tolerance_pct: float = 0.0035
    pretrigger_retest_min_bar_rel_vol: float = 1.00
    pretrigger_retest_min_breakout_bar_rel_vol: float = 1.25
    pretrigger_retest_min_confirm_bar_rel_vol: float = 1.10
    pretrigger_retest_min_close_pos_pct: float = 0.70
    pretrigger_retest_max_upper_wick_pct: float = 0.25
    pretrigger_retest_min_body_pct: float = 0.40
    pretrigger_retest_require_volume: bool = True
    pretrigger_retest_require_momentum: bool = True
    pretrigger_retest_require_trend: bool = True
    pretrigger_retest_require_dual_anchor: bool = True
    pretrigger_retest_score_threshold: int = 2
    pretrigger_retest_arm_break_lookahead_bars: int = 1

    entry_preconditions_enabled: bool = False
    entry_precondition_lookback_bars: int = 3
    entry_precondition_max_vwap_dist_pct: float = 0.01
    entry_precondition_max_ema9_dist_pct: float = 0.005
    entry_precondition_min_vol_ratio: float = 0.50
    entry_precondition_max_vol_ratio: float = 1.20
    entry_anti_chase_max_vwap_dist_pct: float = 0.015
    entry_hard_block_max_vwap_dist_pct: float = 0.08
    entry_structure_filter_enabled: bool = False
    entry_structure_recent_high_lookback_bars: int = 8
    entry_structure_breakout_margin_pct: float = 0.003
    entry_structure_near_high_block_pct: float = 0.005
    exit_stoch_health_filter_enabled: bool = False
    exit_stoch_min_slope: float = 2.0
    exit_stoch_overbought_level: float = 80.0
    schwab_native_warmup_bars_required: int = 50
    schwab_native_use_confirmation: bool = True
    schwab_native_use_chop_regime: bool = False
    require_above_ema20: bool = True
    use_stoch_k_cap: bool = True
    stoch_k_cap_level: float = 90.0
    use_ema9_max_dist: bool = True
    ema9_max_dist_pct: float = 8.0
    vwap_max_dist_pct: float = 10.0
    chop_atr_len: int = 14
    chop_compress_mult: float = 0.25
    chop_flat_mult: float = 0.35
    chop_flat_bars: int = 5
    chop_cross_bars: int = 10
    chop_cross_min: int = 3
    chop_clean_bars: int = 10
    chop_clean_min: int = 7
    chop_trigger_min_hits: int = 2
    chop_restart_vwap_closes: int = 5
    chop_restart_breakout_bars: int = 5
    chop_restart_pullback_hold_bars: int = 5
    p1_min_bars_below_signal: int = 3
    p3_min_score: int = 5
    p3_allow_high_vwap: bool = True
    p3_high_vwap_max_pct: float = 30.0
    p3_high_vwap_max_ema9_pct: float = 2.0
    p3_entry_stoch_k_cap: float | None = None
    p3_allow_momentum_override: bool = True
    p3_momentum_max_ema9_pct: float = 12.0
    p3_momentum_max_stoch_k: float = 98.0
    p3_momentum_vol_mult: float = 2.0
    p3_extreme_hist_lookback: int = 20
    p3_extreme_range_atr: float = 1.20
    p3_extreme_vol_mult: float = 1.80
    p3_extreme_delta_mult: float = 2.00
    p3_extreme_hist_mult: float = 1.25
    p3_extreme_clear_atr: float = 0.10
    p4_body_pct: float = 4.0
    p4_range_pct: float = 5.0
    p4_close_top_pct: float = 35.0
    p4_vol_mult20: float = 1.50
    p4_breakout_lookback: int = 3
    p4_require_close_above_ema9: bool = True
    p5_spike_lookback: int = 15
    p5_spike_ext_pct: float = 2.5
    p5_giveback_pct: float = 2.0
    p5_near_ema9_pct: float = 1.0
    p5_max_body_pct: float = 3.5
    p5_vol_mult5: float = 0.90
    p5_close_ratio: float = 0.50
    p5_breakout_bars: int = 3
    p5_momentum_lookback: int = 12
    p5_momentum_min_pct: float = 3.0
    p5_max_from_high_pct: float = 20.0

    floor_check_interval_secs: int = 5

    scale_fast4_pct: float = 4.0
    scale_fast4_sell_pct: float = 75.0
    scale_degraded1_pct: float = 1.0
    scale_degraded1_sell_pct: float = 25.0
    scale_normal2_pct: float = 2.0
    scale_normal2_sell_pct: float = 50.0
    scale_degraded2_pct: float = 2.0
    scale_degraded2_sell_pct: float = 25.0
    scale_4after2_pct: float = 4.0
    scale_4after2_sell_pct: float = 25.0

    bar_interval_secs: int = 30

    def make_tos_variant(
        self,
        *,
        quantity: int = 100,
        bar_interval_secs: int = 60,
        stop_loss_pct: float = 1.0,
        cooldown_bars: int = 5,
        dry_run: bool | None = None,
    ) -> "TradingConfig":
        fields = asdict(self)
        fields.update(
            {
                "dry_run": self.dry_run if dry_run is None else dry_run,
                "default_quantity": quantity,
                "bar_interval_secs": bar_interval_secs,
                "stop_loss_pct": stop_loss_pct,
                "confirm_bars": 0,
                "min_score": 0,
                "cooldown_bars": cooldown_bars,
                "use_ema_gate": False,
                "p1_require_below_3bars": False,
                "dead_zone_start": "00:00",
                "dead_zone_end": "00:00",
                "entry_vwap_mode": "session_aware",
                "entry_logic_mode": "tos_script",
                "entry_intrabar_enabled": True,
                "require_vwap_filter": True,
                "allow_vwap_cross_entry": True,
                "vol_min": 5_000,
                "stoch_entry_cap": 101.0,
                "p3_histogram_floor": 999.0,
            }
        )
        return TradingConfig(**fields)

    def make_1m_variant(
        self,
        *,
        quantity: int = 100,
        bar_interval_secs: int = 60,
        stop_loss_pct: float = 1.0,
        min_score: int = 4,
        confirm_bars: int = 1,
        cooldown_bars: int = 1,
        dry_run: bool | None = None,
    ) -> "TradingConfig":
        fields = asdict(self)
        fields.update(
            {
                "dry_run": self.dry_run if dry_run is None else dry_run,
                "default_quantity": quantity,
                "bar_interval_secs": bar_interval_secs,
                "stop_loss_pct": stop_loss_pct,
                "min_score": min_score,
                "confirm_bars": confirm_bars,
                "cooldown_bars": cooldown_bars,
                "entry_vwap_mode": "session_aware",
            }
        )
        return TradingConfig(**fields)

    def make_30s_variant(
        self,
        *,
        quantity: int = 100,
        dry_run: bool | None = None,
        entry_preconditions_enabled: bool = False,
    ) -> "TradingConfig":
        fields = asdict(self)
        fields.update(
            {
                "dry_run": self.dry_run if dry_run is None else dry_run,
                "default_quantity": quantity,
                "bar_interval_secs": 30,
                "entry_preconditions_enabled": entry_preconditions_enabled,
                "entry_structure_filter_enabled": False,
                "exit_stoch_health_filter_enabled": True,
                "entry_vwap_mode": "session_aware",
                "p3_histogram_floor": 0.01,
                "ema9_max_distance_gate_enabled": True,
                "ema9_max_distance_pct": 0.08,
                "confirmation_hold_tolerance_pct": 0.003,
                "ticker_loss_pause_streak_limit": 3,
                "ticker_loss_pause_minutes": 30,
                "max_entries_per_symbol_per_session": 0,
                "hard_stop_pause_streak_limit": 2,
                "hard_stop_pause_minutes": 60,
            }
        )
        return TradingConfig(**fields)

    def make_30s_schwab_native_variant(
        self,
        *,
        quantity: int = 100,
        dry_run: bool | None = None,
    ) -> "TradingConfig":
        fields = asdict(self)
        fields.update(
            {
                "dry_run": self.dry_run if dry_run is None else dry_run,
                "default_quantity": quantity,
                "bar_interval_secs": 30,
                "entry_logic_mode": "schwab_native_30s",
                "trading_start_hour": 7,
                "trading_end_hour": 18,
                "dead_zone_start": "00:00",
                "dead_zone_end": "00:00",
                "confirm_bars": 0,
                "min_score": 4,
                "p3_min_score": 5,
                "vol_min": 2_500,
                "cooldown_bars": 10,
                "p3_histogram_floor": 0.01,
                "use_ema_gate": True,
                "require_above_ema20": True,
                "use_stoch_k_cap": True,
                "stoch_k_cap_level": 90.0,
                "use_ema9_max_dist": True,
                "ema9_max_dist_pct": 8.0,
                "vwap_max_dist_pct": 10.0,
                "entry_intrabar_enabled": False,
                "schwab_native_use_confirmation": True,
                "schwab_native_use_chop_regime": True,
                "schwab_native_warmup_bars_required": 50,
                "p3_allow_momentum_override": False,
                "p3_entry_stoch_k_cap": 85.0,
            }
        )
        return TradingConfig(**fields)

    def make_30s_webull_variant(
        self,
        *,
        quantity: int = 100,
        dry_run: bool | None = None,
    ) -> "TradingConfig":
        fields = asdict(
            self.make_30s_schwab_native_variant(
                quantity=quantity,
                dry_run=dry_run,
            )
        )
        fields.update(
            {
                "trading_start_hour": 4,
                "trading_end_hour": 18,
            }
        )
        return TradingConfig(**fields)

    def make_1m_schwab_native_variant(
        self,
        *,
        quantity: int = 100,
        dry_run: bool | None = None,
    ) -> "TradingConfig":
        fields = asdict(
            self.make_30s_schwab_native_variant(
                quantity=quantity,
                dry_run=dry_run,
            )
        )
        fields.update(
            {
                "bar_interval_secs": 60,
            }
        )
        return TradingConfig(**fields)

    def make_30s_pretrigger_variant(
        self,
        *,
        quantity: int = 100,
        dry_run: bool | None = None,
    ) -> "TradingConfig":
        fields = asdict(self)
        fields.update(
            {
                "dry_run": self.dry_run if dry_run is None else dry_run,
                "default_quantity": quantity,
                "bar_interval_secs": 30,
                "entry_logic_mode": "pretrigger_probe",
                "confirm_bars": 0,
                "min_score": 0,
                "use_ema_gate": False,
                "entry_preconditions_enabled": False,
                "entry_structure_filter_enabled": False,
                "entry_vwap_mode": "session_aware",
                "ticker_loss_pause_streak_limit": 3,
                "ticker_loss_pause_minutes": 30,
                "max_entries_per_symbol_per_session": 0,
                "hard_stop_pause_streak_limit": 2,
                "hard_stop_pause_minutes": 60,
            }
        )
        return TradingConfig(**fields)

    def make_30s_reclaim_variant(
        self,
        *,
        quantity: int = 100,
        dry_run: bool | None = None,
    ) -> "TradingConfig":
        fields = asdict(self)
        fields.update(
            {
                "dry_run": self.dry_run if dry_run is None else dry_run,
                "default_quantity": quantity,
                "bar_interval_secs": 30,
                "entry_logic_mode": "pretrigger_reclaim",
                "confirm_bars": 0,
                "min_score": 0,
                "use_ema_gate": False,
                "entry_preconditions_enabled": False,
                "entry_structure_filter_enabled": False,
                "entry_vwap_mode": "session_aware",
                "pretrigger_reclaim_touch_lookback_bars": 8,
                "pretrigger_reclaim_min_pullback_from_high_pct": 0.0025,
                "pretrigger_reclaim_max_pullback_from_high_pct": 0.15,
                "pretrigger_reclaim_max_retrace_fraction_of_leg": 1.2,
                "pretrigger_reclaim_max_extension_above_ema9_pct": 0.02,
                "pretrigger_reclaim_max_extension_above_vwap_pct": 0.04,
                "pretrigger_reclaim_require_higher_low": False,
                "pretrigger_reclaim_require_held_move": False,
                "pretrigger_reclaim_require_volume": False,
                "pretrigger_reclaim_require_pullback_absorption": False,
                "pretrigger_reclaim_require_stoch": False,
                "pretrigger_reclaim_confirm_add_min_peak_profit_pct": 1.0,
                "profit_floor_lock_at_1pct_peak_pct": 0.25,
                "profit_floor_lock_at_2pct_peak_pct": 0.75,
                "pretrigger_reclaim_require_reentry_reset": False,
                "pretrigger_reclaim_reentry_min_reset_from_high_pct": 0.01,
                "pretrigger_reclaim_reentry_touch_lookback_bars": 8,
                "pretrigger_failed_break_lookahead_bars": 4,
                "pretrigger_reclaim_fail_fast_on_macd_below_signal": False,
                "pretrigger_reclaim_fail_fast_on_price_below_ema9": False,
                  "ticker_loss_pause_streak_limit": 3,
                  "ticker_loss_pause_minutes": 30,
                  "ticker_loss_pause_only_on_cold_losses": True,
                  "ticker_loss_pause_cold_peak_profit_pct": 1.0,
              }
          )
        return TradingConfig(**fields)

    def make_30s_retest_variant(
        self,
        *,
        quantity: int = 100,
        dry_run: bool | None = None,
    ) -> "TradingConfig":
        fields = asdict(self)
        fields.update(
            {
                "dry_run": self.dry_run if dry_run is None else dry_run,
                "default_quantity": quantity,
                "bar_interval_secs": 30,
                "entry_logic_mode": "pretrigger_retest",
                "confirm_bars": 0,
                "min_score": 0,
                "use_ema_gate": False,
                "entry_preconditions_enabled": False,
                "entry_structure_filter_enabled": False,
                "entry_vwap_mode": "session_aware",
                "pretrigger_entry_size_factor": 1.0,
                "pretrigger_confirm_entry_size_factor": 0.0,
                "pretrigger_fail_fast_on_macd_below_signal": False,
                "pretrigger_fail_fast_on_price_below_ema9": False,
                "pretrigger_retest_breakout_window_bars": 6,
                "pretrigger_retest_min_breakout_pct": 0.0025,
                "pretrigger_retest_breakout_close_tolerance_pct": 0.0015,
                "pretrigger_retest_breakout_min_close_pos_pct": 0.60,
                "pretrigger_retest_breakout_min_range_expansion": 1.00,
                "pretrigger_retest_max_pullback_from_breakout_pct": 0.04,
                "pretrigger_retest_level_tolerance_pct": 0.005,
                "ticker_loss_pause_streak_limit": 3,
                "ticker_loss_pause_minutes": 30,
            }
        )
        return TradingConfig(**fields)
