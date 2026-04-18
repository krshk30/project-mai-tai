from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IndicatorConfig:
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    stoch_len: int = 5
    stoch_smooth_k: int = 1
    stoch_smooth_d: int = 3
    stoch_exit_level: float = 20.0
    ema1_len: int = 9
    ema2_len: int = 20
    vwap_session_start_hour: int = 9
    vwap_session_start_minute: int = 30
    vwap_session_end_hour: int = 16
    vwap_session_end_minute: int = 0
    extended_vwap_session_start_hour: int = 4
    extended_vwap_session_start_minute: int = 0
    extended_vwap_session_end_hour: int = 16
    extended_vwap_session_end_minute: int = 0


@dataclass
class MomentumAlertConfig:
    min_price: float = 1.0
    max_price: float = 10.0
    min_momentum_volume: int = 100_000
    squeeze_5min_pct: float = 5.0
    squeeze_10min_pct: float = 10.0
    volume_spike_mult: float = 5.0
    alert_cooldown_mins: int = 5


@dataclass
class MomentumConfirmedConfig:
    confirmed_min_volume: int = 500_000
    confirmed_max_float: int = 50_000_000
    rank_min_score: float = 50.0
    live_min_change_pct: float = 20.0
