from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import render_live_day_review as review
from compare_30s_variants import _create_session_factory, _persist_replay_intent

from project_mai_tai.db.models import BrokerAccount, Strategy
from project_mai_tai.services.strategy_engine_app import StrategyBotRuntime, StrategyDefinition
from project_mai_tai.strategy_core.config import IndicatorConfig
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ
from project_mai_tai.strategy_core.trading_config import TradingConfig

RECOVERED_UNIVERSE: tuple[tuple[str, str, str, str], ...] = (
    ("sqlite:///tmp_replay/massive_apr01_sample.sqlite", "massive_apr01_sample", "AGPU", "2026-04-01"),
    ("sqlite:///tmp_replay/massive_apr01_sample.sqlite", "massive_apr01_sample", "BFRG", "2026-04-01"),
    ("sqlite:///tmp_replay/massive_apr01_sample.sqlite", "massive_apr01_sample", "CYCN", "2026-04-01"),
    ("sqlite:///tmp_replay/massive_apr01_sample.sqlite", "massive_apr01_sample", "ELAB", "2026-04-01"),
    ("sqlite:///tmp_replay/massive_apr01_renx.sqlite", "massive_apr01_renx", "RENX", "2026-04-01"),
    ("sqlite:///tmp_replay/massive_apr01_sample.sqlite", "massive_apr01_sample", "SST", "2026-04-01"),
    ("sqlite:///tmp_replay/massive_apr02_sample.sqlite", "massive_apr02_sample", "BDRX", "2026-04-02"),
    ("sqlite:///tmp_replay/massive_apr02_sample.sqlite", "massive_apr02_sample", "BFRG", "2026-04-02"),
    ("sqlite:///tmp_replay/massive_apr02_sample.sqlite", "massive_apr02_sample", "COCP", "2026-04-02"),
    ("sqlite:///tmp_replay/massive_apr02_sample.sqlite", "massive_apr02_sample", "PFSA", "2026-04-02"),
    ("sqlite:///tmp_replay/massive_apr02_sample.sqlite", "massive_apr02_sample", "SKYQ", "2026-04-02"),
    ("sqlite:///tmp_replay/massive_apr02_sample.sqlite", "massive_apr02_sample", "TMDE", "2026-04-02"),
    ("sqlite:///tmp_replay/massive_apr02_sample.sqlite", "massive_apr02_sample", "TURB", "2026-04-02"),
)

APR08_TOP5_UNIVERSE: tuple[tuple[str, str, str, str], ...] = (
    ("sqlite:///tmp_replay/massive_apr08_top5.sqlite", "massive_apr08_top5", "UCAR", "2026-04-08"),
    ("sqlite:///tmp_replay/massive_apr08_top5.sqlite", "massive_apr08_top5", "JEM", "2026-04-08"),
    ("sqlite:///tmp_replay/massive_apr08_top5.sqlite", "massive_apr08_top5", "BBGI", "2026-04-08"),
    ("sqlite:///tmp_replay/massive_apr08_top5.sqlite", "massive_apr08_top5", "HUBC", "2026-04-08"),
    ("sqlite:///tmp_replay/massive_apr08_top5.sqlite", "massive_apr08_top5", "SAFX", "2026-04-08"),
)

COMBINED_RECOVERED_UNIVERSE: tuple[tuple[str, str, str, str], ...] = RECOVERED_UNIVERSE + APR08_TOP5_UNIVERSE

RECLAIM_FOCUS_EXCLUDED_SYMBOLS: frozenset[str] = frozenset(
    {"JEM", "CYCN", "BFRG", "UCAR", "BBGI"}
)

RECLAIM_FOCUS_UNIVERSE: tuple[tuple[str, str, str, str], ...] = tuple(
    item for item in COMBINED_RECOVERED_UNIVERSE if item[2] not in RECLAIM_FOCUS_EXCLUDED_SYMBOLS
)

DEFAULT_OVERRIDES: dict[str, object] = {
    # Keep research aligned with the in-code reclaim baseline and only relax
    # risk throttles that would otherwise hide later setups in replay.
    "max_daily_loss": -1_000_000.0,
    "ticker_loss_pause_streak_limit": 0,
}


