from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from project_mai_tai.db.models import Strategy, StrategyBarHistory
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ

try:
    import redis
except Exception:  # pragma: no cover - optional at import time
    redis = None


@dataclass
class BarSample:
    symbol: str
    bar_time: datetime
    decision_status: str
    decision_reason: str
    decision_path: str
    decision_score: str
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: int
    indicators: dict[str, object]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live-day reclaim what-if simulations.")
    parser.add_argument("--db-url", default=os.getenv("MAI_TAI_DATABASE_URL", ""))
    parser.add_argument("--redis-url", default=os.getenv("MAI_TAI_REDIS_URL", ""))
    parser.add_argument("--strategy", default="macd_30s_reclaim")
    parser.add_argument("--date", required=True, help="ET date in YYYY-MM-DD format")
    parser.add_argument("--symbols", nargs="*", default=None, help="Optional symbol filter")
    parser.add_argument("--lookahead-bars", type=int, default=10)
    parser.add_argument("--target1-pct", type=float, default=1.0)
    parser.add_argument("--target2-pct", type=float, default=2.0)
    parser.add_argument("--stop-pct", type=float, default=1.0)
    parser.add_argument("--output", type=Path, default=Path("tmp_replay/reclaim_live_day_whatif.json"))
    return parser.parse_args()


def _et_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0), tzinfo=EASTERN_TZ)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _load_bars(
    session: Session,
    *,
    strategy_code: str,
    start_utc: datetime,
    end_utc: datetime,
    symbols: set[str] | None,
) -> list[BarSample]:
    stmt = (
        select(
            StrategyBarHistory.symbol,
            StrategyBarHistory.bar_time,
            StrategyBarHistory.decision_status,
            StrategyBarHistory.decision_reason,
            StrategyBarHistory.decision_path,
            StrategyBarHistory.decision_score,
            StrategyBarHistory.open_price,
            StrategyBarHistory.high_price,
            StrategyBarHistory.low_price,
            StrategyBarHistory.close_price,
            StrategyBarHistory.volume,
            StrategyBarHistory.indicators_json,
        )
        .where(
            StrategyBarHistory.strategy_code == strategy_code,
            StrategyBarHistory.bar_time >= start_utc,
            StrategyBarHistory.bar_time < end_utc,
        )
        .order_by(StrategyBarHistory.symbol.asc(), StrategyBarHistory.bar_time.asc())
    )
    rows = session.execute(stmt).all()
    results: list[BarSample] = []
    for row in rows:
        symbol = row.symbol
        if symbols and symbol not in symbols:
            continue
        results.append(
            BarSample(
                symbol=symbol,
                bar_time=row.bar_time,
                decision_status=row.decision_status or "",
                decision_reason=row.decision_reason or "",
                decision_path=row.decision_path or "",
                decision_score=row.decision_score or "",
                open_price=float(row.open_price),
                high_price=float(row.high_price),
                low_price=float(row.low_price),
                close_price=float(row.close_price),
                volume=int(row.volume),
                indicators=dict(row.indicators_json or {}),
            )
        )
    return results


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _selected_vwap(indicators: dict[str, object]) -> float:
    for key in ("decision_vwap", "selected_vwap", "extended_vwap", "vwap"):
        value = _safe_float(indicators.get(key))
        if value and value > 0:
            return value
    return 0.0


def _simplify_reason(reason: str) -> str:
    if not reason:
        return "other"
    if reason.startswith("pretrigger reclaim below VWAP and EMA9 support"):
        return "below_vwap_and_ema9"
    if reason.startswith("pretrigger reclaim below VWAP"):
        return "below_vwap"
    if reason.startswith("pretrigger reclaim below EMA9 support"):
        return "below_ema9"
    if reason.startswith("pretrigger reclaim pullback too shallow"):
        return "pullback_too_shallow"
    if reason.startswith("pretrigger reclaim trend weak"):
        return "trend_weak"
    if reason.startswith("pretrigger reclaim momentum weak"):
        return "momentum_weak"
    if reason.startswith("pretrigger reclaim too extended"):
        return "too_extended"
    if reason.startswith("pretrigger reclaim recovery candle"):
        return "recovery_candle"
    if reason.startswith("pretrigger reclaim score "):
        return "score_below"
    if reason.startswith("cooldown"):
        return "cooldown"
    if " paused (" in reason:
        return "ticker_pause"
    if reason.startswith("already in position"):
        return "already_in_position"
    return "other"


