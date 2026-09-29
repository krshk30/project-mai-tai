"""Strict, default-off order contract for the separate live Schwab ORB strategy."""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload, stream_name
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata

STRATEGY_CODE = "orb_schwab"
SOURCE_SERVICE = "orb-schwab"
QUANTITY = Decimal("2")
_ET = ZoneInfo("America/New_York")


def build_orb_schwab_exit_intent(settings: Settings, symbol: str, entry_id: str, fill_id: str, reason: str) -> TradeIntentEvent:
    from project_mai_tai.orb_schwab_exits import ATR_REASON, BODY_REASON

    if reason not in {BODY_REASON, ATR_REASON}:
        raise ValueError("unknown ORB exit rule")
    UUID(entry_id)
    UUID(fill_id)
    return TradeIntentEvent(
        event_id=uuid5(NAMESPACE_URL, f"orb-schwab-exit:{entry_id}:{fill_id}:{reason}"),
        source_service=SOURCE_SERVICE,
        payload=TradeIntentPayload(
            strategy_code=STRATEGY_CODE, broker_account_name=settings.strategy_schwab_1m_v2_account_name,
            symbol=symbol.upper(), side="sell", quantity=QUANTITY, intent_type="close", reason=reason,
            metadata={"orb_schwab_exit": "true", "entry_order_id": entry_id, "entry_fill_id": fill_id},
        ),
    )


def build_orb_schwab_open_intent(
    settings: Settings, symbol: str, breakout_level: Decimal
) -> TradeIntentEvent:
    """Build an intent only; OMS and the broker remain the execution authorities."""
    ticker = symbol.strip().upper()
    account = settings.strategy_schwab_1m_v2_account_name
    if not ticker or not account.startswith("live:") or settings.provider_for_account(account) != "schwab":
        raise ValueError("ORB live orders require a named Schwab account and symbol")
    metadata = build_orb_schwab_bracket_metadata(breakout_level)
    metadata["orb_intended_break_level"] = format(breakout_level, "f")
    return TradeIntentEvent(
        source_service=SOURCE_SERVICE,
        payload=TradeIntentPayload(
            strategy_code=STRATEGY_CODE,
            broker_account_name=settings.strategy_schwab_1m_v2_account_name,
            symbol=ticker,
            side="buy",
            quantity=QUANTITY,
            intent_type="open",
            reason="ORB_FIXED_HIGH_SCHWAB_STOP_LIMIT",
            metadata=metadata,
        ),
    )


def build_orb_schwab_cancel_intent(settings: Settings, symbol: str) -> TradeIntentEvent:
    """Request cancellation of this strategy's working buy, never its OCO exits."""
    ticker = symbol.strip().upper()
    if not ticker:
        raise ValueError("ORB cancel requires a symbol")
    return TradeIntentEvent(
        source_service=SOURCE_SERVICE,
        payload=TradeIntentPayload(
            strategy_code=STRATEGY_CODE,
            broker_account_name=settings.strategy_schwab_1m_v2_account_name,
            symbol=ticker,
            side="buy",
            quantity=Decimal("0"),
            intent_type="cancel",
            reason="ORB_COMPLETED_SCHWAB_MACD_NEGATIVE",
            metadata={"orb_schwab_cancel": "true"},
        ),
    )


def build_orb_schwab_reprice_intent(
    settings: Settings, symbol: str, breakout_level: Decimal
) -> TradeIntentEvent:
    """Change the existing parent in place; this must never submit a second OPEN."""
    ticker = symbol.strip().upper()
    if not ticker:
        raise ValueError("ORB reprice requires a symbol")
    metadata = build_orb_schwab_bracket_metadata(breakout_level)
    metadata["orb_intended_break_level"] = format(breakout_level, "f")
    metadata["orb_schwab_reprice"] = "true"
    return TradeIntentEvent(
        source_service=SOURCE_SERVICE,
        payload=TradeIntentPayload(
            strategy_code=STRATEGY_CODE,
            broker_account_name=settings.strategy_schwab_1m_v2_account_name,
            symbol=ticker,
            side="buy",
            quantity=Decimal("0"),
            intent_type="cancel",
            reason="ORB_RAISE_EXISTING_SCHWAB_STOP_LIMIT",
            metadata=metadata,
        ),
    )


