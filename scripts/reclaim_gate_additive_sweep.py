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

from reclaim_stage_diagnostics import DEFAULT_OVERRIDES, RECOVERED_UNIVERSE, _ensure_utc, _replay_symbol
from project_mai_tai.strategy_core.trading_config import TradingConfig


OPEN_BASELINE_OVERRIDES: dict[str, object] = {
    "pretrigger_reclaim_require_pullback": False,
    "pretrigger_reclaim_require_touch": False,
    "pretrigger_reclaim_require_pullback_absorption": False,
    "pretrigger_reclaim_require_location": False,
    "pretrigger_reclaim_require_candle": False,
    "pretrigger_reclaim_require_stoch": False,
    "pretrigger_reclaim_require_trend": False,
    "pretrigger_reclaim_require_momentum": False,
    "pretrigger_reclaim_require_volume": False,
    "pretrigger_reclaim_score_threshold": 0,
}


ADDITIVE_PROFILES: dict[str, dict[str, object]] = {
    "open_baseline": {},
    "pullback_on": {
        "pretrigger_reclaim_require_pullback": True,
    },
    "touch_on": {
        "pretrigger_reclaim_require_touch": True,
    },
    "pullback_volume_on": {
        "pretrigger_reclaim_require_pullback_absorption": True,
    },
    "location_on": {
        "pretrigger_reclaim_require_location": True,
    },
    "candle_on": {
        "pretrigger_reclaim_require_candle": True,
    },
    "stoch_on": {
        "pretrigger_reclaim_require_stoch": True,
    },
    "trend_on": {
        "pretrigger_reclaim_require_trend": True,
    },
    "momentum_on": {
        "pretrigger_reclaim_require_momentum": True,
    },
    "volume_on": {
        "pretrigger_reclaim_require_volume": True,
    },
    "score_on": {
        "pretrigger_reclaim_score_threshold": 0.55,
    },
}


def _build_config(overrides: dict[str, object]) -> TradingConfig:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    fields = asdict(config)
    fields.update(DEFAULT_OVERRIDES)
    fields.update(OPEN_BASELINE_OVERRIDES)
    fields.update(overrides)
    return TradingConfig(**fields)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one-by-one reclaim gate additive sweeps.")
    parser.add_argument("--profiles", nargs="*", default=list(ADDITIVE_PROFILES), help="Profile names to run.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    selected = [name for name in args.profiles if name in ADDITIVE_PROFILES]
    if not selected:
        raise SystemExit("No valid profiles requested.")

    results: list[dict[str, object]] = []
    output_path = SCRIPT_DIR.parent / "tmp_replay" / "reclaim_gate_additive_sweep.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    for profile_name in selected:
        overrides = ADDITIVE_PROFILES[profile_name]
        config = _build_config(overrides)
        totals: Counter[str] = Counter()
        symbols_with_intents: list[dict[str, object]] = []

        for db_url, strategy_code, symbol, day in RECOVERED_UNIVERSE:
            _bars, intents, _markers, outcomes = _replay_symbol(
                db_url=db_url,
                source_strategy=strategy_code,
                symbol=symbol,
                day=day,
                config=config,
            )
            outcomes_by_time = {_ensure_utc(item.event_time): item for item in outcomes}
            open_intents = [
                intent
                for intent in intents
                if intent.status == "filled" and intent.side == "buy" and intent.intent_type == "open"
            ]
            if not open_intents:
                continue

            symbol_counts: Counter[str] = Counter()
            for intent in open_intents:
                outcome = outcomes_by_time.get(_ensure_utc(intent.created_at))
                if outcome is None:
                    totals["unresolved"] += 1
                    symbol_counts["unresolved"] += 1
                else:
                    totals[outcome.category] += 1
                    symbol_counts[outcome.category] += 1
            totals["open_intents"] += len(open_intents)
            symbols_with_intents.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "open_intents": len(open_intents),
                    "outcomes": dict(symbol_counts),
                }
            )

        results.append(
            {
                "profile": profile_name,
                "overrides": overrides,
                "totals": {
                    "open_intents": int(totals["open_intents"]),
                    "taken_good": int(totals["taken_good"]),
                    "taken_bad": int(totals["taken_bad"]),
                    "taken_open": int(totals["taken_open"]),
                    "unresolved": int(totals["unresolved"]),
                    "symbols_with_intents": len(symbols_with_intents),
                },
                "symbols_with_intents": symbols_with_intents,
            }
        )
        output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