PULLBACK_RE = re.compile(r"([0-9.]+)% < ([0-9.]+)%")


def _pullback_pct_from_reason(reason: str) -> float | None:
    match = PULLBACK_RE.search(reason)
    if not match:
        return None
    return float(match.group(1))


def _forward_outcome(
    bars: list[BarSample],
    index: int,
    *,
    lookahead_bars: int,
    target1_pct: float,
    target2_pct: float,
    stop_pct: float,
) -> dict[str, object]:
    current = bars[index]
    entry = current.close_price
    future = bars[index + 1 : index + 1 + lookahead_bars]
    target1 = entry * (1.0 + target1_pct / 100.0)
    target2 = entry * (1.0 + target2_pct / 100.0)
    stop = entry * (1.0 - stop_pct / 100.0)
    hit_target1 = None
    hit_target2 = None
    hit_stop = None
    for offset, future_bar in enumerate(future, start=1):
        if hit_target1 is None and future_bar.high_price >= target1:
            hit_target1 = offset
        if hit_target2 is None and future_bar.high_price >= target2:
            hit_target2 = offset
        if hit_stop is None and future_bar.low_price <= stop:
            hit_stop = offset
    stop_first = hit_stop is not None and (hit_target1 is None or hit_stop < hit_target1)
    return {
        "hit_target1": hit_target1 is not None,
        "hit_target2": hit_target2 is not None,
        "stop_first": stop_first,
    }


def _scenario_pause_off(bar: BarSample) -> bool:
    return _simplify_reason(bar.decision_reason) == "ticker_pause"


def _scenario_pullback_025(bar: BarSample) -> bool:
    return _simplify_reason(bar.decision_reason) == "pullback_too_shallow" and (_pullback_pct_from_reason(bar.decision_reason) or -1.0) >= 0.25


def _scenario_pullback_010(bar: BarSample) -> bool:
    return _simplify_reason(bar.decision_reason) == "pullback_too_shallow" and (_pullback_pct_from_reason(bar.decision_reason) or -1.0) >= 0.10


def _scenario_soft_location(bar: BarSample) -> bool:
    category = _simplify_reason(bar.decision_reason)
    if category not in {"below_vwap", "below_ema9", "below_vwap_and_ema9"}:
        return False
    price = bar.close_price
    ema9 = _safe_float(bar.indicators.get("ema9")) or 0.0
    vwap = _selected_vwap(bar.indicators)
    if ema9 <= 0 or vwap <= 0:
        return False
    ema_gap = (ema9 - price) / ema9
    vwap_gap = (vwap - price) / vwap
    bullish_recovery = price > bar.open_price
    if category == "below_vwap":
        return price >= ema9 and 0.0 <= vwap_gap <= 0.010
    if category == "below_ema9":
        return price >= vwap and 0.0 <= ema_gap <= 0.003
    return bullish_recovery and 0.0 <= ema_gap <= 0.003 and 0.0 <= vwap_gap <= 0.010


def _bar_recovery_shape(bar: BarSample) -> tuple[float, float, float]:
    bar_range = max(bar.high_price - bar.low_price, 0.000001)
    body_pct = abs(bar.close_price - bar.open_price) / bar_range
    close_pos_pct = (bar.close_price - bar.low_price) / bar_range
    upper_wick_pct = (bar.high_price - max(bar.open_price, bar.close_price)) / bar_range
    return body_pct, close_pos_pct, upper_wick_pct


