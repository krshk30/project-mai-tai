from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MethodType
from typing import Any

from project_mai_tai.services.strategy_engine_app import StrategyBotRuntime, StrategyDefinition
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.config import IndicatorConfig
from project_mai_tai.strategy_core.schwab_native_30s import (
    SchwabNativeBarBuilderManager,
    SchwabNativeEntryEngine,
    SchwabNativeIndicatorEngine,
)
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ
from project_mai_tai.strategy_core.trading_config import TradingConfig


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


def _format_et(ts: datetime) -> str:
    current = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return current.astimezone(EASTERN_TZ).strftime("%Y-%m-%d %I:%M:%S %p ET")


def _event_timestamp(event: dict[str, Any]) -> datetime:
    timestamp_ns = event.get("timestamp_ns")
    if isinstance(timestamp_ns, int) and timestamp_ns > 0:
        return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=UTC)
    recorded_at_ns = int(event.get("recorded_at_ns") or 0)
    if recorded_at_ns > 0:
        return datetime.fromtimestamp(recorded_at_ns / 1_000_000_000, tz=UTC)
    raise ValueError("capture event missing timestamp_ns and recorded_at_ns")


def _parse_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        payload = json.loads(text)
        if isinstance(payload, dict):
            events.append(payload)
    events.sort(key=lambda item: (int(item.get("arrival_seq", 0)), int(item.get("recorded_at_ns", 0))))
    return events


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
    config = TradingConfig().make_30s_schwab_native_variant(
        quantity=settings.strategy_macd_30s_default_quantity
    )
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay a recorded Schwab tick JSONL capture through the live macd_30s path.",
    )
    parser.add_argument("--input", type=Path, default=None, help="JSONL capture path from record_schwab_ticks.py.")
    parser.add_argument("--symbol", required=True, help="Symbol to replay from the capture.")
    parser.add_argument("--date", default="", help="ET date in YYYY-MM-DD format for archived replay.")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Optional archive root. Defaults to MAI_TAI_SCHWAB_TICK_ARCHIVE_ROOT.",
    )
    parser.add_argument("--env-file", default="", help="Optional KEY=VALUE env file for config overrides.")
    parser.add_argument("--output", type=Path, default=None, help="Optional JSON output path.")
    return parser


