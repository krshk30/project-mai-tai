from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from reclaim_stage_diagnostics import (
    DEFAULT_OVERRIDES,
    RECOVERED_UNIVERSE,
    _ensure_utc,
    _replay_symbol,
    _starter_lifecycle,
)
from project_mai_tai.strategy_core.trading_config import TradingConfig


PROFILES: dict[str, dict[str, object]] = {
    "baseline": {},
    "lookahead_4": {
        "pretrigger_failed_break_lookahead_bars": 4,
    },
    "lookahead_5": {
        "pretrigger_failed_break_lookahead_bars": 5,
    },
    "hold_buf_025": {
        "pretrigger_fail_hold_buf_atr": 0.25,
    },
    "hold_buf_035": {
        "pretrigger_fail_hold_buf_atr": 0.35,
    },
    "confirm_relaxed": {
        "pretrigger_add_max_distance_to_ema9_pct": 0.05,
        "pretrigger_min_bar_rel_vol_breakout": 1.20,
    },
    "lookahead4_confirm_relaxed": {
        "pretrigger_failed_break_lookahead_bars": 4,
        "pretrigger_add_max_distance_to_ema9_pct": 0.05,
        "pretrigger_min_bar_rel_vol_breakout": 1.20,
    },
    "hold025_confirm_relaxed": {
        "pretrigger_fail_hold_buf_atr": 0.25,
        "pretrigger_add_max_distance_to_ema9_pct": 0.05,
        "pretrigger_min_bar_rel_vol_breakout": 1.20,
    },
    "all_relaxed": {
        "pretrigger_failed_break_lookahead_bars": 4,
        "pretrigger_fail_hold_buf_atr": 0.25,
        "pretrigger_add_max_distance_to_ema9_pct": 0.05,
        "pretrigger_min_bar_rel_vol_breakout": 1.20,
    },
    "failfast_no_macd": {
        "pretrigger_fail_fast_on_macd_below_signal": False,
    },
    "failfast_no_ema9": {
        "pretrigger_fail_fast_on_price_below_ema9": False,
    },
    "failfast_hold_only": {
        "pretrigger_fail_fast_on_macd_below_signal": False,
        "pretrigger_fail_fast_on_price_below_ema9": False,
    },
    "lookahead4_hold_only": {
        "pretrigger_failed_break_lookahead_bars": 4,
        "pretrigger_fail_fast_on_macd_below_signal": False,
        "pretrigger_fail_fast_on_price_below_ema9": False,
    },
}


def _build_config(profile_overrides: dict[str, object]) -> TradingConfig:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    fields = asdict(config)
    fields.update(DEFAULT_OVERRIDES)
    fields.update(profile_overrides)
    return TradingConfig(**fields)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sweep reclaim starter-management profiles across the recovered universe.")
    parser.add_argument("--profiles", nargs="*", default=list(PROFILES), help="Profile names to run.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    selected = [name for name in args.profiles if name in PROFILES]
    if not selected:
        raise SystemExit("No valid profiles requested.")

    results: list[dict[str, object]] = []
    output_path = SCRIPT_DIR.parent / "tmp_replay" / "reclaim_starter_management_sweep.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    for profile_name in selected:
        profile_overrides = PROFILES[profile_name]
        config = _build_config(profile_overrides)
        outcome_counts: Counter[str] = Counter()
        lifecycle_counts: Counter[str] = Counter()
        close_reason_counts: Counter[str] = Counter()
        symbols_with_intents: list[dict[str, object]] = []

        for db_url, strategy_code, symbol, day in RECOVERED_UNIVERSE:
            bars, intents, _markers, outcomes = _replay_symbol(
                db_url=db_url,
                source_strategy=strategy_code,
                symbol=symbol,
                day=day,
                config=config,
            )
            outcomes_by_time = {_ensure_utc(outcome.event_time): outcome for outcome in outcomes}
            open_indexes = [
                idx
                for idx, intent in enumerate(intents)
                if intent.status == "filled" and intent.side == "buy" and intent.intent_type == "open"
            ]
            if not open_indexes:
                continue

            symbol_outcomes: Counter[str] = Counter()
            symbol_lifecycles: Counter[str] = Counter()
            for open_index in open_indexes:
                entry = intents[open_index]
                lifecycle = _starter_lifecycle(intents, open_index)
                lifecycle_name = str(lifecycle["lifecycle"])
                lifecycle_counts[lifecycle_name] += 1
                symbol_lifecycles[lifecycle_name] += 1
                if lifecycle["close_reason"]:
                    close_reason_counts[str(lifecycle["close_reason"])] += 1

                outcome = outcomes_by_time.get(_ensure_utc(entry.created_at))
                if outcome is None:
                    symbol_outcomes["unresolved"] += 1
                    outcome_counts["unresolved"] += 1
                else:
                    symbol_outcomes[outcome.category] += 1
                    outcome_counts[outcome.category] += 1

            symbols_with_intents.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "open_intents": len(open_indexes),
                    "outcomes": dict(symbol_outcomes),
                    "lifecycles": dict(symbol_lifecycles),
                }
            )

        results.append(
            {
                "profile": profile_name,
                "overrides": profile_overrides,
                "totals": {
                    "open_intents": sum(item["open_intents"] for item in symbols_with_intents),
                    "taken_good": int(outcome_counts["taken_good"]),
                    "taken_bad": int(outcome_counts["taken_bad"]),
                    "taken_open": int(outcome_counts["taken_open"]),
                    "unresolved": int(outcome_counts["unresolved"]),
                    "symbols_with_intents": len(symbols_with_intents),
                },
                "lifecycle_counts": dict(lifecycle_counts),
                "close_reason_counts": dict(close_reason_counts),
                "symbols_with_intents": symbols_with_intents,
            }
        )
        output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
