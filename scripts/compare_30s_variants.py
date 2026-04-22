from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
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

VARIANT_CODES = ("macd_30s", "macd_30s_probe", "macd_30s_reclaim", "macd_30s_retest")


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare regular/probe/reclaim 30s variants on stored bars.")
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--source-strategy", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--date", required=True, help="ET date in YYYY-MM-DD")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--lookahead-bars", type=int, default=10)
    parser.add_argument("--target-up-pct", type=float, default=2.0)
    parser.add_argument("--stop-down-pct", type=float, default=1.0)
    parser.add_argument("--common-overrides", default="")
    parser.add_argument("--regular-overrides", default="")
    parser.add_argument("--probe-overrides", default="")
    parser.add_argument("--reclaim-overrides", default="")
    parser.add_argument("--retest-overrides", default="")
    return parser.parse_args()


def _parse_json_object(raw_value: str) -> dict[str, object]:
    text = raw_value.strip()
    if not text:
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Override JSON must decode to an object")
    return dict(parsed)


def _apply_overrides(config: TradingConfig, *override_sets: dict[str, object]) -> TradingConfig:
    fields = asdict(config)
    valid_fields = set(fields)
    for overrides in override_sets:
        for field, value in overrides.items():
            if field in valid_fields:
                fields[field] = value
    return TradingConfig(**fields)


def _variant_config(
    variant_code: str,
    *,
    common_overrides: dict[str, object],
    regular_overrides: dict[str, object],
    probe_overrides: dict[str, object],
    reclaim_overrides: dict[str, object],
    retest_overrides: dict[str, object],
) -> TradingConfig:
    base = TradingConfig()
    if variant_code == "macd_30s":
        config = base.make_30s_variant(quantity=100)
        variant_overrides = regular_overrides
    elif variant_code == "macd_30s_probe":
        config = base.make_30s_pretrigger_variant(quantity=100)
        variant_overrides = probe_overrides
    elif variant_code == "macd_30s_reclaim":
        config = base.make_30s_reclaim_variant(quantity=100)
        variant_overrides = reclaim_overrides
    elif variant_code == "macd_30s_retest":
        config = base.make_30s_retest_variant(quantity=100)
        variant_overrides = retest_overrides
    else:
        raise ValueError(f"Unsupported variant: {variant_code}")
    return _apply_overrides(config, common_overrides, variant_overrides)


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


