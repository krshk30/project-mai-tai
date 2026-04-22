from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import compare_30s_variants as compare
import reclaim_stage_diagnostics as diagnostics
import render_live_day_review as review

from project_mai_tai.strategy_core.trading_config import TradingConfig


DEFAULT_VARIANTS = ("macd_30s", "macd_30s_reclaim", "macd_30s_retest")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare the 30s research family across a replay universe.")
    parser.add_argument(
        "--universe",
        choices=("reclaim_focus", "combined", "baseline", "apr08_top5"),
        default="combined",
        help="Replay universe to evaluate.",
    )
    parser.add_argument(
        "--variants",
        nargs="*",
        default=list(DEFAULT_VARIANTS),
        help="Variant codes to include.",
    )
    parser.add_argument("--lookahead-bars", type=int, default=10)
    parser.add_argument("--target-up-pct", type=float, default=2.0)
    parser.add_argument("--stop-down-pct", type=float, default=1.0)
    parser.add_argument("--common-overrides", default="")
    parser.add_argument("--regular-overrides", default="")
    parser.add_argument("--reclaim-overrides", default="")
    parser.add_argument("--retest-overrides", default="")
    parser.add_argument(
        "--output",
        type=Path,
        default=SCRIPT_DIR.parent / "tmp_replay" / "compare_30s_research_family.json",
    )
    return parser.parse_args()


def _load_source_bars(db_url: str, strategy_code: str, symbol: str, day: str) -> list[review.BarRow]:
    engine = create_engine(db_url)
    start_utc, end_utc = review._et_window(date.fromisoformat(day))
    try:
        with Session(engine) as session:
            return review._load_bars(
                session,
                strategy_code=strategy_code,
                symbol=symbol,
                start_utc=start_utc,
                end_utc=end_utc,
            )
    finally:
        engine.dispose()


def _variant_config(
    variant_code: str,
    *,
    common_overrides: dict[str, object],
    regular_overrides: dict[str, object],
    reclaim_overrides: dict[str, object],
    retest_overrides: dict[str, object],
) -> TradingConfig:
    return compare._variant_config(
        variant_code,
        common_overrides=common_overrides,
        regular_overrides=regular_overrides,
        probe_overrides={},
        reclaim_overrides=reclaim_overrides,
        retest_overrides=retest_overrides,
    )


def main() -> None:
    args = _parse_args()
    variants = [code for code in args.variants if code in set(compare.VARIANT_CODES)]
    if not variants:
        raise SystemExit("No valid variants requested.")

    common_overrides = compare._parse_json_object(args.common_overrides)
    regular_overrides = compare._parse_json_object(args.regular_overrides)
    reclaim_overrides = compare._parse_json_object(args.reclaim_overrides)
    retest_overrides = compare._parse_json_object(args.retest_overrides)
    research_defaults_applied: dict[str, object] = {}
    if "ticker_loss_pause_streak_limit" not in common_overrides:
        common_overrides["ticker_loss_pause_streak_limit"] = 0
        research_defaults_applied["ticker_loss_pause_streak_limit"] = 0
    if "ticker_loss_pause_minutes" not in common_overrides:
        common_overrides["ticker_loss_pause_minutes"] = 0
        research_defaults_applied["ticker_loss_pause_minutes"] = 0

    universe = diagnostics.get_reclaim_universe(args.universe)
    aggregate: dict[str, Counter[str]] = {variant: Counter() for variant in variants}
    per_symbol: dict[str, list[dict[str, object]]] = defaultdict(list)

    for db_url, source_strategy, symbol, day in universe:
        bars = _load_source_bars(db_url, source_strategy, symbol, day)
        if not bars:
            continue
        for variant in variants:
            config = _variant_config(
                variant,
                common_overrides=common_overrides,
                regular_overrides=regular_overrides,
                reclaim_overrides=reclaim_overrides,
                retest_overrides=retest_overrides,
            )
            result = compare._run_variant(
                bars=bars,
                symbol=symbol,
                variant_code=variant,
                config=config,
                lookahead_bars=args.lookahead_bars,
                target_up_pct=args.target_up_pct,
                stop_down_pct=args.stop_down_pct,
            )
            aggregate[variant]["symbols"] += 1
            aggregate[variant]["bars"] += int(result["bars"])
            aggregate[variant]["intents"] += int(result["intents"])
            aggregate[variant]["review_markers"] += int(result["review_markers"])
            aggregate[variant]["taken_good"] += int(result["taken_good"])
            aggregate[variant]["taken_bad"] += int(result["taken_bad"])
            aggregate[variant]["taken_open"] += int(result["taken_open"])
            for status, count in result["decision_status_counts"].items():
                aggregate[variant][f"decision_status:{status}"] += int(count)
            for intent_type, count in result["intent_counts"].items():
                aggregate[variant][f"intent_type:{intent_type}"] += int(count)
            per_symbol[variant].append(
                {
                    "date": day,
                    "symbol": symbol,
                    **result,
                }
            )

    summary: list[dict[str, object]] = []
    for variant in variants:
        taken_good = int(aggregate[variant]["taken_good"])
        taken_bad = int(aggregate[variant]["taken_bad"])
        summary.append(
            {
                "variant": variant,
                "universe": args.universe,
                "symbols": int(aggregate[variant]["symbols"]),
                "bars": int(aggregate[variant]["bars"]),
                "intents": int(aggregate[variant]["intents"]),
                "review_markers": int(aggregate[variant]["review_markers"]),
                "taken_good": taken_good,
                "taken_bad": int(aggregate[variant]["taken_bad"]),
                "taken_open": int(aggregate[variant]["taken_open"]),
                "good_minus_bad": taken_good - taken_bad,
                "resolved_good_rate": round(taken_good / max(1, taken_good + taken_bad), 4),
                "decision_status_counts": {
                    key.split(":", 1)[1]: int(value)
                    for key, value in aggregate[variant].items()
                    if key.startswith("decision_status:")
                },
                "intent_type_counts": {
                    key.split(":", 1)[1]: int(value)
                    for key, value in aggregate[variant].items()
                    if key.startswith("intent_type:")
                },
            }
        )

    payload = {
        "universe": args.universe,
        "variants": summary,
        "per_symbol": dict(per_symbol),
        "overrides": {
            "common": common_overrides,
            "regular": regular_overrides,
            "reclaim": reclaim_overrides,
            "retest": retest_overrides,
        },
        "research_defaults_applied": research_defaults_applied,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
