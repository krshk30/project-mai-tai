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


MIX_PROFILES: dict[str, dict[str, object]] = {
    "baseline": {},
    "volume_off": {
        "pretrigger_reclaim_require_volume": False,
    },
    "pullback_volume_off": {
        "pretrigger_reclaim_require_pullback_absorption": False,
    },
    "volume_and_pullback_volume_off": {
        "pretrigger_reclaim_require_volume": False,
        "pretrigger_reclaim_require_pullback_absorption": False,
    },
    "volume_and_stoch_off": {
        "pretrigger_reclaim_require_volume": False,
        "pretrigger_reclaim_require_stoch": False,
    },
    "pullback_volume_and_stoch_off": {
        "pretrigger_reclaim_require_pullback_absorption": False,
        "pretrigger_reclaim_require_stoch": False,
    },
    "volume_pullback_volume_stoch_off": {
        "pretrigger_reclaim_require_volume": False,
        "pretrigger_reclaim_require_pullback_absorption": False,
        "pretrigger_reclaim_require_stoch": False,
    },
    "volume_off_harder_location": {
        "pretrigger_reclaim_require_volume": False,
        "pretrigger_reclaim_max_extension_above_ema9_pct": 0.04,
        "pretrigger_reclaim_max_extension_above_vwap_pct": 0.04,
    },
}


def _build_config(overrides: dict[str, object]) -> TradingConfig:
    fields = asdict(TradingConfig().make_30s_reclaim_variant(quantity=100))
    fields.update(DEFAULT_OVERRIDES)
    fields.update(overrides)
    return TradingConfig(**fields)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run mixed reclaim profile sweeps.")
    parser.add_argument("--profiles", nargs="*", default=list(MIX_PROFILES), help="Profile names to run.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    selected = [name for name in args.profiles if name in MIX_PROFILES]
    if not selected:
        raise SystemExit("No valid profiles requested.")

    results: list[dict[str, object]] = []
    output_path = SCRIPT_DIR.parent / "tmp_replay" / "reclaim_mix_sweep.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    for profile_name in selected:
        config = _build_config(MIX_PROFILES[profile_name])
        totals: Counter[str] = Counter()

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
            for intent in open_intents:
                outcome = outcomes_by_time.get(_ensure_utc(intent.created_at))
                if outcome is None:
                    totals["unresolved"] += 1
                else:
                    totals[outcome.category] += 1
            totals["open_intents"] += len(open_intents)

        good = int(totals["taken_good"])
        bad = int(totals["taken_bad"])
        results.append(
            {
                "profile": profile_name,
                "overrides": MIX_PROFILES[profile_name],
                "open_intents": int(totals["open_intents"]),
                "taken_good": good,
                "taken_bad": bad,
                "taken_open": int(totals["taken_open"]),
                "unresolved": int(totals["unresolved"]),
                "good_minus_bad": good - bad,
                "resolved_good_rate": round(good / max(1, good + bad), 4),
            }
        )
        output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