def get_reclaim_universe(name: str = "reclaim_focus") -> tuple[tuple[str, str, str, str], ...]:
    if name == "baseline":
        return RECOVERED_UNIVERSE
    if name == "apr08_top5":
        return APR08_TOP5_UNIVERSE
    if name == "combined":
        return COMBINED_RECOVERED_UNIVERSE
    if name == "reclaim_focus":
        return RECLAIM_FOCUS_UNIVERSE
    raise ValueError(f"Unsupported universe: {name}")


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _indicator_value(bar: review.BarRow, key: str) -> float | bool | str | None:
    if not isinstance(bar.indicators, dict):
        return None
    value = bar.indicators.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return value


def _selected_vwap(bar: review.BarRow) -> float:
    for key in ("decision_vwap", "selected_vwap", "extended_vwap", "vwap"):
        value = _indicator_value(bar, key)
        if isinstance(value, float) and value > 0:
            return value
    return 0.0


def _pct_distance(price: float, anchor: float) -> float | None:
    if anchor <= 0:
        return None
    return (price - anchor) / anchor


def _average_true_range(bars: list[review.BarRow]) -> float:
    if len(bars) < 2:
        return 0.0
    true_ranges: list[float] = []
    previous_close = float(bars[0].close_price)
    for bar in bars[1:]:
        high = float(bar.high_price)
        low = float(bar.low_price)
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = float(bar.close_price)
    return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0


def _touch_offsets(
    bars: list[review.BarRow],
    index: int,
    *,
    lookback_bars: int,
    tolerance_pct: float,
) -> dict[str, object]:
    current = bars[index]
    current_low = float(current.low_price)
    current_ema9 = float(_indicator_value(current, "ema9") or 0.0)
    current_vwap = _selected_vwap(current)
    current_ema9_touch = current_ema9 > 0 and current_low <= current_ema9 * (1.0 + tolerance_pct)
    current_vwap_touch = current_vwap > 0 and current_low <= current_vwap * (1.0 + tolerance_pct)

    ema9_offset: int | None = None
    vwap_offset: int | None = None
    for offset in range(1, min(index, lookback_bars) + 1):
        bar = bars[index - offset]
        ema9 = float(_indicator_value(bar, "ema9") or 0.0)
        vwap = _selected_vwap(bar)
        if ema9_offset is None and ema9 > 0 and float(bar.low_price) <= ema9 * (1.0 + tolerance_pct):
            ema9_offset = offset
        if vwap_offset is None and vwap > 0 and float(bar.low_price) <= vwap * (1.0 + tolerance_pct):
            vwap_offset = offset
        if ema9_offset is not None and vwap_offset is not None:
            break

    return {
        "current_ema9_touch": current_ema9_touch,
        "current_vwap_touch": current_vwap_touch,
        "prior_ema9_touch_offset": ema9_offset,
        "prior_vwap_touch_offset": vwap_offset,
        "same_bar_touch": current_ema9_touch or current_vwap_touch,
    }


