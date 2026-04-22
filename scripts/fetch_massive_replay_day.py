from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import sys

from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import StrategyBarHistory
from project_mai_tai.market_data.massive_provider import MassiveSnapshotProvider
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch historical 30s bars from Massive/Polygon and store them as replay-ready strategy_bar_history rows.",
    )
    parser.add_argument("--symbol", action="append", required=True, help="Symbol to fetch. Repeat for multiple symbols.")
    parser.add_argument("--date", required=True, help="ET trading day in YYYY-MM-DD.")
    parser.add_argument("--interval-secs", type=int, default=30)
    parser.add_argument("--strategy-code", default="massive_import")
    parser.add_argument("--db-path", type=Path, required=True, help="Output sqlite path.")
    parser.add_argument("--api-key", default="", help="Optional Massive API key. Falls back to MAI_TAI_MASSIVE_API_KEY.")
    parser.add_argument("--limit", type=int, default=50_000)
    parser.add_argument("--keep-outside-hours", action="store_true")
    return parser.parse_args()


def _resolve_api_key(raw: str) -> str:
    api_key = raw.strip() or os.getenv("MAI_TAI_MASSIVE_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Massive API key is required. Set MAI_TAI_MASSIVE_API_KEY or pass --api-key.")
    return api_key


def _fetch_day_bars(
    *,
    api_key: str,
    symbol: str,
    target_day: date,
    interval_secs: int,
    limit: int,
) -> list[dict[str, object]]:
    provider = MassiveSnapshotProvider(api_key)
    client = provider._get_rest_client()
    multiplier, timespan = provider._resolve_agg_interval(interval_secs)
    from_date = target_day.strftime("%Y-%m-%d")
    to_date = (target_day + timedelta(days=1)).strftime("%Y-%m-%d")
    results: list[dict[str, object]] = []
    for agg in client.list_aggs(
        symbol.upper(),
        multiplier,
        timespan,
        from_=from_date,
        to=to_date,
        limit=limit,
    ):
        timestamp_raw = getattr(agg, "timestamp", None)
        close = getattr(agg, "close", None)
        if timestamp_raw is None or close is None:
            continue
        timestamp = timestamp_raw / 1000 if timestamp_raw > 1_000_000_000_000 else float(timestamp_raw)
        bar_time = datetime.fromtimestamp(timestamp, tz=UTC)
        results.append(
            {
                "bar_time": bar_time,
                "open_price": Decimal(str(getattr(agg, "open", close) or close)),
                "high_price": Decimal(str(getattr(agg, "high", close) or close)),
                "low_price": Decimal(str(getattr(agg, "low", close) or close)),
                "close_price": Decimal(str(close)),
                "volume": int(getattr(agg, "volume", 0) or 0),
                "trade_count": int(
                    getattr(agg, "transactions", None)
                    or getattr(agg, "trade_count", None)
                    or 1
                ),
            }
        )
    return results


def _filter_bars_to_day(
    bars: list[dict[str, object]],
    *,
    target_day: date,
    keep_outside_hours: bool,
) -> list[dict[str, object]]:
    filtered: list[dict[str, object]] = []
    for bar in bars:
        bar_time = bar["bar_time"]
        if not isinstance(bar_time, datetime):
            continue
        eastern_time = bar_time.astimezone(EASTERN_TZ)
        if eastern_time.date() != target_day:
            continue
        if not keep_outside_hours and not (4 <= eastern_time.hour < 20):
            continue
        filtered.append(bar)
    filtered.sort(key=lambda item: item["bar_time"])
    return filtered


def _write_symbol_rows(
    session: Session,
    *,
    strategy_code: str,
    symbol: str,
    interval_secs: int,
    bars: list[dict[str, object]],
) -> int:
    session.execute(
        delete(StrategyBarHistory).where(
            StrategyBarHistory.strategy_code == strategy_code,
            StrategyBarHistory.symbol == symbol,
            StrategyBarHistory.interval_secs == interval_secs,
        )
    )
    for bar in bars:
        session.add(
            StrategyBarHistory(
                strategy_code=strategy_code,
                symbol=symbol,
                interval_secs=interval_secs,
                bar_time=bar["bar_time"],
                open_price=bar["open_price"],
                high_price=bar["high_price"],
                low_price=bar["low_price"],
                close_price=bar["close_price"],
                volume=int(bar["volume"]),
                trade_count=int(bar["trade_count"]),
                position_state="flat",
                position_quantity=0,
                decision_status="source",
                decision_reason="massive_import",
                decision_path="",
                decision_score="",
                decision_score_details="",
                indicators_json={
                    "trade_count": int(bar["trade_count"]),
                    "source": "massive_import",
                },
            )
        )
    return len(bars)


def main() -> None:
    args = _parse_args()
    api_key = _resolve_api_key(args.api_key)
    target_day = date.fromisoformat(args.date)
    args.db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(f"sqlite+pysqlite:///{args.db_path}")
    Base.metadata.create_all(engine)

    imported: list[dict[str, object]] = []
    with Session(engine) as session:
        for raw_symbol in args.symbol:
            symbol = raw_symbol.upper()
            fetched = _fetch_day_bars(
                api_key=api_key,
                symbol=symbol,
                target_day=target_day,
                interval_secs=args.interval_secs,
                limit=args.limit,
            )
            bars = _filter_bars_to_day(
                fetched,
                target_day=target_day,
                keep_outside_hours=args.keep_outside_hours,
            )
            count = _write_symbol_rows(
                session,
                strategy_code=args.strategy_code,
                symbol=symbol,
                interval_secs=args.interval_secs,
                bars=bars,
            )
            imported.append(
                {
                    "symbol": symbol,
                    "bars": count,
                    "first_bar": bars[0]["bar_time"].isoformat() if bars else "",
                    "last_bar": bars[-1]["bar_time"].isoformat() if bars else "",
                }
            )
        session.commit()

    print(
        json.dumps(
            {
                "db_path": str(args.db_path),
                "strategy_code": args.strategy_code,
                "date": args.date,
                "interval_secs": args.interval_secs,
                "imported": imported,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
