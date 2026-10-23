"""PRICE1: no-I/O floor on the in-memory price of a new BUY, never a SELL."""

from decimal import Decimal, InvalidOperation
import logging


logger = logging.getLogger("price1")
MINIMUM_BUY_PRICE = Decimal("1.00")


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
    keys = {
        "schwab_1m_v2": ("entry_price", "stop_price", "reference_price"),
        "momentum_warn_live": ("signal_price",),
        "orb_schwab": ("orb_intended_break_level",),
    }[order.strategy_code]
    prices = [metadata[key] for key in keys if key in metadata]
    if not prices:
        prices = [None]
    kind = str(metadata.get("order_type", getattr(order, "order_type", "market"))).lower()
    if kind in {"limit", "stop_limit", "stop-limit"} and "limit_price" in metadata:
        prices.append(metadata["limit_price"])
    return all(buy_price_allowed(price, bot=order.strategy_code, symbol=order.symbol)
               for price in prices)