def orb_schwab_intent_refusal(
    event: TradeIntentEvent, settings: Settings, now: datetime
) -> str | None:
    """Reject every shape that could become a naked or misrouted live entry."""
    if not settings.orb_live_schwab_orders_enabled or not settings.orb_enabled:
        return "orb_schwab_live_disabled"
    payload = event.payload
    if event.source_service != SOURCE_SERVICE:
        return "orb_schwab_wrong_source"
    if not payload.symbol or payload.symbol != payload.symbol.strip().upper():
        return "orb_schwab_invalid_symbol"
    if (
        payload.broker_account_name != settings.strategy_schwab_1m_v2_account_name
        or not payload.broker_account_name.startswith("live:")
        or settings.provider_for_account(payload.broker_account_name) != "schwab"
    ):
        return "orb_schwab_wrong_account"
    if payload.intent_type == "close":
        from project_mai_tai.orb_schwab_exits import ATR_REASON, BODY_REASON

        try:
            UUID(payload.metadata["entry_order_id"])
            UUID(payload.metadata["entry_fill_id"])
        except (KeyError, ValueError, TypeError):
            return "orb_schwab_exit_identity_missing"
        if (
            payload.side != "sell" or payload.quantity != QUANTITY
            or payload.reason not in {BODY_REASON, ATR_REASON}
            or payload.metadata.get("orb_schwab_exit") != "true"
            or set(payload.metadata) != {"orb_schwab_exit", "entry_order_id", "entry_fill_id"}
        ):
            return "orb_schwab_unsupported_exit"
        if now.tzinfo is None or now.astimezone(_ET).weekday() >= 5 or not (
            time(9, 30) <= now.astimezone(_ET).time() < time(16)
        ):
            return "orb_schwab_outside_exit_window"
        return None
    if payload.intent_type == "cancel":
        if payload.metadata.get("orb_schwab_reprice") == "true":
            if payload.side != "buy" or payload.quantity != 0 or now.tzinfo is None:
                return "orb_schwab_unsupported_intent"
            now_et = now.astimezone(_ET)
            if now_et.weekday() >= 5 or not time(9, 29) <= now_et.time() < time(9, 30, 15):
                return "orb_schwab_outside_reprice_window"
            try:
                level = Decimal(payload.metadata["orb_intended_break_level"])
                expected = build_orb_schwab_bracket_metadata(level)
            except (KeyError, InvalidOperation, TypeError, ValueError):
                return "orb_schwab_invalid_bracket"
            expected.update(
                {"orb_intended_break_level": format(level, "f"), "orb_schwab_reprice": "true"}
            )
            return None if payload.metadata == expected else "orb_schwab_invalid_bracket"
        if (
            payload.side == "buy"
            and payload.quantity == 0
            and payload.metadata == {"orb_schwab_cancel": "true"}
        ):
            return None
        return "orb_schwab_unsupported_intent"
    if payload.intent_type != "open" or payload.side != "buy" or payload.quantity != QUANTITY:
        return "orb_schwab_unsupported_intent"
    if now.tzinfo is None:
        return "orb_schwab_invalid_clock"
    now_et = now.astimezone(_ET)
    if now_et.weekday() >= 5 or not time(9, 28) <= now_et.time() < time(9, 28, 15):
        return "orb_schwab_outside_entry_window"
    try:
        raw_level = Decimal(payload.metadata["orb_intended_break_level"])
        expected = build_orb_schwab_bracket_metadata(raw_level)
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return "orb_schwab_invalid_bracket"
    expected["orb_intended_break_level"] = format(raw_level, "f")
    if payload.metadata != expected:
        return "orb_schwab_invalid_bracket"
    return None


async def publish_orb_schwab_intent(
    redis: Redis, settings: Settings, event: TradeIntentEvent, now: datetime
) -> str:
    """Publish to OMS, never directly to a broker; OMS rechecks every guard."""
    refusal = orb_schwab_intent_refusal(event, settings, now)
    if refusal is not None:
        raise ValueError(f"ORB Schwab intent refused before publish: {refusal}")
    return await redis.xadd(
        stream_name(settings.redis_stream_prefix, "strategy-intents"),
        {"data": event.model_dump_json()},
        maxlen=settings.redis_strategy_intent_stream_maxlen,
        approximate=True,
    )
