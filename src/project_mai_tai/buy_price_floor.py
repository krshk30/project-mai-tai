"""PRICE1: no-I/O floor on the in-memory price of a new BUY, never a SELL."""

from decimal import Decimal, InvalidOperation
from datetime import UTC, date, datetime
import json
import logging
from uuid import UUID


logger = logging.getLogger("price1")
MINIMUM_BUY_PRICE = Decimal("1.00")


def _unique_signal_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_signal_key")
        value[key] = item
    return value


def _handoff_signal(metadata):
    raw = metadata["momlive_signal"]
    if not isinstance(raw, str) or len(raw) > 65_536:
        raise ValueError("signal_envelope_invalid")
    value = json.loads(raw, object_pairs_hook=_unique_signal_object)
    if not isinstance(value, dict) or not isinstance(value.get("signal_price"), str):
        raise ValueError("signal_envelope_invalid")
    nested = Decimal(value["signal_price"])
    if not nested.is_finite():
        raise ValueError("signal_price_conflict")
    if "signal_price" in metadata:
        if not isinstance(metadata["signal_price"], str):
            raise ValueError("signal_price_conflict")
        top = Decimal(metadata["signal_price"])
        if not top.is_finite() or nested != top:
            raise ValueError("signal_price_conflict")
    return value


def buy_intent_allowed(event):
    """Bind a real MOM handoff envelope before the canonical OMS actor offer.

    The original producer carries its raw price in momlive_signal. A duplicated
    top-level price, if present, must agree. Never substitute a cap or quote.
    """
    order = event.payload
    metadata = order.metadata
    handoff = (order.strategy_code == "momentum_warn_live" and order.side == "buy"
        and order.intent_type in {"open", "scale"} and (
            "momlive_signal" in metadata or metadata.get("one_shot") == "true"
            or order.reason == "MOMLIVE1_DETECTED"))
    if handoff:
        try:
            value = _handoff_signal(metadata)
            start, end, observed = (value[key] for key in (
                "interval_start_ms", "interval_end_ms", "observed_ns"))
            if (event.source_service != "momentum-paper" or metadata.get("one_shot") != "true"
                    or UUID(metadata["episode_id"]) != event.event_id
                    or value["symbol"] != order.symbol or order.symbol != order.symbol.upper()
                    or not isinstance(value["source_id"], str) or not value["source_id"]
                    or any(type(stamp) is not int for stamp in (start, end, observed))
                    or not 0 <= start <= end <= observed//1_000_000 or observed <= 0
                    or event.produced_at != datetime.fromtimestamp(observed/1e9, UTC)):
                raise ValueError("signal_identity_or_time_conflict")
            date.fromisoformat(value["session_date"])
        except (ValueError, KeyError, TypeError, ArithmeticError, OverflowError):
            logger.info("[PRICE1-NO-PRICE] bot=%s symbol=%s payload_keys=%s metadata_keys=%s",
                        order.strategy_code, order.symbol,
                        sorted(order.model_dump()), sorted(metadata))
            logger.info("[PRICE1-REFUSED] bot=%s symbol=%s reason=handoff_signal_envelope_UNKNOWN",
                        order.strategy_code, order.symbol)
            return False
    return buy_order_allowed(order)


def buy_price_allowed(price, *, bot, symbol, side="buy"):
    if side != "buy":
        return True
    try:
        value = price if isinstance(price, Decimal) else Decimal(str(price))
        if value.is_finite() and value >= MINIMUM_BUY_PRICE:
            return True
    except (InvalidOperation, ValueError, TypeError):
        pass
    logger.info("[PRICE1-REFUSED] bot=%s symbol=%s price=%s", bot, symbol, price)
    return False


def buy_order_allowed(order):
    """Check the existing signal/entry anchor, never substitute its padded cap.

    Cancels (including cancellation of an old BUY) and every SELL bypass this
    entry-only rule. Reprice plans carry their original entry anchor as well.
    """
    if order.side != "buy" or order.strategy_code not in {
        "schwab_1m_v2", "momentum_warn_live", "orb_schwab"
    }:
        return True
    metadata = order.metadata
    if order.intent_type not in {"open", "scale"} and not (
        order.strategy_code == "orb_schwab" and metadata.get("orb_schwab_reprice") == "true"
    ):
        return True
    signal = None
    if order.strategy_code == "momentum_warn_live" and "momlive_signal" in metadata:
        try:
            signal = _handoff_signal(metadata)
        except (ValueError, KeyError, TypeError, ArithmeticError):
            logger.info("[PRICE1-NO-PRICE] bot=%s symbol=%s metadata_keys=%s",
                        order.strategy_code, order.symbol, sorted(metadata))
            logger.info("[PRICE1-REFUSED] bot=%s symbol=%s reason=signal_price_conflict_or_missing",
                        order.strategy_code, order.symbol)
            return False
    keys = {
        "schwab_1m_v2": ("entry_price", "stop_price", "reference_price"),
        "momentum_warn_live": ("signal_price",),
        "orb_schwab": ("orb_intended_break_level",),
    }[order.strategy_code]
    prices = ([signal["signal_price"]] if signal is not None else
              [metadata[key] for key in keys if key in metadata])
    if not prices:
        logger.info("[PRICE1-NO-PRICE] bot=%s symbol=%s metadata_keys=%s",
                    order.strategy_code, order.symbol, sorted(metadata))
        prices = [None]
    kind = str(metadata.get("order_type", getattr(order, "order_type", "market"))).lower()
    if kind in {"limit", "stop_limit", "stop-limit"} and "limit_price" in metadata:
        prices.append(metadata["limit_price"])
    return all(buy_price_allowed(price, bot=order.strategy_code, symbol=order.symbol)
               for price in prices)