def _create_session_factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def _run_variant(
    *,
    bars: list[review.BarRow],
    symbol: str,
    variant_code: str,
    config: TradingConfig,
    lookahead_bars: int,
    target_up_pct: float,
    stop_down_pct: float,
) -> dict[str, object]:
    runtime_code = f"analysis_{variant_code}_{symbol.lower()}"
    session_factory, engine = _create_session_factory()
    try:
        with session_factory() as session:
            strategy = Strategy(
                code=runtime_code,
                name=runtime_code,
                execution_mode="shadow",
                metadata_json={"replay_variant": variant_code},
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

        now_holder = {"value": bars[0].bar_time.astimezone(EASTERN_TZ)}

        def _now_provider() -> datetime:
            return now_holder["value"]

        runtime = StrategyBotRuntime(
            StrategyDefinition(
                code=runtime_code,
                display_name=variant_code,
                account_name=f"replay:{runtime_code}",
                interval_secs=30,
                trading_config=config,
                indicator_config=IndicatorConfig(),
            ),
            now_provider=_now_provider,
            session_factory=session_factory,
            use_live_aggregate_bars=True,
            live_aggregate_fallback_enabled=False,
        )
        runtime.set_watchlist([symbol])

        fill_counter = 0

        def _apply_intents(intents, event_time: datetime, fill_price: float) -> None:
            nonlocal fill_counter
            for intent in intents:
                payload = intent.payload
                _persist_replay_intent(
                    session_factory=session_factory,
                    strategy_id=strategy_id,
                    broker_account_id=broker_account_id,
                    payload=payload,
                    created_at=event_time,
                )
                fill_counter += 1
                runtime.apply_execution_fill(
                    client_order_id=f"replay-{runtime_code}-{fill_counter}",
                    symbol=str(payload.symbol),
                    intent_type=str(payload.intent_type),
                    status="filled",
                    side=str(payload.side),
                    quantity=Decimal(str(payload.quantity)),
                    price=Decimal(str(fill_price)),
                    path=str(payload.metadata.get("path", "")),
                )

        for bar in bars:
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
            _apply_intents(intents, bar.bar_time, bar.close_price)

        now_holder["value"] = (bars[-1].bar_time + timedelta(seconds=30)).astimezone(EASTERN_TZ)
        final_intents, _completed = runtime.flush_completed_bars()
        _apply_intents(final_intents, bars[-1].bar_time + timedelta(seconds=30), bars[-1].close_price)

        start_utc = bars[0].bar_time.astimezone(UTC) - timedelta(minutes=1)
        end_utc = bars[-1].bar_time.astimezone(UTC) + timedelta(minutes=1)
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

        for replay_bar in replay_bars:
            replay_bar.bar_time = _ensure_utc(replay_bar.bar_time)
        for intent in intents:
            intent.created_at = _ensure_utc(intent.created_at)

        review_markers = review._future_review(
            replay_bars,
            lookahead_bars=lookahead_bars,
            target_up_pct=target_up_pct,
            stop_down_pct=stop_down_pct,
        )
        actual_outcomes = review._classify_actual_outcomes(
            replay_bars,
            intents,
            lookahead_bars=lookahead_bars,
            target_up_pct=target_up_pct,
            stop_down_pct=stop_down_pct,
        )

        decision_status_counts = Counter(bar.decision_status or "none" for bar in replay_bars)
        blocked_reasons = Counter(
            (bar.decision_reason or "").strip()
            for bar in replay_bars
            if (bar.decision_status or "").strip() == "blocked" and (bar.decision_reason or "").strip()
        )
        intent_type_counts = Counter(intent.intent_type for intent in intents)
        return {
            "variant": variant_code,
            "bars": len(replay_bars),
            "intents": len(intents),
            "intent_counts": dict(sorted(intent_type_counts.items())),
            "review_markers": len(review_markers),
            "taken_good": sum(1 for item in actual_outcomes if item.category == "taken_good"),
            "taken_bad": sum(1 for item in actual_outcomes if item.category == "taken_bad"),
            "taken_open": sum(1 for item in actual_outcomes if item.category == "taken_open"),
            "decision_status_counts": dict(sorted(decision_status_counts.items())),
            "top_blocked_reasons": blocked_reasons.most_common(8),
        }
    finally:
        engine.dispose()


def main() -> None:
    args = _parse_args()
    target_day = date.fromisoformat(args.date)
    start_utc, end_utc = review._et_window(target_day)
    source_engine = create_engine(args.db_url)
    try:
        with Session(source_engine) as session:
            source_bars = review._load_bars(
                session,
                strategy_code=args.source_strategy,
                symbol=args.symbol.upper(),
                start_utc=start_utc,
                end_utc=end_utc,
            )
    finally:
        source_engine.dispose()

    if not source_bars:
        raise SystemExit(f"No bars found for {args.source_strategy} {args.symbol} on {args.date}")

    common_overrides = _parse_json_object(args.common_overrides)
    regular_overrides = _parse_json_object(args.regular_overrides)
    probe_overrides = _parse_json_object(args.probe_overrides)
    reclaim_overrides = _parse_json_object(args.reclaim_overrides)
    retest_overrides = _parse_json_object(args.retest_overrides)

    results = []
    for variant_code in VARIANT_CODES:
        config = _variant_config(
            variant_code,
            common_overrides=common_overrides,
            regular_overrides=regular_overrides,
            probe_overrides=probe_overrides,
            reclaim_overrides=reclaim_overrides,
            retest_overrides=retest_overrides,
        )
        results.append(
            _run_variant(
                bars=source_bars,
                symbol=args.symbol.upper(),
                variant_code=variant_code,
                config=config,
                lookahead_bars=args.lookahead_bars,
                target_up_pct=args.target_up_pct,
                stop_down_pct=args.stop_down_pct,
            )
        )

    payload = {
        "source_db_url": args.db_url,
        "source_strategy": args.source_strategy,
        "symbol": args.symbol.upper(),
        "date": args.date,
        "source_bars": len(source_bars),
        "variants": results,
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
