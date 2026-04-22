from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sys
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import render_live_day_review as review

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, Strategy, TradeIntent
from project_mai_tai.services.strategy_engine_app import StrategyBotRuntime, StrategyDefinition
from project_mai_tai.strategy_core.config import IndicatorConfig
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ
from project_mai_tai.strategy_core.trading_config import TradingConfig


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay a strategy variant from stored live bars and render a review chart.")
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--source-strategy", default="macd_30s")
    parser.add_argument("--variant", default="macd_30s_probe")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--date", required=True, help="ET date in YYYY-MM-DD")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--lookahead-bars", type=int, default=10)
    parser.add_argument("--target-up-pct", type=float, default=2.0)
    parser.add_argument("--stop-down-pct", type=float, default=1.0)
    return parser.parse_args()


def _persist_replay_intent(
    *,
    session_factory: sessionmaker[Session],
    strategy_id,
    broker_account_id,
    payload,
    created_at: datetime,
) -> None:
    stage = str(payload.metadata.get("entry_stage", "") or "")
    intent_type = str(payload.intent_type)
    if intent_type == "open" and stage == "confirm_add":
        intent_type = "add"
    with session_factory() as session:
        session.add(
            TradeIntent(
                strategy_id=strategy_id,
                broker_account_id=broker_account_id,
                symbol=str(payload.symbol),
                side=str(payload.side),
                intent_type=intent_type,
                quantity=Decimal(str(payload.quantity)),
                reason=str(payload.reason),
                status="filled",
                payload=dict(payload.metadata),
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.commit()


def main() -> None:
    args = _parse_args()
    target_day = date.fromisoformat(args.date)
    start_utc, end_utc = review._et_window(target_day)
    source_engine = create_engine(args.db_url)

    with Session(source_engine) as session:
        source_bars = review._load_bars(
            session,
            strategy_code=args.source_strategy,
            symbol=args.symbol.upper(),
            start_utc=start_utc,
            end_utc=end_utc,
        )

    if not source_bars:
        raise SystemExit(f"No bars found for {args.source_strategy} {args.symbol} on {args.date}")

    replay_db_path = args.output.with_suffix(".sqlite")
    if replay_db_path.exists():
        replay_db_path.unlink()
    replay_engine = create_engine(f"sqlite+pysqlite:///{replay_db_path}")
    Base.metadata.create_all(replay_engine)
    replay_session_factory = sessionmaker(bind=replay_engine, expire_on_commit=False)

    with replay_session_factory() as session:
        strategy = Strategy(
            code=args.variant,
            name=args.variant,
            execution_mode="shadow",
            metadata_json={"replay_source": args.source_strategy},
        )
        account = BrokerAccount(
            name=f"replay:{args.variant}",
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

    def _now_provider() -> datetime:
        return now_holder["value"]

    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code=args.variant,
            display_name=args.variant,
            account_name=f"replay:{args.variant}",
            interval_secs=30,
            trading_config=TradingConfig().make_30s_pretrigger_variant(quantity=100),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=_now_provider,
        session_factory=replay_session_factory,
        use_live_aggregate_bars=True,
    )
    runtime.set_watchlist([args.symbol.upper()])

    fill_counter = 0

    def _apply_intents(intents, event_time: datetime, fill_price: float) -> None:
        nonlocal fill_counter
        for intent in intents:
            payload = intent.payload
            _persist_replay_intent(
                session_factory=replay_session_factory,
                strategy_id=strategy_id,
                broker_account_id=broker_account_id,
                payload=payload,
                created_at=event_time,
            )
            fill_counter += 1
            runtime.apply_execution_fill(
                client_order_id=f"replay-{fill_counter}",
                symbol=str(payload.symbol),
                intent_type=str(payload.intent_type),
                status="filled",
                side=str(payload.side),
                quantity=Decimal(str(payload.quantity)),
                price=Decimal(str(fill_price)),
                path=str(payload.metadata.get("path", "")),
            )

    for bar in source_bars:
        now_holder["value"] = bar.bar_time.astimezone(EASTERN_TZ)
        intents = runtime.handle_live_bar(
            symbol=args.symbol.upper(),
            open_price=bar.open_price,
            high_price=bar.high_price,
            low_price=bar.low_price,
            close_price=bar.close_price,
            volume=bar.volume,
            timestamp=bar.bar_time.timestamp(),
            trade_count=1,
        )
        _apply_intents(intents, bar.bar_time, bar.close_price)

    now_holder["value"] = (source_bars[-1].bar_time + timedelta(seconds=30)).astimezone(EASTERN_TZ)
    final_intents, _completed = runtime.flush_completed_bars()
    _apply_intents(final_intents, source_bars[-1].bar_time + timedelta(seconds=30), source_bars[-1].close_price)

    with replay_session_factory() as session:
        bars = review._load_bars(
            session,
            strategy_code=args.variant,
            symbol=args.symbol.upper(),
            start_utc=start_utc,
            end_utc=end_utc,
        )
        intents = review._load_intents(
            session,
            strategy_code=args.variant,
            symbol=args.symbol.upper(),
            start_utc=start_utc,
            end_utc=end_utc,
        )
    for bar in bars:
        bar.bar_time = _ensure_utc(bar.bar_time)
    for intent in intents:
        intent.created_at = _ensure_utc(intent.created_at)

    review_markers = review._future_review(
        bars,
        lookahead_bars=args.lookahead_bars,
        target_up_pct=args.target_up_pct,
        stop_down_pct=args.stop_down_pct,
    )
    actual_outcomes = review._classify_actual_outcomes(
        bars,
        intents,
        lookahead_bars=args.lookahead_bars,
        target_up_pct=args.target_up_pct,
        stop_down_pct=args.stop_down_pct,
    )
    html_text = review._render_chart(
        strategy_code=args.variant,
        symbol=args.symbol.upper(),
        bars=bars,
        intents=intents,
        review_markers=review_markers,
        actual_outcomes=actual_outcomes,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html_text, encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "bars": len(bars),
                "intents": len(intents),
                "review_markers": len(review_markers),
                "taken_good": sum(1 for item in actual_outcomes if item.category == "taken_good"),
                "taken_bad": sum(1 for item in actual_outcomes if item.category == "taken_bad"),
                "taken_open": sum(1 for item in actual_outcomes if item.category == "taken_open"),
            }
        )
    )


if __name__ == "__main__":
    main()
