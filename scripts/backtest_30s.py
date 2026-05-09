"""Backtest using production Schwab-native engines (replay helper).

Defaults to the **macd_30s** schwab-native TradingConfig variant. Use::

    --replay-variant schwab_1m --one-minute

to load ``make_1m_schwab_native_variant()`` (**cooldown_bars=5**, **intrabar_enabled=True**).

Data sources::

| Mode | Meaning |
|---|---|
| Tick stream | Bucket trades into Schwab-native bars (--bar-interval-secs); good for gap recovery tests. |
| Native live-minute bars (--use-live-bar-recordings) | Replays Schwab-archived ``event_type=live_bar`` blobs only (recommended for parity with broker charts). |
| schwab_1m aggregate parity (--schwab-1m-mixed-feed + schwab_1m) | Replay **trade** ticks and **live_bar** blobs in chronological order from the shared JSONL. Matches live StrategyBotRuntime aggregate mode: Schwab ticks can drive ``_evaluate_intrabar_entry_from_trade_tick`` **before** the native minute finalize lands. |

Notes on exit simulation: exits here still simplify to **bar-close** evaluation (matching the existing script). Entries can happen on ticks when mixed-feed intrabar replication is enabled.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from project_mai_tai.market_data.schwab_tick_archive import load_recorded_live_bars
from project_mai_tai.strategy_core.models import OHLCVBar
from project_mai_tai.strategy_core.schwab_native_30s import (
    SchwabNativeBarBuilder,
    SchwabNativeEntryEngine,
    SchwabNativeIndicatorEngine,
)
from project_mai_tai.strategy_core.trading_config import TradingConfig
from project_mai_tai.strategy_core.config import IndicatorConfig


EASTERN = ZoneInfo("America/New_York")


def fmt_et(ts_epoch: float) -> str:
    return datetime.fromtimestamp(ts_epoch, UTC).astimezone(EASTERN).strftime("%H:%M:%S")


def _epoch_from_tick_ns(ts_ns: int) -> float:
    if ts_ns <= 0:
        return 0.0
    if ts_ns > 1_000_000_000_000_000_000:
        return ts_ns / 1e9
    if ts_ns > 1_000_000_000_000:
        return ts_ns / 1000.0
    return float(ts_ns)


def _canonical_path_key(path: str | None) -> str:
    """Normalize engine path vs persisted history CSV labeling (e.g. P1_MACD_CROSS -> P1_CROSS)."""
    if path is None:
        return ""
    return str(path).strip().upper().replace("MACD_", "")


@dataclass
class Trade:
    symbol: str
    entry_time: str
    entry_price: float
    entry_path: str
    entry_score: int
    exit_time: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    peak_profit_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    chop_at_entry: str = "-"  # "YES" / "no" / "?" at entry bar
    # Entry-bar indicator snapshot (for pattern analysis)
    stoch_k_at_entry: float = 0.0
    hist_at_entry: float = 0.0
    macd_delta_at_entry: float = 0.0
    ema9_dist_pct_at_entry: float = 0.0
    vwap_dist_pct_at_entry: float = 0.0
    vol_vs_avg20_at_entry: float = 0.0
    p3_used_momentum_override: bool = False
    p3_used_high_vwap_override: bool = False
    entry_source: str = "bar_close"  # bar_close | tick_intrabar
    # scale-out tracking
    qty_sold_pct: int = 0
    scales_hit: list[str] = field(default_factory=list)
    # realized weighted pnl across the full position (100%)
    pnl_pct: float = 0.0

    def result(self) -> str:
        return "WIN" if self.pnl_pct > 0 else "LOSS"

    def row(self) -> str:
        return (
            f"[{self.result():<4}] {self.symbol:<6} {self.entry_path:<11} "
            f"entry {self.entry_time} ${self.entry_price:.4f} "
            f"({self.entry_source}) -> "
            f"exit {self.exit_time} ${self.exit_price:.4f}  "
            f"chop_at_entry={self.chop_at_entry:<3} "
            f"reason={self.exit_reason:<14} peak={self.peak_profit_pct:+.2f}% "
            f"drawdown={self.max_drawdown_pct:+.2f}%  "
            f"scales={','.join(self.scales_hit) or '-':<13} PNL={self.pnl_pct:+.3f}%"
        )


def run_backtest(
    symbol: str,
    tick_file: Path,
    disabled_paths: frozenset[str] = frozenset(),
    *,
    replay_day: str | None = None,
    bar_interval_secs: int = 30,
    use_live_bar_recordings: bool = False,
    schwab_1m_mixed_feed: bool = False,
    replay_variant: str = "macd_30s",
    p3_min_score: int | None = None,
    p3_no_momentum_override: bool = False,
    p3_no_high_vwap_override: bool = False,
    p3_max_stoch_k: float | None = None,
    p3_max_ema9_dist_pct: float | None = None,
    p3_max_vwap_dist_pct: float | None = None,
    p3_min_vol_ratio: float | None = None,
    p1_min_score: int | None = None,
    p1_min_volume_abs: float | None = None,
    p1_min_bars_below_signal: int | None = None,
    p1_require_price_above_ema9: bool = False,
    p1_require_price_above_vwap: bool = False,
    p1_require_price_cross_above_vwap: bool = False,
    p1_require_ema9_trend_rising: bool = False,
    p1_require_macd_increasing: bool = False,
    p1_min_hist_value: float | None = None,
    p1_require_hist_positive: bool = False,
    p1_require_hist_growing: bool = False,
    p1_require_stoch_rising: bool = False,
    p4_body_pct: float | None = None,
    p4_range_pct: float | None = None,
    p4_close_top_pct: float | None = None,
    p4_vol_mult20: float | None = None,
    p4_breakout_lookback: int | None = None,
    p4_require_close_above_ema9: bool | None = None,
    p4_enabled: bool | None = None,
    disable_floor_exit: bool = False,
    macd_fast: int | None = None,
    macd_slow: int | None = None,
    macd_signal: int | None = None,
    ema1_len: int | None = None,
    ema2_len: int | None = None,
) -> tuple[list[Trade], dict[str, int]]:
    replay_variant_norm = replay_variant.strip().lower()
    if replay_variant_norm not in {"macd_30s", "schwab_1m"}:
        raise ValueError(f"unknown replay_variant: {replay_variant!r}")
    if schwab_1m_mixed_feed:
        if replay_variant_norm != "schwab_1m":
            raise ValueError("--schwab-1m-mixed-feed requires replay_variant schwab_1m")
        if use_live_bar_recordings:
            raise ValueError(
                "--schwab-1m-mixed-feed replays trades + native live_bar from JSONL; "
                "omit --use-live-bar-recordings"
            )

    base = TradingConfig()
    if replay_variant_norm == "schwab_1m":
        trading = base.make_1m_schwab_native_variant(quantity=10)
    else:
        trading = base.make_30s_schwab_native_variant(quantity=10)
        trading.bar_interval_secs = int(bar_interval_secs)
    if p3_min_score is not None:
        trading.p3_min_score = int(p3_min_score)
    if p3_no_momentum_override:
        trading.p3_allow_momentum_override = False
    if p3_no_high_vwap_override:
        trading.p3_allow_high_vwap = False
    if p4_body_pct is not None:
        trading.p4_body_pct = float(p4_body_pct)
    if p4_range_pct is not None:
        trading.p4_range_pct = float(p4_range_pct)
    if p4_close_top_pct is not None:
        trading.p4_close_top_pct = float(p4_close_top_pct)
    if p4_vol_mult20 is not None:
        trading.p4_vol_mult20 = float(p4_vol_mult20)
    if p4_breakout_lookback is not None:
        trading.p4_breakout_lookback = int(p4_breakout_lookback)
    if p4_require_close_above_ema9 is not None:
        trading.p4_require_close_above_ema9 = bool(p4_require_close_above_ema9)
    if p4_enabled is not None:
        trading.p4_enabled = bool(p4_enabled)
    indicator_cfg = IndicatorConfig()
    if macd_fast is not None:
        indicator_cfg.macd_fast = int(macd_fast)
    if macd_slow is not None:
        indicator_cfg.macd_slow = int(macd_slow)
    if macd_signal is not None:
        indicator_cfg.macd_signal = int(macd_signal)
    if ema1_len is not None:
        indicator_cfg.ema1_len = int(ema1_len)
    if ema2_len is not None:
        indicator_cfg.ema2_len = int(ema2_len)

    builder = SchwabNativeBarBuilder(
        ticker=symbol,
        interval_secs=int(trading.bar_interval_secs),
        time_provider=lambda: 0.0,
    )
    ind_engine = SchwabNativeIndicatorEngine(indicator_cfg)
    # Give the entry engine a now_provider that returns the current bar's ET time
    # so `_time_allowed` and trading-window gates evaluate against the historical bar,
    # not wall clock at backtest runtime.
    current_bar_et: list[datetime] = [datetime.now(EASTERN)]

    def bar_now_provider() -> datetime:
        return current_bar_et[0]

    entry_engine = SchwabNativeEntryEngine(
        trading, name="macd_30s-backtest", now_provider=bar_now_provider
    )

    trades: list[Trade] = []
    open_trade: Trade | None = None
    bars_closed = 0
    # Track why entries didn't fire
    block_counts: dict[str, int] = {
        "chop_active": 0,
        "chop_blocks_p1p2": 0,
        "chop_blocks_p3": 0,
        "warmup": 0,
        "other_blocked": 0,
        "idle_no_path": 0,
        "pending": 0,
        "signal": 0,
        # Pre-9:30 breakdown
        "pre930_bars_evaluated": 0,
        "pre930_warmup_blocks": 0,
        "pre930_other_blocks": 0,
        "pre930_idle": 0,
        "pre930_chop": 0,
        "pre930_signals": 0,
        # First-evaluation time
        "first_eval_time_et": 0,  # set to int HHMMSS below if we find one
        "first_evaluable_bar_et": 0,
        "first_pre930_tick_et": 0,
        "last_tick_et": 0,
    }

    # Exit config mirroring production (Pine defaults + TradingConfig fields)
    scale2 = float(trading.scale_normal2_pct)
    scale2_sell = int(trading.scale_normal2_sell_pct)
    fast4 = float(trading.scale_fast4_pct)
    fast4_sell = int(trading.scale_fast4_sell_pct)
    after2_4 = float(trading.scale_4after2_pct)
    after2_4_sell = int(trading.scale_4after2_sell_pct)
    # Pine tier thresholds; not in TradingConfig as of today
    tier2_t = 1.0
    tier3_t = 3.0
    floor1 = float(trading.profit_floor_lock_at_1pct_peak_pct)
    floor2 = float(trading.profit_floor_lock_at_2pct_peak_pct)
    floor3 = float(trading.profit_floor_lock_at_3pct_peak_pct)
    floor4 = 2.5  # Pine floor@4%
    trail_gap = float(trading.profit_floor_trail_buffer_over_4pct_pct)
    stoch_exit = float(indicator_cfg.stoch_exit_level)

    # Bar running state for floor system
    floor_pct = -999.0
    floor_active = False

    def _suppress_signal_via_filters(sig: dict | None, inds: dict) -> dict | None:
        """Apply disabled_paths + optional P3 hard caps (mirrors StrategyBot tightening)."""
        if sig is None:
            return None
        if str(sig.get("path", "")) in disabled_paths:
            entry_engine._last_buy_bar.pop(symbol, None)  # type: ignore[attr-defined]
            return None
        path_name = str(sig.get("path", ""))
        if path_name == "P1_CROSS":
            reject = False
            if p1_min_score is not None and int(sig.get("score", 0) or 0) < p1_min_score:
                reject = True
            if p1_min_volume_abs is not None and float(inds.get("volume", 0) or 0) < p1_min_volume_abs:
                reject = True
            if p1_min_bars_below_signal is not None and int(inds.get("bars_below_signal_prev", 0) or 0) < p1_min_bars_below_signal:
                reject = True
            if p1_require_price_above_ema9 and not bool(inds.get("price_above_ema9", False)):
                reject = True
            if p1_require_price_above_vwap and not bool(inds.get("price_above_vwap", False)):
                reject = True
            if p1_require_price_cross_above_vwap and not bool(inds.get("price_cross_above_vwap", False)):
                reject = True
            if p1_require_ema9_trend_rising and not bool(inds.get("ema9_trend_rising", False)):
                reject = True
            if p1_require_macd_increasing and not bool(inds.get("macd_increasing", False)):
                reject = True
            if p1_min_hist_value is not None and float(inds.get("hist_value", 0) or 0) < p1_min_hist_value:
                reject = True
            if p1_require_hist_positive and float(inds.get("hist_value", 0) or 0) <= 0:
                reject = True
            if p1_require_hist_growing and not bool(inds.get("hist_growing", False)):
                reject = True
            if p1_require_stoch_rising and not bool(inds.get("stoch_k_rising", False)):
                reject = True
            if reject:
                entry_engine._last_buy_bar.pop(symbol, None)  # type: ignore[attr-defined]
                return None
        if path_name == "P3_SURGE":
            sk = float(inds.get("stoch_k", 0) or 0)
            e9d = float(inds.get("ema9_dist_pct", 0) or 0)
            vwd = float(inds.get("vwap_dist_pct", 0) or 0)
            vol_now = float(inds.get("volume", 0) or 0)
            vol_avg = float(inds.get("vol_avg20", 0) or 0)
            vol_ratio = vol_now / vol_avg if vol_avg > 0 else 0.0
            in_session = bool(inds.get("in_regular_session", False))
            reject = False
            if p3_max_stoch_k is not None and sk >= p3_max_stoch_k:
                reject = True
            if p3_max_ema9_dist_pct is not None and e9d >= p3_max_ema9_dist_pct:
                reject = True
            if p3_max_vwap_dist_pct is not None and in_session and vwd >= p3_max_vwap_dist_pct:
                reject = True
            if p3_min_vol_ratio is not None and vol_ratio < p3_min_vol_ratio:
                reject = True
            if reject:
                entry_engine._last_buy_bar.pop(symbol, None)  # type: ignore[attr-defined]
                return None
        return sig

    def finalize_open_trade(
        *,
        sig: dict,
        inds: dict,
        chop_now: bool,
        entry_time_et: str,
        entry_px: float,
        entry_src: str,
        is_pre930: bool,
    ) -> None:
        nonlocal open_trade, floor_pct, floor_active
        block_counts["signal"] += 1
        if is_pre930:
            block_counts["pre930_signals"] += 1
        vwap_dist = float(inds.get("vwap_dist_pct", 0) or 0)
        ema9_dist = float(inds.get("ema9_dist_pct", 0) or 0)
        stoch_k_val = float(inds.get("stoch_k", 0) or 0)
        vol_now = float(inds.get("volume", 0) or 0)
        vol_avg20_now = float(inds.get("vol_avg20", 0) or 0)
        vol_ratio = vol_now / vol_avg20_now if vol_avg20_now > 0 else 0.0
        path_name = str(sig.get("path", ""))
        used_momentum_override = False
        used_high_vwap_override = False
        if path_name == "P3_SURGE":
            base_vwap_ok = (
                trading.vwap_max_dist_pct <= 0
                or not bool(inds.get("in_regular_session", False))
                or vwap_dist < trading.vwap_max_dist_pct
            )
            if not base_vwap_ok and stoch_k_val < trading.stoch_k_cap_level:
                used_high_vwap_override = True
            if stoch_k_val >= trading.stoch_k_cap_level:
                used_momentum_override = True
        open_trade = Trade(
            symbol=symbol,
            entry_time=entry_time_et,
            entry_price=entry_px,
            entry_path=path_name,
            entry_score=int(sig.get("score", 0) or 0),
            chop_at_entry="YES" if chop_now else "no",
            stoch_k_at_entry=stoch_k_val,
            hist_at_entry=float(inds.get("hist_value", 0) or 0),
            macd_delta_at_entry=float(inds.get("macd_delta", 0) or 0),
            ema9_dist_pct_at_entry=ema9_dist,
            vwap_dist_pct_at_entry=vwap_dist,
            vol_vs_avg20_at_entry=vol_ratio,
            p3_used_momentum_override=used_momentum_override,
            p3_used_high_vwap_override=used_high_vwap_override,
            entry_source=entry_src,
        )
        floor_pct = -999.0
        floor_active = False

    def try_aggregate_intrabar_from_trade(price: float, size: int, timestamp_ns_raw: int) -> None:
        """Match StrategyBotRuntime when use_live_aggregate_bars: ticks do NOT call builder.on_trade."""
        nonlocal open_trade
        if open_trade is not None:
            return
        if not bool(getattr(trading, "entry_intrabar_enabled", False)):
            return
        iv = float(trading.bar_interval_secs)
        tick_ts = (
            timestamp_ns_raw / 1e9
            if timestamp_ns_raw > 1_000_000_000_000_000_000
            else (
                timestamp_ns_raw / 1e3 if timestamp_ns_raw > 1_000_000_000_000 else float(timestamp_ns_raw)
            )
        )
        if tick_ts <= 0:
            return
        current_bar_et[0] = datetime.fromtimestamp(tick_ts, UTC).astimezone(EASTERN)
        bars_wb = builder.get_bars_with_current_as_dicts()
        if not bars_wb:
            return
        bucket_start = (tick_ts // iv) * iv
        adjusted = [dict(b) for b in bars_wb]
        cur_bar = adjusted[-1]
        cur_ts = float(cur_bar.get("timestamp", 0) or 0)
        tk_sz = max(0, int(size))

        if cur_ts <= 0 or bucket_start > cur_ts:
            last_close = float(cur_bar.get("close", price) or price)
            adjusted.append(
                {
                    "open": last_close,
                    "high": max(last_close, price),
                    "low": min(last_close, price),
                    "close": price,
                    "volume": tk_sz,
                    "timestamp": float(bucket_start),
                    "trade_count": 1,
                }
            )
        elif bucket_start == cur_ts:
            cur_bar["high"] = max(float(cur_bar.get("high", price) or price), price)
            cur_bar["low"] = min(float(cur_bar.get("low", price) or price), price)
            cur_bar["close"] = price
            cur_bar["volume"] = max(0, int(cur_bar.get("volume", 0) or 0)) + tk_sz
            cur_bar["trade_count"] = max(0, int(cur_bar.get("trade_count", 0) or 0)) + 1
        else:
            return

        indicators = ind_engine.calculate(adjusted)
        if indicators is None:
            return
        hm = current_bar_et[0].hour * 100 + current_bar_et[0].minute
        is_pre930_local = hm < 930
        chop_now = bool(getattr(entry_engine, "_chop_lock_active", {}).get(symbol, False))
        bare_idx = builder.get_bar_count() + 1
        sig = entry_engine.check_entry(symbol, indicators, bare_idx, position_tracker=None)
        decision = entry_engine.pop_last_decision(symbol) or {}
        chop_active_now = bool(getattr(entry_engine, "_chop_lock_active", {}).get(symbol, False))
        sig = _suppress_signal_via_filters(sig, indicators)

        if sig is not None:
            et_str = datetime.fromtimestamp(tick_ts, UTC).astimezone(EASTERN).strftime("%H:%M:%S")
            finalize_open_trade(
                sig=sig,
                inds=indicators,
                chop_now=chop_active_now,
                entry_time_et=et_str,
                entry_px=float(price),
                entry_src="tick_intrabar",
                is_pre930=is_pre930_local,
            )
            return

        _ = decision

    def process_bar(bar_obj) -> None:
        nonlocal open_trade, bars_closed, floor_pct, floor_active
        bars_closed += 1
        bars_history = builder.get_bars_as_dicts()
        indicators = ind_engine.calculate(bars_history)
        if indicators is None:
            # Record that a pre-9:30 bar tried but couldn't evaluate (warmup)
            if bars_history:
                last_bar = bars_history[-1]
                ts = float(last_bar.get("timestamp", 0))
                et = datetime.fromtimestamp(ts, UTC).astimezone(EASTERN)
                hm = et.hour * 100 + et.minute
                if hm < 930:
                    block_counts["pre930_warmup_blocks"] += 1
            return

        price = float(indicators["price"])
        bar_time = float(indicators["bar_timestamp"])

        # Log first evaluable bar
        et = datetime.fromtimestamp(bar_time, UTC).astimezone(EASTERN)
        hm = et.hour * 100 + et.minute
        if block_counts["first_evaluable_bar_et"] == 0:
            block_counts["first_evaluable_bar_et"] = et.hour * 10000 + et.minute * 100 + et.second
        is_pre930 = hm < 930
        if is_pre930:
            block_counts["pre930_bars_evaluated"] += 1

        # Pin the entry engine's "now" to this bar's ET time so time-window gates
        # use historical context, not wall clock.
        current_bar_et[0] = datetime.fromtimestamp(bar_time, UTC).astimezone(EASTERN)

        if open_trade is None:
            # Look for entry
            signal = entry_engine.check_entry(
                symbol, indicators, bars_closed, position_tracker=None
            )
            # Capture the decision that was just recorded so we can categorize it
            decision = entry_engine.pop_last_decision(symbol) or {}
            status = str(decision.get("status", ""))
            reason = str(decision.get("reason", "")).lower()
            # Sample chop state before entry decision via internal dict
            chop_active_now = bool(getattr(entry_engine, "_chop_lock_active", {}).get(symbol, False))

            signal = _suppress_signal_via_filters(signal, indicators)

            if signal is not None:
                finalize_open_trade(
                    sig=signal,
                    inds=indicators,
                    chop_now=chop_active_now,
                    entry_time_et=fmt_et(bar_time),
                    entry_px=price,
                    entry_src="bar_close",
                    is_pre930=is_pre930,
                )
            else:
                if status == "pending":
                    block_counts["pending"] += 1
                elif status == "idle":
                    block_counts["idle_no_path"] += 1
                    if is_pre930:
                        block_counts["pre930_idle"] += 1
                elif status == "blocked":
                    if "chop lock active" in reason:
                        block_counts["chop_active"] += 1
                        if is_pre930:
                            block_counts["pre930_chop"] += 1
                        if "p1/p2 gated" in reason and "p3 override active" in reason:
                            # P3 override is active; only P1/P2 blocked
                            block_counts["chop_blocks_p1p2"] += 1
                        elif "p1/p2/p3 gated" in reason:
                            block_counts["chop_blocks_p1p2"] += 1
                            block_counts["chop_blocks_p3"] += 1
                    elif "warmup" in reason:
                        block_counts["warmup"] += 1
                    else:
                        block_counts["other_blocked"] += 1
                        if is_pre930:
                            block_counts["pre930_other_blocks"] += 1
            return

        # Have open position — track exits
        entry_px = open_trade.entry_price
        profit_pct = (price - entry_px) / entry_px * 100.0 if entry_px > 0 else 0.0
        if profit_pct > open_trade.peak_profit_pct:
            open_trade.peak_profit_pct = profit_pct
        if profit_pct < open_trade.max_drawdown_pct:
            open_trade.max_drawdown_pct = profit_pct

        # Ratchet floor
        peak = open_trade.peak_profit_pct
        if peak >= 4.0:
            trail = peak - trail_gap
            floor_pct = max(floor_pct, max(trail, floor4))
            floor_active = True
        elif peak >= 3.0:
            floor_pct = max(floor_pct, floor3)
            floor_active = True
        elif peak >= 2.0:
            floor_pct = max(floor_pct, floor2)
            floor_active = True
        elif peak >= 1.0:
            floor_pct = max(floor_pct, floor1)
            floor_active = True

        # Scale exit milestones
        hit_fast4 = "FAST4" in open_trade.scales_hit
        hit_2pct = "2PCT" in open_trade.scales_hit
        hit_4after2 = "4AFTER2" in open_trade.scales_hit

        if not hit_2pct and not hit_fast4 and profit_pct >= fast4:
            # fast 4: skipping 2% entirely
            open_trade.scales_hit.append("FAST4")
            open_trade.qty_sold_pct += fast4_sell
            # realize partial pnl weighted
            open_trade.pnl_pct += profit_pct * fast4_sell / 100.0
            return
        if not hit_2pct and not hit_fast4 and profit_pct >= scale2:
            open_trade.scales_hit.append("2PCT")
            open_trade.qty_sold_pct += scale2_sell
            open_trade.pnl_pct += profit_pct * scale2_sell / 100.0
            return
        if hit_2pct and not hit_4after2 and profit_pct >= after2_4:
            open_trade.scales_hit.append("4AFTER2")
            open_trade.qty_sold_pct += after2_4_sell
            open_trade.pnl_pct += profit_pct * after2_4_sell / 100.0
            return

        # Exit conditions — close remaining qty
        remaining_pct = max(0, 100 - open_trade.qty_sold_pct)

        def close(reason: str) -> None:
            nonlocal open_trade, floor_pct, floor_active
            assert open_trade is not None
            open_trade.exit_time = fmt_et(bar_time)
            open_trade.exit_price = price
            open_trade.exit_reason = reason
            open_trade.pnl_pct += profit_pct * remaining_pct / 100.0
            trades.append(open_trade)
            open_trade = None
            floor_pct = -999.0
            floor_active = False
            # tell entry engine we exited for cooldown tracking
            entry_engine.record_exit(symbol, bars_closed)

        macd_cross_below = bool(indicators.get("macd_cross_below", False))
        stoch_k = float(indicators.get("stoch_k", 0) or 0)
        stoch_k_prev = float(indicators.get("stoch_k_prev", 0) or 0)
        close_below_ema9 = price < float(indicators.get("ema9", 0) or 0)

        # Floor breach: instant close
        if floor_active and not disable_floor_exit:
            floor_price = entry_px * (1.0 + floor_pct / 100.0)
            if price <= floor_price:
                close("FLOOR_BREACH")
                return

        # MACD cross below: exits all tiers
        if macd_cross_below:
            close("MACD_BEAR")
            return

        # Tier-based stoch exits
        tier = 1 if peak < tier2_t else (2 if peak < tier3_t else 3)
        stoch_falling = stoch_k < stoch_k_prev
        stoch_below = stoch_k < stoch_exit
        if tier == 1 and stoch_below and stoch_falling:
            close("STOCHK_T1")
            return
        if tier == 2 and stoch_below and stoch_falling and close_below_ema9:
            close("STOCHK_T2")
            return
        # tier 3: no stoch exit; only macd or floor

    if schwab_1m_mixed_feed:
        if int(trading.bar_interval_secs) != 60:
            raise ValueError("--schwab-1m-mixed-feed requires 60-second schwab_1m config")
        sortable: list[tuple[float, int, str, dict[str, object]]] = []
        with tick_file.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh):
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = str(payload.get("event_type", "trade")).lower()
                if ev == "trade":
                    try:
                        ts_ns = int(payload.get("timestamp_ns") or payload.get("recorded_at_ns") or 0)
                    except (TypeError, ValueError):
                        ts_ns = 0
                    sort_ts = float(_epoch_from_tick_ns(ts_ns))
                    sortable.append((sort_ts, lineno, "trade", payload))
                elif ev == "live_bar":
                    try:
                        p_iv = int(payload.get("interval_secs", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if p_iv != int(trading.bar_interval_secs):
                        continue
                    try:
                        ts = float(payload.get("timestamp", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if ts <= 0:
                        continue
                    sortable.append((ts, lineno, "live_bar", payload))
        sortable.sort(key=lambda x: (x[0], x[1]))

        if not sortable:
            print(
                f"\n[backtest:{symbol}] Mixed replay found no trade / live_bar lines in {tick_file} "
                f"(needs event_type trade + live_bar with interval_secs={int(trading.bar_interval_secs)})"
            )

        for _sort_ts, _lineno, kind, payload in sortable:
            if kind == "trade":
                price = float(payload.get("price", 0) or 0)
                size = int(payload.get("size", 0) or 0)
                try:
                    ts_ns_raw = int(payload.get("timestamp_ns") or payload.get("recorded_at_ns") or 0)
                except (TypeError, ValueError):
                    ts_ns_raw = 0
                if price <= 0 or ts_ns_raw <= 0:
                    continue
                try_aggregate_intrabar_from_trade(price, size, ts_ns_raw)
            else:
                try:
                    o = float(payload.get("open", 0) or 0)
                    h = float(payload.get("high", 0) or 0)
                    lo = float(payload.get("low", 0) or 0)
                    c = float(payload.get("close", 0) or 0)
                    vol = int(payload.get("volume", 0) or 0)
                    tc = int(payload.get("trade_count", 0) or 0)
                    ts = float(payload.get("timestamp", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if c <= 0 or ts <= 0:
                    continue
                completed_lb = builder.on_final_bar(
                    OHLCVBar(
                        open=o,
                        high=h,
                        low=lo,
                        close=c,
                        volume=vol,
                        timestamp=ts,
                        trade_count=max(0, tc),
                    )
                )
                for bar_obj in completed_lb:
                    process_bar(bar_obj)
    elif use_live_bar_recordings:
        if int(trading.bar_interval_secs) != 60:
            raise ValueError("--use-live-bar-recordings currently supports only 60-second replay")
        if not replay_day:
            raise ValueError("replay_day is required when using live bar recordings")
        recorded_bars = load_recorded_live_bars(
            tick_file.parent.parent,
            symbol=symbol,
            day=replay_day,
            interval_secs=int(trading.bar_interval_secs),
        )
        for recorded_bar in recorded_bars:
            completed = builder.on_final_bar(
                OHLCVBar(
                    open=recorded_bar.open,
                    high=recorded_bar.high,
                    low=recorded_bar.low,
                    close=recorded_bar.close,
                    volume=recorded_bar.volume,
                    timestamp=recorded_bar.timestamp,
                    trade_count=recorded_bar.trade_count,
                )
            )
            for bar in completed:
                process_bar(bar)
    else:
        # Stream ticks
        with tick_file.open("r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    tick = json.loads(line)
                except Exception:
                    continue
                ev_type = tick.get("event_type", "trade")
                if ev_type != "trade":
                    continue
                price = float(tick.get("price", 0) or 0)
                size = int(tick.get("size", 0) or 0)
                ts_ns = int(tick.get("timestamp_ns", 0) or 0)
                cum_vol = tick.get("cumulative_volume")
                # Feed live tick. Process any completed bars BEFORE the current tick.
                completed = builder.on_trade(price, size, ts_ns, cum_vol)
                for bar in completed:
                    process_bar(bar)

        # Force-close any remaining current bar at end
        completed = builder.check_bar_closes()
        for bar in completed:
            process_bar(bar)

    # Close any open position at last price
    if open_trade is not None:
        bars_history = builder.get_bars_as_dicts()
        indicators = ind_engine.calculate(bars_history)
        if indicators is not None:
            price = float(indicators["price"])
            bar_time = float(indicators["bar_timestamp"])
            profit_pct = (price - open_trade.entry_price) / open_trade.entry_price * 100.0
            remaining_pct = max(0, 100 - open_trade.qty_sold_pct)
            open_trade.exit_time = fmt_et(bar_time)
            open_trade.exit_price = price
            open_trade.exit_reason = "EOD"
            open_trade.pnl_pct += profit_pct * remaining_pct / 100.0
            trades.append(open_trade)

    return trades, block_counts


def _et_hms_from_history_cell(cell: str) -> str:
    """Normalize history export times (e.g. '11:09:45 AM ET') to HH:MM:SS."""
    s = str(cell).replace(" ET", "").strip()
    for fmt in ("%I:%M:%S %p", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time().strftime("%H:%M:%S")
        except ValueError:
            continue
    return ""


def compare_trades_to_closed_csv(*, replay: list[Trade], csv_path: Path, symbol: str) -> None:
    """Best-effort parity check vs engine export (path labels may use P1_MACD_CROSS vs P1_CROSS)."""
    if not csv_path.is_file():
        print(f"\n[compare] CSV not found: {csv_path}")
        return
    rows: list[dict[str, str]] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if str(row.get("ticker", "")).upper().strip() != symbol.upper().strip():
                continue
            rows.append(row)
    print(f"\n[compare:{symbol}] replay={len(replay)}  csv={len(rows)}  ({csv_path.name})")
    used: set[int] = set()
    for t in replay:
        rk = _canonical_path_key(t.entry_path)
        matched: str | None = None
        best_i = -1
        best_score = 1e9
        for i, row in enumerate(rows):
            if i in used:
                continue
            hk = _canonical_path_key(row.get("path"))
            if hk != rk:
                continue
            try:
                epub = float(str(row.get("entry_price", "")).strip() or "nan")
            except ValueError:
                epub = float("nan")
            pr_diff = abs(epub - float(t.entry_price))
            et_hist = _et_hms_from_history_cell(str(row.get("entry_time", "")))
            tm_diff = 1.0
            if et_hist and t.entry_time:
                tt = t.entry_time.split(":")
                hh = et_hist.split(":")
                if len(tt) >= 3 and len(hh) >= 3:
                    s_replay = int(tt[0]) * 3600 + int(tt[1]) * 60 + int(tt[2])
                    s_hist = int(hh[0]) * 3600 + int(hh[1]) * 60 + int(hh[2])
                    tm_diff = abs(s_replay - s_hist)
            score = pr_diff * 100.0 + tm_diff
            if score < best_score:
                best_score = score
                best_i = i
        if best_i >= 0 and best_score < 5.0:
            used.add(best_i)
            matched = "match"
        elif best_i >= 0:
            matched = f"weak(score={best_score:.2f})"
        else:
            matched = "no_csv_row"
        print(
            f"  {t.entry_time}  {t.entry_path}  ${t.entry_price:.4f}  "
            f"src={t.entry_source:<12}  CSV: {matched}"
        )
    for i, row in enumerate(rows):
        if i in used:
            continue
        print(
            f"  --- CSV only: {row.get('entry_time')}  {row.get('path')}  "
            f"${row.get('entry_price')}"
        )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None, help="single date YYYY-MM-DD (skips --all)")
    p.add_argument("--symbols", nargs="+", default=["ELPW", "AGPU"])
    p.add_argument("--all", action="store_true", help="sweep every date and every symbol found in --tick-root")
    p.add_argument("--min-file-bytes", type=int, default=20000, help="skip tick files smaller than this")
    p.add_argument(
        "--disable-paths",
        nargs="*",
        default=[],
        help="entry paths to disable, e.g. --disable-paths P3_SURGE",
    )
    # P3 tightening knobs
    p.add_argument("--p3-min-score", type=int, default=None, help="override p3_min_score")
    p.add_argument("--p3-no-momentum-override", action="store_true",
                   help="disable p3_allow_momentum_override (no stoch_k>90 bypass)")
    p.add_argument("--p3-no-high-vwap-override", action="store_true",
                   help="disable p3_allow_high_vwap (no vwap_dist>10%% bypass)")
    p.add_argument("--p3-max-stoch-k", type=float, default=None,
                   help="hard cap stoch_k at entry for P3 (overrides config); e.g. 80")
    p.add_argument("--p3-max-ema9-dist-pct", type=float, default=None,
                   help="hard cap ema9_dist_pct at entry for P3; e.g. 4.0")
    p.add_argument("--p3-max-vwap-dist-pct", type=float, default=None,
                   help="hard cap vwap_dist_pct at entry for P3 (in-session only); e.g. 8.0")
    p.add_argument("--p3-min-vol-ratio", type=float, default=None,
                   help="require volume >= this * vol_avg20 for P3; e.g. 1.5")
    p.add_argument("--p1-min-score", type=int, default=None,
                   help="require signal score >= this for P1_CROSS")
    p.add_argument("--p1-min-volume-abs", type=float, default=None,
                   help="require current bar volume >= this for P1_CROSS")
    p.add_argument("--p1-min-bars-below-signal", type=int, default=None,
                   help="require bars_below_signal_prev >= this for P1_CROSS")
    p.add_argument("--p1-require-price-above-ema9", action="store_true",
                   help="require price_above_ema9 for P1_CROSS")
    p.add_argument("--p1-require-price-above-vwap", action="store_true",
                   help="require price_above_vwap for P1_CROSS")
    p.add_argument("--p1-require-price-cross-above-vwap", action="store_true",
                   help="require price_cross_above_vwap for P1_CROSS")
    p.add_argument("--p1-require-ema9-trend-rising", action="store_true",
                   help="require ema9_trend_rising for P1_CROSS")
    p.add_argument("--p1-require-macd-increasing", action="store_true",
                   help="require macd_increasing for P1_CROSS")
    p.add_argument("--p1-min-hist-value", type=float, default=None,
                   help="require hist_value >= this absolute floor for P1_CROSS")
    p.add_argument("--p1-require-hist-positive", action="store_true",
                   help="require hist_value > 0 for P1_CROSS")
    p.add_argument("--p1-require-hist-growing", action="store_true",
                   help="require hist_growing for P1_CROSS")
    p.add_argument("--p1-require-stoch-rising", action="store_true",
                   help="require stoch_k_rising for P1_CROSS")
    p.add_argument("--p4-body-pct", type=float, default=None, help="override P4 body percent threshold")
    p.add_argument("--p4-range-pct", type=float, default=None, help="override P4 range percent threshold")
    p.add_argument("--p4-close-top-pct", type=float, default=None,
                   help="override P4 close-near-high threshold (smaller = stricter)")
    p.add_argument("--p4-vol-mult20", type=float, default=None,
                   help="override P4 volume >= vol_avg20 * this multiplier")
    p.add_argument("--p4-breakout-lookback", type=int, default=None,
                   help="override P4 breakout lookback bars")
    p.add_argument("--p4-no-close-above-ema9", action="store_true",
                   help="disable the P4 close-above-EMA9 requirement")
    p.add_argument("--macd-fast", type=int, default=None, help="override MACD fast length")
    p.add_argument("--macd-slow", type=int, default=None, help="override MACD slow length")
    p.add_argument("--macd-signal", type=int, default=None, help="override MACD signal length")
    p.add_argument("--ema1-len", type=int, default=None, help="override fast EMA length")
    p.add_argument("--ema2-len", type=int, default=None, help="override slow EMA length")
    p.add_argument(
        "--disable-floor-exit",
        action="store_true",
        help="Disable FLOOR_BREACH exits while keeping all other exits unchanged.",
    )
    p.add_argument(
        "--tick-root",
        default="/var/lib/project-mai-tai/schwab_ticks",
        help="Directory containing YYYY-MM-DD/SYMBOL.jsonl tick recordings",
    )
    p.add_argument(
        "--bar-interval-secs",
        type=int,
        default=30,
        metavar="N",
        help="Bucket size for OHLC bars from ticks (30 = production; 60 = 1-minute simulation)",
    )
    p.add_argument(
        "--one-minute",
        action="store_true",
        help="Same as --bar-interval-secs 60",
    )
    p.add_argument(
        "--use-live-bar-recordings",
        action="store_true",
        help="For 1-minute replay, use stored native Schwab live bars instead of rebuilding from trades",
    )
    p.add_argument(
        "--replay-variant",
        choices=("macd_30s", "schwab_1m"),
        default="macd_30s",
        help="TradingConfig factory: schwab_1m uses make_1m_schwab_native_variant",
    )
    p.add_argument(
        "--schwab-1m-mixed-feed",
        action="store_true",
        help="schwab_1m: merge trade + live_bar lines from JSONL (intrabar ticks, then native minute final)",
    )
    p.add_argument(
        "--compare-closed-csv",
        default=None,
        metavar="PATH",
        help="After each symbol replay, fuzzy-compare entries to a macdbot / engine closed-trade CSV export",
    )
    args = p.parse_args()
    if args.schwab_1m_mixed_feed and args.use_live_bar_recordings:
        print("\n[backtest] Choose either --schwab-1m-mixed-feed or --use-live-bar-recordings, not both.")
        return

    bar_interval = (
        60 if args.one_minute or args.replay_variant == "schwab_1m" else max(1, int(args.bar_interval_secs))
    )
    if args.use_live_bar_recordings and bar_interval != 60:
        print("\n[backtest] --use-live-bar-recordings currently requires --one-minute / 60-second bars.")
        return

    tick_root = Path(args.tick_root)
    if not tick_root.is_dir():
        print(
            f"\n[backtest] Tick root is not a directory: {tick_root}\n"
            "  On the VPS, recordings usually live at "
            "/var/lib/project-mai-tai/schwab_ticks/YYYY-MM-DD/*.jsonl\n"
            "  Locally, use --tick-root with a path that contains per-day folders."
        )
        return

    # Build list of (date, symbol, path) combos
    combos: list[tuple[str, str, Path]] = []
    if args.all:
        for date_dir in sorted(tick_root.iterdir()):
            if not date_dir.is_dir():
                continue
            for tick_file in sorted(date_dir.glob("*.jsonl")):
                if tick_file.name.startswith("__"):
                    continue
                if tick_file.stat().st_size < args.min_file_bytes:
                    continue
                symbol = tick_file.stem
                combos.append((date_dir.name, symbol, tick_file))
    else:
        date = args.date or "2026-04-22"
        date_dir = tick_root / date
        for sym in args.symbols:
            path = date_dir / f"{sym}.jsonl"
            if not path.exists():
                print(f"[skip] {sym} — no tick file at {path}")
                continue
            combos.append((date, sym, path))

    disabled = frozenset(s.strip().upper() for s in args.disable_paths if s.strip())
    if disabled:
        print(f"\n[config] disabled paths: {sorted(disabled)}")

    if not combos:
        print(
            f"\n[backtest] No tick files found under {tick_root!s} "
            f"(date={getattr(args, 'date', None)!r}, --all={args.all}). "
            f"Copy schwab_ticks/YYYY-MM-DD/*.jsonl from the VPS or set --tick-root."
        )
        return

    src = (
        "native_live_bars"
        if args.use_live_bar_recordings
        else ("schwab_1m_mixed" if args.schwab_1m_mixed_feed else "trade_ticks")
    )
    print(
        f"\n[backtest] variant={args.replay_variant}  "
        f"bar_interval_secs={bar_interval} "
        f"({'1-minute' if bar_interval == 60 else f'{bar_interval}s'})  tick_root={tick_root}"
        f"  source={src}"
        f"  macd={args.macd_fast or 12}/{args.macd_slow or 26}/{args.macd_signal or 9}"
        f"  ema={args.ema1_len or 9}/{args.ema2_len or 20}"
    )

    all_trades: list[tuple[str, Trade]] = []  # (date, trade)
    all_blocks: dict[str, int] = {}
    for date, sym, path in combos:
        trades, blocks = run_backtest(
            sym,
            path,
            disabled_paths=disabled,
            replay_day=date,
            bar_interval_secs=bar_interval,
            use_live_bar_recordings=args.use_live_bar_recordings,
            schwab_1m_mixed_feed=args.schwab_1m_mixed_feed,
            replay_variant=args.replay_variant,
            p3_min_score=args.p3_min_score,
            p3_no_momentum_override=args.p3_no_momentum_override,
            p3_no_high_vwap_override=args.p3_no_high_vwap_override,
            p3_max_stoch_k=args.p3_max_stoch_k,
            p3_max_ema9_dist_pct=args.p3_max_ema9_dist_pct,
            p3_max_vwap_dist_pct=args.p3_max_vwap_dist_pct,
            p3_min_vol_ratio=args.p3_min_vol_ratio,
            p1_min_score=args.p1_min_score,
            p1_min_volume_abs=args.p1_min_volume_abs,
            p1_min_bars_below_signal=args.p1_min_bars_below_signal,
            p1_require_price_above_ema9=args.p1_require_price_above_ema9,
            p1_require_price_above_vwap=args.p1_require_price_above_vwap,
            p1_require_price_cross_above_vwap=args.p1_require_price_cross_above_vwap,
            p1_require_ema9_trend_rising=args.p1_require_ema9_trend_rising,
            p1_require_macd_increasing=args.p1_require_macd_increasing,
            p1_min_hist_value=args.p1_min_hist_value,
            p1_require_hist_positive=args.p1_require_hist_positive,
            p1_require_hist_growing=args.p1_require_hist_growing,
            p1_require_stoch_rising=args.p1_require_stoch_rising,
            p4_body_pct=args.p4_body_pct,
            p4_range_pct=args.p4_range_pct,
            p4_close_top_pct=args.p4_close_top_pct,
            p4_vol_mult20=args.p4_vol_mult20,
            p4_breakout_lookback=args.p4_breakout_lookback,
            p4_require_close_above_ema9=False if args.p4_no_close_above_ema9 else None,
            disable_floor_exit=args.disable_floor_exit,
            macd_fast=args.macd_fast,
            macd_slow=args.macd_slow,
            macd_signal=args.macd_signal,
            ema1_len=args.ema1_len,
            ema2_len=args.ema2_len,
        )
        print(f"\n=== {date}  {sym}  ({path.stat().st_size // 1024}KB, {len(trades)} trades) ===")
        if not trades:
            print("  no trades")
        for t in trades:
            print(f"  {t.row()}")
        if args.compare_closed_csv:
            compare_trades_to_closed_csv(
                replay=trades,
                csv_path=Path(args.compare_closed_csv),
                symbol=sym,
            )
        chop_n = blocks.get("chop_active", 0)
        warmup_n = blocks.get("warmup", 0)
        first_eval = blocks.get("first_evaluable_bar_et", 0)
        pre930 = blocks.get("pre930_bars_evaluated", 0)
        pre930_warm = blocks.get("pre930_warmup_blocks", 0)
        pre930_sig = blocks.get("pre930_signals", 0)
        pre930_chop = blocks.get("pre930_chop", 0)
        pre930_idle = blocks.get("pre930_idle", 0)
        pre930_other = blocks.get("pre930_other_blocks", 0)
        if chop_n or warmup_n or pre930 or pre930_warm:
            h = first_eval // 10000
            m = (first_eval // 100) % 100
            s = first_eval % 100
            print(
                f"  [bars] first_evaluable_bar={h:02d}:{m:02d}:{s:02d}  pre930_warmup_bars={pre930_warm}  "
                f"pre930_evaluated={pre930}  pre930_signals={pre930_sig}  "
                f"pre930_chop_blocks={pre930_chop}  pre930_idle={pre930_idle}  pre930_other_blocked={pre930_other}"
            )
            print(
                f"  [bar-level blocks] chop_active={chop_n} (chop_blocks_p1p2={blocks.get('chop_blocks_p1p2', 0)}, "
                f"chop_blocks_p3={blocks.get('chop_blocks_p3', 0)})  warmup_bars={warmup_n}  "
                f"pending={blocks.get('pending', 0)}  idle={blocks.get('idle_no_path', 0)}  "
                f"other_blocked={blocks.get('other_blocked', 0)}"
            )
        for t in trades:
            all_trades.append((date, t))
        for k, v in blocks.items():
            all_blocks[k] = all_blocks.get(k, 0) + v

    if not all_trades:
        return

    print("\n\n==================== OVERALL SUMMARY ====================")
    total_pnl = sum(t.pnl_pct for _, t in all_trades)
    winners = [t for _, t in all_trades if t.pnl_pct > 0]
    losers = [t for _, t in all_trades if t.pnl_pct <= 0]
    print(
        f"total trades={len(all_trades)}  winners={len(winners)}  losers={len(losers)}  "
        f"net_pnl={total_pnl:+.3f}%"
    )

    # Per-path scoreboard
    from collections import defaultdict
    by_path: dict[str, list[Trade]] = defaultdict(list)
    for _, t in all_trades:
        by_path[t.entry_path].append(t)

    print("\n--- BY ENTRY PATH ---")
    print(f"{'path':<12} {'trades':>6} {'wins':>5} {'losses':>6} {'winrate':>8} "
          f"{'avg_pnl':>9} {'net_pnl':>9} {'best':>7} {'worst':>7}")
    for path in sorted(by_path.keys()):
        ts = by_path[path]
        n = len(ts)
        wins = [x for x in ts if x.pnl_pct > 0]
        losses = [x for x in ts if x.pnl_pct <= 0]
        net = sum(x.pnl_pct for x in ts)
        avg = net / n if n else 0.0
        winrate = (len(wins) / n * 100.0) if n else 0.0
        best = max((x.pnl_pct for x in ts), default=0.0)
        worst = min((x.pnl_pct for x in ts), default=0.0)
        print(
            f"{path:<12} {n:>6} {len(wins):>5} {len(losses):>6} {winrate:>7.1f}% "
            f"{avg:>+8.3f}% {net:>+8.3f}% {best:>+6.2f}% {worst:>+6.2f}%"
        )

    # Per-date summary
    by_date: dict[str, list[Trade]] = defaultdict(list)
    for d, t in all_trades:
        by_date[d].append(t)
    print("\n--- BY DATE ---")
    print(f"{'date':<12} {'trades':>6} {'wins':>5} {'losses':>6} {'winrate':>8} {'net_pnl':>9}")
    for d in sorted(by_date.keys()):
        ts = by_date[d]
        n = len(ts)
        wins = len([x for x in ts if x.pnl_pct > 0])
        losses = n - wins
        net = sum(x.pnl_pct for x in ts)
        winrate = wins / n * 100.0 if n else 0.0
        print(f"{d:<12} {n:>6} {wins:>5} {losses:>6} {winrate:>7.1f}% {net:>+8.3f}%")

    # Detailed P3 entry report so we can see what distinguishes winners from losers
    p3_trades = [(d, t) for d, t in all_trades if t.entry_path == "P3_SURGE"]
    if p3_trades:
        print("\n--- P3_SURGE entry fingerprint (sorted by date/time) ---")
        print(
            f"{'date':<12} {'sym':<6} {'entry_et':<9} {'score':>5} {'stochK':>6} "
            f"{'hist':>7} {'mDelta':>8} {'e9dist%':>8} {'vwap%':>7} {'vol/avg':>8} "
            f"{'override':<10} {'pnl%':>8}"
        )
        for d, t in sorted(p3_trades, key=lambda x: (x[0], x[1].entry_time)):
            override = (
                "MOMENTUM" if t.p3_used_momentum_override
                else ("HIGH_VWAP" if t.p3_used_high_vwap_override else "—")
            )
            tag = "WIN" if t.pnl_pct > 0 else "LOSS"
            print(
                f"{d:<12} {t.symbol:<6} {t.entry_time:<9} {t.entry_score:>5} "
                f"{t.stoch_k_at_entry:>6.1f} {t.hist_at_entry:>7.4f} "
                f"{t.macd_delta_at_entry:>8.4f} {t.ema9_dist_pct_at_entry:>7.2f} "
                f"{t.vwap_dist_pct_at_entry:>6.2f} {t.vol_vs_avg20_at_entry:>7.2f} "
                f"{override:<10} [{tag}] {t.pnl_pct:>+6.2f}"
            )

    # Chop stats overall
    print("\n--- CHOP RULE ACTIVITY ---")
    print(f"chop_active_bars={all_blocks.get('chop_active', 0)}  "
          f"of_which_blocks_p1p2={all_blocks.get('chop_blocks_p1p2', 0)}  "
          f"blocks_p3={all_blocks.get('chop_blocks_p3', 0)}")
    print(f"warmup_bars={all_blocks.get('warmup', 0)}  "
          f"pending_bars={all_blocks.get('pending', 0)}  "
          f"idle_bars={all_blocks.get('idle_no_path', 0)}  "
          f"other_blocked_bars={all_blocks.get('other_blocked', 0)}  "
          f"signal_bars={all_blocks.get('signal', 0)}")

    # What-if: drop a path
    print("\n--- WHAT-IF: net P&L if we disable each path ---")
    print(f"{'without':<12} {'kept trades':>12} {'net_pnl':>9} {'delta vs baseline':>18}")
    baseline = total_pnl
    for path in sorted(by_path.keys()):
        kept = [t for _, t in all_trades if t.entry_path != path]
        net = sum(x.pnl_pct for x in kept)
        delta = net - baseline
        print(f"{path:<12} {len(kept):>12} {net:>+8.3f}% {delta:>+17.3f}%")


if __name__ == "__main__":
    main()
