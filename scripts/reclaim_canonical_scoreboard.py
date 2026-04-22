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
    _ensure_utc,
    _replay_symbol,
    _starter_lifecycle,
    get_reclaim_universe,
)
from project_mai_tai.strategy_core.trading_config import TradingConfig


PROFILES: dict[str, dict[str, object]] = {
    "current_research_baseline": {},
    "location_off": {
        "pretrigger_reclaim_require_location": False,
    },
    "trend_off": {
        "pretrigger_reclaim_require_trend": False,
    },
    "momentum_off": {
        "pretrigger_reclaim_require_momentum": False,
    },
}


def _build_config(overrides: dict[str, object]) -> TradingConfig:
    fields = asdict(TradingConfig().make_30s_reclaim_variant(quantity=100))
    fields.update(DEFAULT_OVERRIDES)
    fields.update(overrides)
    return TradingConfig(**fields)


def _score_profile(config: TradingConfig, *, universe_name: str = "combined") -> dict[str, object]:
    entry_outcomes: Counter[str] = Counter()
    entry_lifecycles: Counter[str] = Counter()
    universe = get_reclaim_universe(universe_name)

    for db_url, strategy_code, symbol, day in universe:
        _bars, intents, _markers, outcomes = _replay_symbol(
            db_url=db_url,
            source_strategy=strategy_code,
            symbol=symbol,
            day=day,
            config=config,
        )
        open_indexes = [
            idx
            for idx, intent in enumerate(intents)
            if intent.status == "filled" and intent.side == "buy" and intent.intent_type == "open"
        ]
        outcomes_by_time = {_ensure_utc(outcome.event_time): outcome for outcome in outcomes}

        for entry_index in open_indexes:
            intent = intents[entry_index]
            lifecycle = _starter_lifecycle(intents, entry_index)
            entry_lifecycles[str(lifecycle["lifecycle"])] += 1

            outcome = outcomes_by_time.get(_ensure_utc(intent.created_at))
            if outcome is None:
                entry_outcomes["unresolved"] += 1
            else:
                entry_outcomes[str(outcome.category)] += 1

        entry_outcomes["open_intents"] += len(open_indexes)

    good = int(entry_outcomes["taken_good"])
    bad = int(entry_outcomes["taken_bad"])
    taken_open = int(entry_outcomes["taken_open"])
    unresolved = int(entry_outcomes["unresolved"])
    return {
        "open_intents": int(entry_outcomes["open_intents"]),
        "taken_good": good,
        "taken_bad": bad,
        "taken_open": taken_open,
        "unresolved": unresolved,
        "good_minus_bad": good - bad,
        "resolved_good_rate": round(good / max(1, good + bad), 4),
        "lifecycle_counts": {
            "starter_no_confirm": int(entry_lifecycles["starter_no_confirm"]),
            "starter_fail_fast": int(entry_lifecycles["starter_fail_fast"]),
            "starter_closed": int(entry_lifecycles["starter_closed"]),
            "starter_with_add": int(entry_lifecycles["starter_with_add"]),
            "starter_open": int(entry_lifecycles["starter_open"]),
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute the canonical reclaim scoreboard.")
    parser.add_argument("--profiles", nargs="*", default=list(PROFILES), help="Profile names to run.")
    parser.add_argument(
        "--universe",
        choices=("reclaim_focus", "combined", "baseline", "apr08_top5"),
        default="reclaim_focus",
        help="Which replay universe to evaluate.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    selected = [name for name in args.profiles if name in PROFILES]
    if not selected:
        raise SystemExit("No valid profiles requested.")

    results: list[dict[str, object]] = []
    output_path = SCRIPT_DIR.parent / "tmp_replay" / "reclaim_canonical_scoreboard.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    for profile_name in selected:
        overrides = PROFILES[profile_name]
        config = _build_config(overrides)
        results.append(
            {
                "profile": profile_name,
                "universe": args.universe,
                "overrides": overrides,
                **_score_profile(config, universe_name=args.universe),
            }
        )
        output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
