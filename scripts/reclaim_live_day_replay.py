from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import compare_30s_variants as compare
import render_live_day_review as review

from project_mai_tai.db.models import StrategyBarHistory
from project_mai_tai.strategy_core.trading_config import TradingConfig


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay current reclaim rules on a live reclaim day.")
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--date", required=True, help="ET date in YYYY-MM-DD")
    parser.add_argument("--source-strategy", default="macd_30s_reclaim")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--lookahead-bars", type=int, default=10)
    parser.add_argument("--target-up-pct", type=float, default=2.0)
    parser.add_argument("--stop-down-pct", type=float, default=1.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tmp_replay/reclaim_live_day_replay.json"),
    )
    return parser.parse_args()


def _build_reclaim_config() -> TradingConfig:
    return TradingConfig().make_30s_reclaim_variant(quantity=100)


def _load_symbols(
    session: Session,
    *,
    strategy_code: str,
    start_utc,
    end_utc,
) -> list[str]:
    rows = session.execute(
        select(StrategyBarHistory.symbol)
        .where(
            StrategyBarHistory.strategy_code == strategy_code,
            StrategyBarHistory.bar_time >= start_utc,
            StrategyBarHistory.bar_time < end_utc,
        )
        .distinct()
        .order_by(StrategyBarHistory.symbol.asc())
    ).all()
    return [str(row.symbol).upper() for row in rows]


def main() -> None:
    args = _parse_args()
    target_day = date.fromisoformat(args.date)
    start_utc, end_utc = review._et_window(target_day)
    engine = create_engine(args.db_url)
    try:
        with Session(engine) as session:
            symbols = [symbol.upper() for symbol in (args.symbols or [])] or _load_symbols(
                session,
                strategy_code=args.source_strategy,
                start_utc=start_utc,
                end_utc=end_utc,
            )
            bars_by_symbol = {
                symbol: review._load_bars(
                    session,
                    strategy_code=args.source_strategy,
                    symbol=symbol,
                    start_utc=start_utc,
                    end_utc=end_utc,
                )
                for symbol in symbols
            }
    finally:
        engine.dispose()

    config = _build_reclaim_config()
    aggregate: Counter[str] = Counter()
    per_symbol: list[dict[str, object]] = []

    for symbol in symbols:
        bars = bars_by_symbol.get(symbol, [])
        if not bars:
            continue
        result = compare._run_variant(
            bars=bars,
            symbol=symbol,
            variant_code="macd_30s_reclaim",
            config=config,
            lookahead_bars=args.lookahead_bars,
            target_up_pct=args.target_up_pct,
            stop_down_pct=args.stop_down_pct,
        )
        aggregate["symbols"] += 1
        aggregate["taken_good"] += int(result["taken_good"])
        aggregate["taken_bad"] += int(result["taken_bad"])
        aggregate["taken_open"] += int(result["taken_open"])
        aggregate["intents"] += int(result["intents"])
        per_symbol.append(
            {
                "symbol": symbol,
                "bars": int(result["bars"]),
                "intents": int(result["intents"]),
                "taken_good": int(result["taken_good"]),
                "taken_bad": int(result["taken_bad"]),
                "taken_open": int(result["taken_open"]),
                "decision_status_counts": result["decision_status_counts"],
                "top_blocked_reasons": result["top_blocked_reasons"],
            }
        )

    payload = {
        "date": args.date,
        "source_strategy": args.source_strategy,
        "config": asdict(config),
        "summary": {
            "symbols": int(aggregate["symbols"]),
            "taken_good": int(aggregate["taken_good"]),
            "taken_bad": int(aggregate["taken_bad"]),
            "taken_open": int(aggregate["taken_open"]),
            "intents": int(aggregate["intents"]),
            "resolved_good_rate": round(
                aggregate["taken_good"] / max(1, aggregate["taken_good"] + aggregate["taken_bad"]),
                4,
            ),
        },
        "per_symbol": per_symbol,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
