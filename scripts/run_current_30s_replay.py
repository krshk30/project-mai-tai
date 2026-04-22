from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MethodType

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import render_live_day_review as review

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, Strategy, TradeIntent
from project_mai_tai.services.strategy_engine_app import StrategyBotRuntime, StrategyDefinition
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.config import IndicatorConfig
from project_mai_tai.strategy_core.models import OHLCVBar
from project_mai_tai.strategy_core.schwab_native_30s import (
    SchwabNativeBarBuilderManager,
    SchwabNativeEntryEngine,
    SchwabNativeIndicatorEngine,
)
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ
from project_mai_tai.strategy_core.trading_config import TradingConfig


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay the current Schwab-native macd_30s bot on stored 30s bars.")
    parser.add_argument("--db-url", required=True, help="Source database URL containing strategy_bar_history rows.")
    parser.add_argument("--source-strategy", required=True, help="Source strategy_code to read bars from.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--date", required=True, help="ET date in YYYY-MM-DD format.")
    parser.add_argument("--env-file", default="", help="Optional KEY=VALUE env file used for config overrides.")
    parser.add_argument("--output", type=Path, default=None, help="Optional JSON output path.")
    return parser.parse_args()


def _load_env_file(path_text: str) -> None:
    path = Path(path_text)
    if not path.exists():
        raise SystemExit(f"Env file not found: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


def _create_session_factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def _apply_trading_config_overrides(
    config: TradingConfig,
    raw_overrides: str,
    *,
    settings: Settings,
) -> TradingConfig:
    if not raw_overrides.strip():
        return config
    overrides = settings.parse_strategy_config_overrides(raw_overrides)
    if not overrides:
        return config
    valid_fields = set(TradingConfig.__dataclass_fields__)
    fields = dict(config.__dict__)
    for field, value in overrides.items():
        if field in valid_fields:
            fields[field] = value
    return TradingConfig(**fields)


def _build_live_like_trading_config(settings: Settings) -> TradingConfig:
    config = TradingConfig().make_30s_schwab_native_variant(quantity=100)
    config = _apply_trading_config_overrides(
        config,
        settings.strategy_macd_30s_common_config_overrides_json,
        settings=settings,
    )
    config = _apply_trading_config_overrides(
        config,
        settings.strategy_macd_30s_config_overrides_json,
        settings=settings,
    )
    return config


def _persist_replay_intent(
    *,
    session_factory: sessionmaker[Session],
    strategy_id,
    broker_account_id,
    payload,
    created_at: datetime,
) -> None:
    intent_type = str(payload.intent_type)
    stage = str(payload.metadata.get("entry_stage", "") or "")
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


def _format_et(ts: datetime) -> str:
    current = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return current.astimezone(EASTERN_TZ).strftime("%Y-%m-%d %I:%M:%S %p ET")


def main() -> None:
    args = _parse_args()
    if args.env_file.strip():
        _load_env_file(args.env_file)

    settings = Settings()
    symbol = args.symbol.upper()
    target_day = date.fromisoformat(args.date)
    start_utc, end_utc = review._et_window(target_day)

    source_engine = create_engine(args.db_url)
    try:
        with Session(source_engine) as source_session:
            source_bars = review._load_bars(
                source_session,
                strategy_code=args.source_strategy,
                symbol=symbol,
                start_utc=start_utc,
                end_utc=end_utc,
            )
    finally:
        source_engine.dispose()

    if not source_bars:
        raise SystemExit(f"No bars found for {args.source_strategy} {symbol} on {args.date}")

    session_factory, engine = _create_session_factory()
    try:
        runtime_code = f"analysis_macd_30s_{symbol.lower()}_{args.date.replace('-', '')}"
        with session_factory() as session:
            strategy = Strategy(
                code=runtime_code,
                name=runtime_code,
                execution_mode="shadow",
                metadata_json={"replay_variant": "macd_30s"},
            )
            account = BrokerAccount(
                name=f"replay:{runtime_code}",
                provider="replay",
                environment="analysis",
                external_account_id=None,
            )
            session.add_all([strategy, account])
            session.commit()
            session.refresh(strategy)
            session.refresh(account)
            strategy_id = strategy.id
            broker_account_id = account.id

        trading_config = _build_live_like_trading_config(settings)
        indicator_config = IndicatorConfig()

        now_holder = {"value": source_bars[0].bar_time.astimezone(EASTERN_TZ)}

        def _now_provider() -> datetime:
            return now_holder["value"]

        runtime = StrategyBotRuntime(
            StrategyDefinition(
                code=runtime_code,
                display_name="MACD Bot",
                account_name="paper:macd_30s",
                interval_secs=30,
                trading_config=trading_config,
                indicator_config=indicator_config,
            ),
            now_provider=_now_provider,
            session_factory=session_factory,
            use_live_aggregate_bars=False,
            live_aggregate_fallback_enabled=False,
            builder_manager=SchwabNativeBarBuilderManager(
                interval_secs=30,
                time_provider=lambda: _now_provider().timestamp(),
            ),
            indicator_engine=SchwabNativeIndicatorEngine(indicator_config),
            entry_engine=SchwabNativeEntryEngine(
                trading_config,
                name="MACD Bot",
                now_provider=_now_provider,
            ),
        )
        runtime.set_watchlist([symbol])
        original_decorate_indicators = runtime._decorate_indicators

        def _compat_decorate_indicators(self, replay_symbol: str, trading_indicators: dict[str, float | bool]) -> dict[str, object]:
            indicators = original_decorate_indicators(replay_symbol, trading_indicators)
            stoch_k = float(indicators.get("stoch_k", 0) or 0)
            stoch_k_prev = float(indicators.get("stoch_k_prev", stoch_k) or stoch_k)
            indicators.setdefault(
                "stoch_k_below_exit",
                bool(indicators.get("stoch_cross_below_exit", False))
                or stoch_k < float(indicator_config.stoch_exit_level),
            )
            indicators.setdefault("stoch_k_falling", stoch_k < stoch_k_prev)
            indicators.setdefault("stoch_k_prev2", stoch_k_prev)
            return indicators

        runtime._decorate_indicators = MethodType(_compat_decorate_indicators, runtime)

        event_log: list[dict[str, object]] = []

        def _apply_intents(intents, event_time: datetime, fill_price: float) -> None:
            for intent in intents:
                payload = intent.payload
                _persist_replay_intent(
                    session_factory=session_factory,
                    strategy_id=strategy_id,
                    broker_account_id=broker_account_id,
                    payload=payload,
                    created_at=event_time,
                )
                event_log.append(
                    {
                        "time": _format_et(event_time),
                        "time_raw": event_time.isoformat(),
                        "symbol": str(payload.symbol),
                        "side": str(payload.side),
                        "intent_type": str(payload.intent_type),
                        "reason": str(payload.reason),
                        "quantity": int(payload.quantity),
                        "price": round(float(fill_price), 4),
                        "path": str(payload.metadata.get("path", "") or ""),
                        "entry_stage": str(payload.metadata.get("entry_stage", "") or ""),
                    }
                )
                runtime.apply_execution_fill(
                    client_order_id=f"replay-{runtime_code}-{len(event_log)}",
                    symbol=str(payload.symbol),
                    intent_type=str(payload.intent_type),
                    status="filled",
                    side=str(payload.side),
                    quantity=Decimal(str(payload.quantity)),
                    price=Decimal(str(fill_price)),
                    path=str(payload.metadata.get("path", "")),
                    reason=str(payload.reason),
                )

        for bar in source_bars:
            completed_at = bar.bar_time + timedelta(seconds=30)
            now_holder["value"] = completed_at.astimezone(EASTERN_TZ)
            builder = runtime.builder_manager.get_or_create(symbol)
            builder.bars.append(
                OHLCVBar(
                    open=bar.open_price,
                    high=bar.high_price,
                    low=bar.low_price,
                    close=bar.close_price,
                    volume=bar.volume,
                    timestamp=bar.bar_time.timestamp(),
                    trade_count=int(bar.indicators.get("trade_count", 1) if isinstance(bar.indicators, dict) else 1),
                )
            )
            builder._bar_count += 1
            builder._trim_history()
            intents = runtime._evaluate_completed_bar(symbol)
            _apply_intents(intents, completed_at, bar.close_price)

        closed_trades = runtime.positions.get_closed_today()
        open_positions = runtime.positions.get_all_positions()
        last_close = float(source_bars[-1].close_price)

        open_mark_to_market: list[dict[str, object]] = []
        for position in open_positions:
            quantity = int(position.get("quantity", 0) or 0)
            entry_price = float(position.get("entry_price", 0) or 0)
            current_price = last_close
            unrealized = round((current_price - entry_price) * quantity, 2)
            open_mark_to_market.append(
                {
                    "ticker": str(position.get("ticker", symbol)),
                    "entry_price": round(entry_price, 4),
                    "last_price": round(current_price, 4),
                    "quantity": quantity,
                    "unrealized_pnl": unrealized,
                    "current_profit_pct": round(((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0.0, 2),
                    "path": str(position.get("path", "") or ""),
                }
            )

        wins = sum(1 for trade in closed_trades if float(trade.get("pnl", 0) or 0) > 0)
        losses = sum(1 for trade in closed_trades if float(trade.get("pnl", 0) or 0) < 0)
        flat = sum(1 for trade in closed_trades if float(trade.get("pnl", 0) or 0) == 0)

        payload = {
            "symbol": symbol,
            "date": args.date,
            "source_strategy": args.source_strategy,
            "bar_count": len(source_bars),
            "first_bar": source_bars[0].bar_time.isoformat(),
            "last_bar": source_bars[-1].bar_time.isoformat(),
            "config": asdict(trading_config),
            "summary": {
                "closed_trades": len(closed_trades),
                "wins": wins,
                "losses": losses,
                "flat": flat,
                "realized_pnl": round(sum(float(trade.get("pnl", 0) or 0) for trade in closed_trades), 2),
                "open_positions": len(open_positions),
                "open_mark_to_market_pnl": round(sum(float(item["unrealized_pnl"]) for item in open_mark_to_market), 2),
                "mark_to_market_total_pnl": round(
                    sum(float(trade.get("pnl", 0) or 0) for trade in closed_trades)
                    + sum(float(item["unrealized_pnl"]) for item in open_mark_to_market),
                    2,
                ),
                "intent_count": len(event_log),
            },
            "closed_trades": closed_trades,
            "open_mark_to_market": open_mark_to_market,
            "events": event_log,
        }

        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        print(json.dumps(payload, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