def _mean_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _case_metrics(
    bars: list[review.BarRow],
    index: int,
    *,
    config: TradingConfig,
) -> dict[str, object]:
    bar = bars[index]
    current_close = float(bar.close_price)
    current_open = float(bar.open_price)
    current_high = float(bar.high_price)
    current_low = float(bar.low_price)
    current_volume = float(bar.volume)
    ema9 = float(_indicator_value(bar, "ema9") or 0.0)
    ema20 = float(_indicator_value(bar, "ema20") or 0.0)
    selected_vwap = _selected_vwap(bar)

    lookback = int(config.pretrigger_reclaim_lookback_bars)
    start = max(0, index - lookback + 1)
    reclaim_window = bars[start : index + 1]
    recent_high = max(float(item.high_price) for item in reclaim_window)
    spike_local = max(range(len(reclaim_window)), key=lambda offset: float(reclaim_window[offset].high_price))
    spike_bar = reclaim_window[spike_local]
    pre_spike_window = reclaim_window[: spike_local + 1]
    pre_spike_price = min(float(item.low_price) for item in pre_spike_window)
    pullback_phase = reclaim_window[spike_local + 1 :]
    pullback_low = min([current_low, *(float(item.low_price) for item in pullback_phase)])
    spike_gain = max(0.0, recent_high - pre_spike_price)
    pullback_pct = (recent_high - current_close) / recent_high if recent_high > 0 else None
    retrace_fraction = (recent_high - pullback_low) / spike_gain if spike_gain > 0 else None
    close_retrace_fraction = (recent_high - current_close) / spike_gain if spike_gain > 0 else None

    atr_window = bars[max(0, index - 14) : index]
    atr14 = _average_true_range(atr_window)
    retrace_atr = (recent_high - pullback_low) / atr14 if atr14 > 0 else None

    pullback_volumes = [float(item.volume) for item in pullback_phase] or [current_volume]
    spike_volume = float(spike_bar.volume)
    pullback_volume_ratio = (
        (sum(pullback_volumes) / len(pullback_volumes)) / spike_volume
        if spike_volume > 0 and pullback_volumes
        else None
    )

    volume_avg_bars = min(index, int(config.pretrigger_volume_avg_bars))
    prior_volumes = [float(item.volume) for item in bars[index - volume_avg_bars : index]] if volume_avg_bars > 0 else []
    current_rel_vol = current_volume / (sum(prior_volumes) / len(prior_volumes)) if prior_volumes else None

    bar_range = max(current_high - current_low, 0.000001)
    body_pct = abs(current_close - current_open) / bar_range
    close_pos_pct = (current_close - current_low) / bar_range
    upper_wick_pct = (current_high - max(current_open, current_close)) / bar_range

    touch = _touch_offsets(
        bars,
        index,
        lookback_bars=int(config.pretrigger_reclaim_touch_lookback_bars),
        tolerance_pct=float(config.pretrigger_reclaim_touch_tolerance_pct),
    )

    return {
        "bar_time_et": _ensure_utc(bar.bar_time).astimezone(EASTERN_TZ).isoformat(),
        "price": current_close,
        "decision_status": bar.decision_status,
        "decision_reason": bar.decision_reason,
        "decision_score": bar.decision_score,
        "decision_path": bar.decision_path,
        "recent_high": recent_high,
        "spike_high": float(spike_bar.high_price),
        "spike_low": float(spike_bar.low_price),
        "spike_volume": spike_volume,
        "pre_spike_price": pre_spike_price,
        "spike_gain": spike_gain,
        "pullback_low": pullback_low,
        "pullback_pct_from_high": pullback_pct,
        "retrace_fraction_of_leg": retrace_fraction,
        "close_retrace_fraction_of_leg": close_retrace_fraction,
        "retrace_atr": retrace_atr,
        "pullback_bars": max(0, len(pullback_phase)),
        "pullback_volume_ratio": pullback_volume_ratio,
        "current_rel_vol": current_rel_vol,
        "ema9": ema9,
        "ema20": ema20,
        "selected_vwap": selected_vwap,
        "ema9_extension_pct": _pct_distance(current_close, ema9),
        "vwap_extension_pct": _pct_distance(current_close, selected_vwap),
        "close_above_ema9": ema9 > 0 and current_close >= ema9,
        "close_above_vwap": selected_vwap > 0 and current_close >= selected_vwap,
        "body_pct": body_pct,
        "close_pos_pct": close_pos_pct,
        "upper_wick_pct": upper_wick_pct,
        "macd": _indicator_value(bar, "macd"),
        "signal": _indicator_value(bar, "signal"),
        "histogram": _indicator_value(bar, "histogram"),
        "stoch_k": _indicator_value(bar, "stoch_k"),
        **touch,
    }


def _next_open_index(intents: list[review.IntentRow], start_index: int) -> int | None:
    for offset in range(start_index + 1, len(intents)):
        item = intents[offset]
        if item.side == "buy" and item.intent_type == "open" and item.status == "filled":
            return offset
    return None