def _resolve_input_path(args: argparse.Namespace, settings: Settings, symbol: str) -> Path:
    if args.input is not None:
        return args.input
    day_text = str(args.date or "").strip()
    if not day_text:
        raise SystemExit("Provide either --input or --date for archived replay.")
    root = args.root or Path(settings.schwab_tick_archive_root)
    return root / day_text / f"{symbol}.jsonl"


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.env_file.strip():
        _load_env_file(args.env_file)

    settings = Settings()
    symbol = str(args.symbol).upper()
    input_path = _resolve_input_path(args, settings, symbol)
    all_events = _parse_events(input_path)
    events = [event for event in all_events if str(event.get("symbol", "")).upper() == symbol]
    if not events:
        raise SystemExit(f"No events for {symbol} in {args.input}")

    trading_config = _build_live_like_trading_config(settings)
    indicator_config = IndicatorConfig()
    now_holder = {"value": _event_timestamp(events[0]).astimezone(EASTERN_TZ)}

    def _now_provider() -> datetime:
        return now_holder["value"]

    runtime = StrategyBotRuntime(
        StrategyDefinition(
            code=f"replay_macd_30s_{symbol.lower()}",
            display_name="MACD Bot",
            account_name="paper:macd_30s",
            interval_secs=30,
            trading_config=trading_config,
            indicator_config=indicator_config,
        ),
        now_provider=_now_provider,
        session_factory=None,
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

    latest_trade_price = 0.0
    event_log: list[dict[str, Any]] = []
    lots: list[dict[str, Any]] = []
    closed_legs: list[dict[str, Any]] = []

    def _resolve_fill_price(side: str, reference_price: float) -> float:
        quote = runtime.latest_quotes.get(symbol, {})
        if side == "buy":
            return float(quote.get("ask") or reference_price or latest_trade_price or 0.0)
        return float(quote.get("bid") or reference_price or latest_trade_price or 0.0)

    def _record_sell_lots(*, sold_qty: int, exit_price: float, exit_time: datetime, exit_reason: str, intent_type: str) -> None:
        remaining = sold_qty
        while remaining > 0 and lots:
            lot = lots[0]
            lot_qty = int(lot["remaining_qty"])
            matched_qty = min(remaining, lot_qty)
            closed_legs.append(
                {
                    "symbol": symbol,
                    "entry_time": lot["entry_time"],
                    "entry_time_raw": lot["entry_time_raw"],
                    "entry_price": round(float(lot["entry_price"]), 4),
                    "entry_signal": lot["entry_signal"],
                    "path": lot["path"],
                    "quantity": matched_qty,
                    "exit_time": _format_et(exit_time),
                    "exit_time_raw": exit_time.isoformat(),
                    "exit_price": round(float(exit_price), 4),
                    "exit_reason": exit_reason,
                    "intent_type": intent_type,
                    "pnl": round((float(exit_price) - float(lot["entry_price"])) * matched_qty, 2),
                }
            )
            lot["remaining_qty"] = lot_qty - matched_qty
            remaining -= matched_qty
            if lot["remaining_qty"] <= 0:
                lots.pop(0)

    def _apply_intents(intents: list[Any], event_time: datetime) -> None:
        for intent in intents:
            payload = intent.payload
            side = str(payload.side)
            intent_type = str(payload.intent_type)
            reference_price = float(payload.metadata.get("reference_price") or latest_trade_price or 0.0)
            fill_price = _resolve_fill_price(side, reference_price)
            quantity = int(payload.quantity)
            event_log.append(
                {
                    "time": _format_et(event_time),
                    "time_raw": event_time.isoformat(),
                    "symbol": str(payload.symbol),
                    "side": side,
                    "intent_type": intent_type,
                    "reason": str(payload.reason),
                    "quantity": quantity,
                    "price": round(fill_price, 4),
                    "path": str(payload.metadata.get("path", "") or ""),
                    "entry_stage": str(payload.metadata.get("entry_stage", "") or ""),
                }
            )
            if intent_type == "open" and side == "buy":
                lots.append(
                    {
                        "entry_time": _format_et(event_time),
                        "entry_time_raw": event_time.isoformat(),
                        "entry_price": fill_price,
                        "remaining_qty": quantity,
                        "entry_signal": str(payload.reason),
                        "path": str(payload.metadata.get("path", "") or ""),
                    }
                )
            elif intent_type in {"close", "scale"} and side == "sell":
                _record_sell_lots(
                    sold_qty=quantity,
                    exit_price=fill_price,
                    exit_time=event_time,
                    exit_reason=str(payload.reason),
                    intent_type=intent_type,
                )
            runtime.apply_execution_fill(
                client_order_id=f"replay-{len(event_log)}",
                symbol=str(payload.symbol),
                intent_type=intent_type,
                status="filled",
                side=side,
                quantity=Decimal(str(payload.quantity)),
                price=Decimal(str(fill_price)),
                level=str(payload.metadata.get("level", "")) or None,
                path=str(payload.metadata.get("path", "")),
                reason=str(payload.reason),
            )

    for event in events:
        event_time = _event_timestamp(event)
        now_holder["value"] = event_time.astimezone(EASTERN_TZ)
        event_type = str(event.get("event_type", "")).lower()
        if event_type == "quote":
            runtime.handle_quote_tick(
                symbol=symbol,
                bid_price=float(event.get("bid_price") or 0.0) or None,
                ask_price=float(event.get("ask_price") or 0.0) or None,
            )
            continue
        if event_type != "trade":
            continue
        latest_trade_price = float(event.get("price") or latest_trade_price or 0.0)
        intents = runtime.handle_trade_tick(
            symbol=symbol,
            price=latest_trade_price,
            size=int(event.get("size") or 0),
            timestamp_ns=int(event.get("timestamp_ns") or 0) or None,
            cumulative_volume=int(event.get("cumulative_volume")) if event.get("cumulative_volume") is not None else None,
        )
        _apply_intents(intents, event_time)

    now_holder["value"] = (_event_timestamp(events[-1]) + timedelta(seconds=60)).astimezone(EASTERN_TZ)
    final_intents, _ = runtime.flush_completed_bars()
    _apply_intents(final_intents, _event_timestamp(events[-1]) + timedelta(seconds=60))

    latest_quote = runtime.latest_quotes.get(symbol, {})
    mark_price = float(latest_quote.get("bid") or latest_trade_price or 0.0)
    open_positions = []
    for lot in lots:
        open_positions.append(
            {
                "symbol": symbol,
                "entry_time": lot["entry_time"],
                "entry_time_raw": lot["entry_time_raw"],
                "entry_price": round(float(lot["entry_price"]), 4),
                "quantity": int(lot["remaining_qty"]),
                "path": lot["path"],
                "mark_price": round(mark_price, 4),
                "unrealized_pnl": round((mark_price - float(lot["entry_price"])) * int(lot["remaining_qty"]), 2),
            }
        )

    by_signal: dict[str, dict[str, Any]] = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for trade in closed_legs:
        signal = str(trade["path"] or trade["entry_signal"])
        row = by_signal[signal]
        pnl = float(trade["pnl"])
        row["trades"] += 1
        row["pnl"] += pnl
        if pnl > 0:
            row["wins"] += 1
        elif pnl < 0:
            row["losses"] += 1

    realized_pnl = round(sum(float(item["pnl"]) for item in closed_legs), 2)
    open_unrealized = round(sum(float(item["unrealized_pnl"]) for item in open_positions), 2)
    payload = {
        "symbol": symbol,
        "input": str(input_path),
        "event_count": len(events),
        "quote_count": sum(1 for event in events if str(event.get("event_type", "")).lower() == "quote"),
        "trade_tick_count": sum(1 for event in events if str(event.get("event_type", "")).lower() == "trade"),
        "config": asdict(trading_config),
        "summary": {
            "closed_legs": len(closed_legs),
            "wins": sum(1 for item in closed_legs if float(item["pnl"]) > 0),
            "losses": sum(1 for item in closed_legs if float(item["pnl"]) < 0),
            "realized_pnl": realized_pnl,
            "open_lots": len(open_positions),
            "open_unrealized_pnl": open_unrealized,
            "mark_to_market_total_pnl": round(realized_pnl + open_unrealized, 2),
            "intent_count": len(event_log),
        },
        "by_signal": {
            key: {
                "trades": value["trades"],
                "wins": value["wins"],
                "losses": value["losses"],
                "pnl": round(float(value["pnl"]), 2),
            }
            for key, value in sorted(by_signal.items())
        },
        "closed_legs": closed_legs,
        "open_positions": open_positions,
        "events": event_log,
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