def _scenario_dual_anchor_recovery(bar: BarSample) -> bool:
    category = _simplify_reason(bar.decision_reason)
    if category not in {"below_vwap", "below_ema9", "below_vwap_and_ema9"}:
        return False
    price = bar.close_price
    current_low = bar.low_price
    ema9 = _safe_float(bar.indicators.get("ema9")) or 0.0
    vwap = _selected_vwap(bar.indicators)
    current_bar_rel_vol = _safe_float(bar.indicators.get("current_bar_rel_vol")) or 0.0
    if ema9 <= 0 or vwap <= 0:
        return False
    body_pct, close_pos_pct, upper_wick_pct = _bar_recovery_shape(bar)
    bullish_recovery = price > bar.open_price
    near_ema9 = price >= ema9 * (1.0 - 0.003)
    near_vwap = price >= vwap * (1.0 - 0.0125)
    touched_anchor = current_low <= max(ema9 * 1.01, vwap * 1.01)
    return (
        bullish_recovery
        and touched_anchor
        and near_ema9
        and near_vwap
        and current_bar_rel_vol >= 0.8
        and body_pct >= 0.20
        and close_pos_pct >= 0.55
        and upper_wick_pct <= 0.35
    )


def _scenario_dual_anchor_recovery_loose(bar: BarSample) -> bool:
    category = _simplify_reason(bar.decision_reason)
    if category not in {"below_vwap_and_ema9", "below_ema9"}:
        return False
    price = bar.close_price
    current_low = bar.low_price
    ema9 = _safe_float(bar.indicators.get("ema9")) or 0.0
    vwap = _selected_vwap(bar.indicators)
    if ema9 <= 0 or vwap <= 0:
        return False
    body_pct, close_pos_pct, upper_wick_pct = _bar_recovery_shape(bar)
    bullish_recovery = price > bar.open_price
    near_ema9 = price >= ema9 * (1.0 - 0.005)
    near_vwap = price >= vwap * (1.0 - 0.015)
    touched_anchor = current_low <= max(ema9 * 1.01, vwap * 1.01)
    return (
        bullish_recovery
        and touched_anchor
        and near_ema9
        and near_vwap
        and body_pct >= 0.15
        and close_pos_pct >= 0.50
        and upper_wick_pct <= 0.45
    )


def _scenario_dual_anchor_recovery_plus_pause(bar: BarSample) -> bool:
    return _scenario_pause_off(bar) or _scenario_dual_anchor_recovery(bar)


def _scenario_pullback_025_plus_dual_anchor_recovery(bar: BarSample) -> bool:
    return _scenario_pullback_025(bar) or _scenario_dual_anchor_recovery(bar)


def _scenario_dual_anchor_recovery_loose_plus_pause(bar: BarSample) -> bool:
    return _scenario_pause_off(bar) or _scenario_dual_anchor_recovery_loose(bar)


def _scenario_below_vwap_and_ema9(bar: BarSample) -> bool:
    return _simplify_reason(bar.decision_reason) == "below_vwap_and_ema9"


def _scenario_below_ema9(bar: BarSample) -> bool:
    return _simplify_reason(bar.decision_reason) == "below_ema9"


def _scenario_soft_location_plus_pause(bar: BarSample) -> bool:
    return _scenario_pause_off(bar) or _scenario_soft_location(bar)


def _scenario_pause_plus_pullback_025(bar: BarSample) -> bool:
    return _scenario_pause_off(bar) or _scenario_pullback_025(bar)


def _scenario_pause_plus_pullback_010(bar: BarSample) -> bool:
    return _scenario_pause_off(bar) or _scenario_pullback_010(bar)


def _scenario_below_vwap_and_ema9_plus_pause(bar: BarSample) -> bool:
    return _scenario_pause_off(bar) or _scenario_below_vwap_and_ema9(bar)


def _scenario_below_ema9_plus_pause(bar: BarSample) -> bool:
    return _scenario_pause_off(bar) or _scenario_below_ema9(bar)