def _starter_lifecycle(intents: list[review.IntentRow], entry_index: int) -> dict[str, object]:
    entry = intents[entry_index]
    boundary = _next_open_index(intents, entry_index)
    later = intents[entry_index + 1 : boundary]
    had_add = any(item.side == "buy" and item.intent_type == "add" and item.status == "filled" for item in later)
    first_close = next((item for item in later if item.side == "sell" and item.intent_type == "close" and item.status == "filled"), None)
    if first_close is None:
        lifecycle = "starter_open"
        close_reason = ""
    elif first_close.reason == "PRETRIGGER_NO_CONFIRM":
        lifecycle = "starter_no_confirm"
        close_reason = first_close.reason
    elif first_close.reason == "PRETRIGGER_FAIL_FAST":
        lifecycle = "starter_fail_fast"
        close_reason = first_close.reason
    else:
        lifecycle = "starter_closed"
        close_reason = first_close.reason
    if had_add:
        lifecycle = "starter_with_add"
    return {
        "entry_time": entry.created_at,
        "had_add": had_add,
        "close_reason": close_reason,
        "lifecycle": lifecycle,
    }


def _replay_symbol(
    *,
    db_url: str,
    source_strategy: str,
    symbol: str,
    day: str,
    config: TradingConfig,
) -> tuple[list[review.BarRow], list[review.IntentRow], list[review.ReviewMarker], list[review.ActualOutcome]]:
    source_engine = create_engine(db_url)
    start_utc, end_utc = review._et_window(date.fromisoformat(day))
    try:
        with Session(source_engine) as session:
            source_bars = review._load_bars(
                session,
                strategy_code=source_strategy,
                symbol=symbol,
                start_utc=start_utc,
                end_utc=end_utc,
            )
    finally:
        source_engine.dispose()

    session_factory, engine = _create_session_factory()
    runtime_code = f"diag_reclaim_{symbol.lower()}_{day.replace('-', '')}"
    try:
        with session_factory() as session:
            strategy = Strategy(
                code=runtime_code,
                name=runtime_code,
                execution_mode="shadow",
                metadata_json={"diagnostic": "reclaim_stage"},
            )
            account = BrokerAccount(
                name=f"replay:{runtime_code}",
                provider="replay",
                environment="analysis",
                external_account_id=None,
            )
            session.add(strategy)
            session.add(account)
            session.commit()
            session.refresh(strategy)
            session.refresh(account)
            strategy_id = strategy.id
            broker_account_id = account.id

        now_holder = {"value": source_bars[0].bar_time.astimezone(EASTERN_TZ)}

        runtime = StrategyBotRuntime(
            StrategyDefinition(
                code=runtime_code,
                display_name="macd_30s_reclaim",
                account_name=f"replay:{runtime_code}",
                interval_secs=30,
                trading_config=config,
                indicator_config=IndicatorConfig(),
            ),
            now_provider=lambda: now_holder["value"],
            session_factory=session_factory,
            use_live_aggregate_bars=True,
            live_aggregate_fallback_enabled=False,
        )
        runtime.set_watchlist([symbol])

        fill_counter = 0
        for bar in source_bars:
            now_holder["value"] = bar.bar_time.astimezone(EASTERN_TZ)
            intents = runtime.handle_live_bar(
                symbol=symbol,
                open_price=bar.open_price,
                high_price=bar.high_price,
                low_price=bar.low_price,
                close_price=bar.close_price,
                volume=bar.volume,
                timestamp=bar.bar_time.timestamp(),
                trade_count=bar.indicators.get("trade_count", 1) if isinstance(bar.indicators, dict) else 1,
            )
            for intent in intents:
                payload = intent.payload
                _persist_replay_intent(
                    session_factory=session_factory,
                    strategy_id=strategy_id,
                    broker_account_id=broker_account_id,
                    payload=payload,
                    created_at=bar.bar_time,
                )
                fill_counter += 1
                runtime.apply_execution_fill(
                    client_order_id=f"{runtime_code}-{fill_counter}",
                    symbol=str(payload.symbol),
                    intent_type=str(payload.intent_type),
                    status="filled",
                    side=str(payload.side),
                    quantity=payload.quantity,
                    price=bar.close_price,
                    path=str(payload.metadata.get("path", "")),
                )

        now_holder["value"] = (source_bars[-1].bar_time + timedelta(seconds=30)).astimezone(EASTERN_TZ)
        final_intents, _ = runtime.flush_completed_bars()
        for intent in final_intents:
            payload = intent.payload
            _persist_replay_intent(
                session_factory=session_factory,
                strategy_id=strategy_id,
                broker_account_id=broker_account_id,
                payload=payload,
                created_at=source_bars[-1].bar_time + timedelta(seconds=30),
            )

        start_utc = source_bars[0].bar_time.astimezone(UTC) - timedelta(minutes=1)
        end_utc = source_bars[-1].bar_time.astimezone(UTC) + timedelta(minutes=1)
        with session_factory() as session:
            replay_bars = review._load_bars(
                session,
                strategy_code=runtime_code,
                symbol=symbol,
                start_utc=start_utc,
                end_utc=end_utc,
            )
            intents = review._load_intents(
                session,
                strategy_code=runtime_code,
                symbol=symbol,
                start_utc=start_utc,
                end_utc=end_utc,
            )
        for bar in replay_bars:
            bar.bar_time = _ensure_utc(bar.bar_time)
        for intent in intents:
            intent.created_at = _ensure_utc(intent.created_at)

        markers = review._future_review(
            replay_bars,
            lookahead_bars=10,
            target_up_pct=2.0,
            stop_down_pct=1.0,
        )
        outcomes = review._classify_actual_outcomes(
            replay_bars,
            intents,
            lookahead_bars=10,
            target_up_pct=2.0,
            stop_down_pct=1.0,
        )
        return replay_bars, intents, markers, outcomes
    finally:
        engine.dispose()


