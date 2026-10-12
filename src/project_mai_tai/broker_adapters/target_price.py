"""A sell target is a minimum price, so round upward on both brokers' grid."""
from decimal import ROUND_CEILING, Decimal


def target_limit_price(price: Decimal) -> Decimal:
    tick = Decimal("0.01") if price >= 1 else Decimal("0.0001")
    return price.quantize(tick, rounding=ROUND_CEILING)