SCENARIOS: dict[str, Callable[[BarSample], bool]] = {
    "pause_off": _scenario_pause_off,
    "pullback_025": _scenario_pullback_025,
    "pullback_010": _scenario_pullback_010,
    "pause_plus_pullback_025": _scenario_pause_plus_pullback_025,
    "pause_plus_pullback_010": _scenario_pause_plus_pullback_010,
    "soft_location": _scenario_soft_location,
    "soft_location_plus_pause": _scenario_soft_location_plus_pause,
    "dual_anchor_recovery": _scenario_dual_anchor_recovery,
    "dual_anchor_recovery_plus_pause": _scenario_dual_anchor_recovery_plus_pause,
    "dual_anchor_recovery_loose": _scenario_dual_anchor_recovery_loose,
    "dual_anchor_recovery_loose_plus_pause": _scenario_dual_anchor_recovery_loose_plus_pause,
    "pullback_025_plus_dual_anchor_recovery": _scenario_pullback_025_plus_dual_anchor_recovery,
    "below_vwap_and_ema9": _scenario_below_vwap_and_ema9,
    "below_ema9": _scenario_below_ema9,
    "below_vwap_and_ema9_plus_pause": _scenario_below_vwap_and_ema9_plus_pause,
    "below_ema9_plus_pause": _scenario_below_ema9_plus_pause,
}


def _analyze_blockers(
    bars_by_symbol: dict[str, list[BarSample]],
    *,
    lookahead_bars: int,
    target1_pct: float,
    target2_pct: float,
    stop_pct: float,
) -> tuple[list[dict[str, object]], dict[str, list[dict[str, object]]]]:
    overall: dict[str, Counter[str]] = defaultdict(Counter)
    by_symbol: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))

    for symbol, bars in bars_by_symbol.items():
        for index, bar in enumerate(bars):
            if bar.decision_status != "blocked":
                continue
            category = _simplify_reason(bar.decision_reason)
            outcome = _forward_outcome(
                bars,
                index,
                lookahead_bars=lookahead_bars,
                target1_pct=target1_pct,
                target2_pct=target2_pct,
                stop_pct=stop_pct,
            )
            overall[category]["count"] += 1
            by_symbol[symbol][category]["count"] += 1
            if outcome["hit_target1"]:
                overall[category]["pop1"] += 1
                by_symbol[symbol][category]["pop1"] += 1
            if outcome["hit_target2"]:
                overall[category]["pop2"] += 1
                by_symbol[symbol][category]["pop2"] += 1
            if outcome["stop_first"]:
                overall[category]["stop_first"] += 1
                by_symbol[symbol][category]["stop_first"] += 1

    def _rows(source: dict[str, Counter[str]]) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for category, counts in source.items():
            total = int(counts["count"])
            if total <= 0:
                continue
            rows.append(
                {
                    "category": category,
                    "count": total,
                    "pop1_rate": round(counts["pop1"] / total, 3),
                    "pop2_rate": round(counts["pop2"] / total, 3),
                    "stop_first_rate": round(counts["stop_first"] / total, 3),
                }
            )
        rows.sort(key=lambda item: (-int(item["count"]), str(item["category"])))
        return rows

    symbol_rows = {symbol: _rows(source) for symbol, source in by_symbol.items()}
    return _rows(overall), symbol_rows