def _build_config(overrides: dict[str, object]) -> TradingConfig:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    fields = asdict(config)
    fields.update(overrides)
    return TradingConfig(**fields)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay reclaim diagnostics across a chosen universe.")
    parser.add_argument(
        "--universe",
        choices=("reclaim_focus", "combined", "baseline", "apr08_top5"),
        default="reclaim_focus",
        help="Which replay universe to evaluate.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    config = _build_config(DEFAULT_OVERRIDES)
    universe = get_reclaim_universe(args.universe)
    entry_lifecycle_counts: Counter[str] = Counter()
    entry_outcome_counts: Counter[str] = Counter()
    blocked_reason_counts: Counter[str] = Counter()
    should_enter_reason_counts: Counter[str] = Counter()
    per_symbol: list[dict[str, object]] = []
    samples: dict[str, list[dict[str, object]]] = defaultdict(list)
    aggregate_metrics: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    for db_url, strategy_code, symbol, day in universe:
        bars, intents, markers, outcomes = _replay_symbol(
            db_url=db_url,
            source_strategy=strategy_code,
            symbol=symbol,
            day=day,
            config=config,
        )
        bar_index_by_time = {bar.bar_time: idx for idx, bar in enumerate(bars)}
        markers_by_time = {marker.bar_time: marker for marker in markers}

        open_intent_indexes = [
            idx
            for idx, intent in enumerate(intents)
            if intent.status == "filled" and intent.side == "buy" and intent.intent_type == "open"
        ]
        symbol_summary = {
            "date": day,
            "symbol": symbol,
            "open_intents": len(open_intent_indexes),
            "outcomes": Counter(),
            "lifecycles": Counter(),
            "should_enter_blocked": 0,
        }

        outcomes_by_time = {_ensure_utc(outcome.event_time): outcome for outcome in outcomes}

        for entry_index in open_intent_indexes:
            intent = intents[entry_index]
            entry_time = _ensure_utc(intent.created_at)
            outcome = outcomes_by_time.get(entry_time)
            if outcome is None:
                lifecycle = _starter_lifecycle(intents, entry_index)
                lifecycle_name = str(lifecycle["lifecycle"])
                entry_lifecycle_counts[lifecycle_name] += 1
                symbol_summary["lifecycles"][lifecycle_name] += 1
                samples[lifecycle_name].append(
                    {
                        "date": day,
                        "symbol": symbol,
                        "category": "unresolved",
                        "lifecycle": lifecycle_name,
                        "close_reason": lifecycle["close_reason"],
                        "had_add": lifecycle["had_add"],
                        "note": "Starter had no classified forward outcome in the lookahead window.",
                        "bar_time_et": entry_time.astimezone(EASTERN_TZ).isoformat(),
                    }
                )
                continue

            bar_time = _ensure_utc(outcome.bar_time)
            bar_index = bar_index_by_time.get(bar_time)
            if bar_index is None:
                continue
            lifecycle = _starter_lifecycle(intents, entry_index)
            metrics = _case_metrics(bars, bar_index, config=config)
            category = outcome.category
            lifecycle_name = str(lifecycle["lifecycle"])
            entry_outcome_counts[category] += 1
            entry_lifecycle_counts[lifecycle_name] += 1
            symbol_summary["outcomes"][category] += 1
            symbol_summary["lifecycles"][lifecycle_name] += 1
            sample = {
                "date": day,
                "symbol": symbol,
                "category": category,
                "lifecycle": lifecycle_name,
                "close_reason": lifecycle["close_reason"],
                "had_add": lifecycle["had_add"],
                "note": outcome.note,
                **metrics,
            }
            samples[category].append(sample)
            samples[lifecycle_name].append(sample)
            for key in (
                "pullback_pct_from_high",
                "retrace_fraction_of_leg",
                "close_retrace_fraction_of_leg",
                "retrace_atr",
                "pullback_bars",
                "pullback_volume_ratio",
                "current_rel_vol",
                "ema9_extension_pct",
                "vwap_extension_pct",
                "body_pct",
                "close_pos_pct",
                "upper_wick_pct",
            ):
                value = metrics.get(key)
                if isinstance(value, float):
                    aggregate_metrics[category][key].append(value)
                    aggregate_metrics[lifecycle_name][key].append(value)

        for marker in markers:
            reason = marker.reason or "(unspecified)"
            blocked_reason_counts[reason] += 1
            if marker.category != "should_enter":
                continue
            should_enter_reason_counts[reason] += 1
            symbol_summary["should_enter_blocked"] += 1
            bar_index = bar_index_by_time.get(_ensure_utc(marker.bar_time))
            if bar_index is None:
                continue
            metrics = _case_metrics(bars, bar_index, config=config)
            sample = {
                "date": day,
                "symbol": symbol,
                "marker_category": marker.category,
                "marker_note": marker.note,
                "blocked_reason": reason,
                **metrics,
            }
            samples["blocked_should_enter"].append(sample)
            for key in (
                "pullback_pct_from_high",
                "retrace_fraction_of_leg",
                "close_retrace_fraction_of_leg",
                "retrace_atr",
                "pullback_bars",
                "pullback_volume_ratio",
                "current_rel_vol",
                "ema9_extension_pct",
                "vwap_extension_pct",
                "body_pct",
                "close_pos_pct",
                "upper_wick_pct",
            ):
                value = metrics.get(key)
                if isinstance(value, float):
                    aggregate_metrics["blocked_should_enter"][key].append(value)

        symbol_summary["outcomes"] = dict(symbol_summary["outcomes"])
        symbol_summary["lifecycles"] = dict(symbol_summary["lifecycles"])
        per_symbol.append(symbol_summary)

    payload = {
        "baseline_overrides": DEFAULT_OVERRIDES,
        "universe": [
            {
                "db_url": db_url,
                "source_strategy": strategy_code,
                "symbol": symbol,
                "date": day,
            }
            for db_url, strategy_code, symbol, day in universe
        ],
        "summary": {
            "entry_outcome_counts": dict(entry_outcome_counts),
            "entry_lifecycle_counts": dict(entry_lifecycle_counts),
            "blocked_reason_counts": dict(blocked_reason_counts.most_common(12)),
            "should_enter_blocked_reasons": dict(should_enter_reason_counts.most_common(12)),
            "metric_means": {
                category: {
                    key: _mean_or_none(values)
                    for key, values in metrics.items()
                }
                for category, metrics in aggregate_metrics.items()
            },
        },
        "per_symbol": per_symbol,
        "samples": {
            "taken_good": samples["taken_good"][:12],
            "taken_bad": samples["taken_bad"][:12],
            "starter_no_confirm": samples["starter_no_confirm"][:12],
            "starter_fail_fast": samples["starter_fail_fast"][:12],
            "blocked_should_enter": samples["blocked_should_enter"][:24],
        },
    }

    output_path = SCRIPT_DIR.parent / "tmp_replay" / "reclaim_stage_diagnostics.json"
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