def _analyze_scenarios(
    bars_by_symbol: dict[str, list[BarSample]],
    *,
    lookahead_bars: int,
    target1_pct: float,
    target2_pct: float,
    stop_pct: float,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for name, predicate in SCENARIOS.items():
        counts: Counter[str] = Counter()
        by_symbol: Counter[str] = Counter()
        for symbol, bars in bars_by_symbol.items():
            for index, bar in enumerate(bars):
                if bar.decision_status != "blocked" or not predicate(bar):
                    continue
                counts["candidate_bars"] += 1
                by_symbol[symbol] += 1
                outcome = _forward_outcome(
                    bars,
                    index,
                    lookahead_bars=lookahead_bars,
                    target1_pct=target1_pct,
                    target2_pct=target2_pct,
                    stop_pct=stop_pct,
                )
                if outcome["hit_target1"]:
                    counts["pop1"] += 1
                if outcome["hit_target2"]:
                    counts["pop2"] += 1
                if outcome["stop_first"]:
                    counts["stop_first"] += 1
        total = int(counts["candidate_bars"])
        results.append(
            {
                "scenario": name,
                "candidate_bars": total,
                "pop1_rate": round(counts["pop1"] / total, 3) if total else 0.0,
                "pop2_rate": round(counts["pop2"] / total, 3) if total else 0.0,
                "stop_first_rate": round(counts["stop_first"] / total, 3) if total else 0.0,
                "symbol_counts": dict(by_symbol),
            }
        )
    return results


def _parse_exit_time_et(day_text: str, value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.removesuffix(" ET").strip()
    try:
        local_dt = datetime.strptime(f"{day_text} {text}", "%Y-%m-%d %I:%M:%S %p")
    except ValueError:
        return None
    return local_dt.replace(tzinfo=EASTERN_TZ).astimezone(UTC)


def _build_cold_pause_windows(
    closed_today: list[dict[str, object]],
    *,
    day_text: str,
    streak_limit: int = 3,
    pause_minutes: int = 30,
    cold_peak_profit_pct: float = 1.0,
) -> dict[str, list[tuple[datetime, datetime]]]:
    by_symbol: dict[str, list[tuple[datetime, float, float]]] = defaultdict(list)
    for trade in closed_today:
        symbol = str(trade.get("ticker", "") or "").upper()
        exit_time = _parse_exit_time_et(day_text, trade.get("exit_time"))
        if not symbol or exit_time is None:
            continue
        pnl = float(trade.get("pnl", 0.0) or 0.0)
        peak = float(trade.get("peak_profit_pct", 0.0) or 0.0)
        by_symbol[symbol].append((exit_time, pnl, peak))

    windows: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for symbol, trades in by_symbol.items():
        streak = 0
        for exit_time, pnl, peak in sorted(trades, key=lambda item: item[0]):
            if pnl >= 0 or peak >= cold_peak_profit_pct:
                streak = 0
                continue
            streak += 1
            if streak >= streak_limit:
                windows[symbol].append((exit_time, exit_time + timedelta(minutes=pause_minutes)))
    return windows


def _scenario_cold_pause_only(bar: BarSample, pause_windows: dict[str, list[tuple[datetime, datetime]]]) -> bool:
    if _simplify_reason(bar.decision_reason) != "ticker_pause":
        return False
    symbol_windows = pause_windows.get(bar.symbol.upper(), [])
    return any(start <= bar.bar_time <= end for start, end in symbol_windows)


def _load_reclaim_closed_today(redis_url: str, strategy_code: str) -> list[dict[str, object]]:
    if not redis_url or redis is None:
        return []
    client = redis.from_url(redis_url, decode_responses=True)
    entries = client.xrevrange("mai_tai:strategy-state", count=1)
    if not entries:
        return []
    payload = json.loads(entries[0][1]["data"]).get("payload", {})
    bots = payload.get("bots", [])
    if not isinstance(bots, list):
        return []
    bot_state = next((item for item in bots if item.get("strategy_code") == strategy_code), None)
    if not isinstance(bot_state, dict):
        return []
    closed = bot_state.get("closed_today", [])
    return closed if isinstance(closed, list) else []


def _analyze_closed_trades(closed_today: list[dict[str, object]]) -> dict[str, object]:
    summary = {
        "total": len(closed_today),
        "wins": 0,
        "losses": 0,
        "flat": 0,
        "losses_peak_ge_1": 0,
        "losses_peak_ge_2": 0,
        "losses_peak_eq_0": 0,
        "wins_peak_lt_1": 0,
    }
    by_symbol: Counter[str] = Counter()
    by_path: Counter[str] = Counter()
    for trade in closed_today:
        pnl = float(trade.get("pnl", 0.0) or 0.0)
        peak = float(trade.get("peak_profit_pct", 0.0) or 0.0)
        if pnl > 0.001:
            summary["wins"] += 1
            if peak < 1.0:
                summary["wins_peak_lt_1"] += 1
        elif pnl < -0.001:
            summary["losses"] += 1
            by_symbol[str(trade.get("ticker", ""))] += 1
            by_path[str(trade.get("path", ""))] += 1
            if peak >= 1.0:
                summary["losses_peak_ge_1"] += 1
            if peak >= 2.0:
                summary["losses_peak_ge_2"] += 1
            if abs(peak) < 1e-9:
                summary["losses_peak_eq_0"] += 1
        else:
            summary["flat"] += 1
    return {
        "summary": summary,
        "loss_by_symbol": dict(by_symbol),
        "loss_by_path": dict(by_path),
        "sample": closed_today[:20],
    }


def main() -> None:
    args = _parse_args()
    if not args.db_url:
        raise SystemExit("A database URL is required via --db-url or MAI_TAI_DATABASE_URL.")

    target_day = date.fromisoformat(args.date)
    start_utc, end_utc = _et_window(target_day)
    selected_symbols = set(args.symbols or [])

    engine = create_engine(args.db_url)
    with Session(engine) as session:
        bars = _load_bars(
            session,
            strategy_code=args.strategy,
            start_utc=start_utc,
            end_utc=end_utc,
            symbols=selected_symbols or None,
        )

    bars_by_symbol: dict[str, list[BarSample]] = defaultdict(list)
    for bar in bars:
        bars_by_symbol[bar.symbol].append(bar)

    blocker_rows, blocker_by_symbol = _analyze_blockers(
        bars_by_symbol,
        lookahead_bars=args.lookahead_bars,
        target1_pct=args.target1_pct,
        target2_pct=args.target2_pct,
        stop_pct=args.stop_pct,
    )
    scenarios = _analyze_scenarios(
        bars_by_symbol,
        lookahead_bars=args.lookahead_bars,
        target1_pct=args.target1_pct,
        target2_pct=args.target2_pct,
        stop_pct=args.stop_pct,
    )
    closed_today = _load_reclaim_closed_today(args.redis_url, args.strategy)
    trade_analysis = _analyze_closed_trades(closed_today)
    cold_pause_windows = _build_cold_pause_windows(closed_today, day_text=args.date)
    cold_pause_counts: Counter[str] = Counter()
    for symbol, bars in bars_by_symbol.items():
        for index, bar in enumerate(bars):
            if not _scenario_cold_pause_only(bar, cold_pause_windows):
                continue
            cold_pause_counts["candidate_bars"] += 1
            outcome = _forward_outcome(
                bars,
                index,
                lookahead_bars=args.lookahead_bars,
                target1_pct=args.target1_pct,
                target2_pct=args.target2_pct,
                stop_pct=args.stop_pct,
            )
            if outcome["hit_target1"]:
                cold_pause_counts["pop1"] += 1
            if outcome["hit_target2"]:
                cold_pause_counts["pop2"] += 1
            if outcome["stop_first"]:
                cold_pause_counts["stop_first"] += 1
    cold_total = int(cold_pause_counts["candidate_bars"])
    scenario_cold_pause = {
        "scenario": "pause_only_on_cold_losses_1pct",
        "candidate_bars": cold_total,
        "pop1_rate": round(cold_pause_counts["pop1"] / cold_total, 3) if cold_total else 0.0,
        "pop2_rate": round(cold_pause_counts["pop2"] / cold_total, 3) if cold_total else 0.0,
        "stop_first_rate": round(cold_pause_counts["stop_first"] / cold_total, 3) if cold_total else 0.0,
    }

    result = {
        "date": args.date,
        "strategy": args.strategy,
        "symbols": sorted(bars_by_symbol),
        "lookahead_bars": args.lookahead_bars,
        "target1_pct": args.target1_pct,
        "target2_pct": args.target2_pct,
        "stop_pct": args.stop_pct,
        "blocked_overall": blocker_rows,
        "blocked_by_symbol": blocker_by_symbol,
        "scenarios": scenarios + [scenario_cold_pause],
        "closed_trade_analysis": trade_analysis,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
