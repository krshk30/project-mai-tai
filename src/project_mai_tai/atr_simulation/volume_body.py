"""Exact whole-minute value area, deliberately off the live strategy loop."""

from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from .types import BodySnapshot, TradePrint


MINUTE_NS = 60_000_000_000
PRICE_SCALE = Decimal(10000)


def price_bin(price: Decimal) -> int:
    if not price.is_finite() or price <= 0:
        raise ValueError("positive_finite_price_required")
    return int((price * PRICE_SCALE).to_integral_value(rounding=ROUND_HALF_UP))


def legal_cap(trigger: Decimal) -> Decimal:
    price_bin(trigger)
    tick = Decimal("0.01") if trigger >= 1 else Decimal("0.0001")
    return ((trigger * Decimal("1.005")) / tick).to_integral_value(
        rounding=ROUND_FLOOR
    ) * tick


class MinuteBody:
    """Accept every causal print, including pre-touch odd lots and zero sizes.

    Memory is O(B) occupied prices for the current minute; snapshot O(B log B).
    No price-domain/bin cap discards trades. This is not a live O(1) claim.
    Caller proves prefix coverage; the presence of a first print cannot do so.
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.minute_start_ns: int | None = None
        self._bins: dict[int, int] = {}
        self._total = 0
        self._last_key: tuple[int, int] | None = None
        self._last_raw_identity: tuple[str, str] | None = None

    def add(self, trade: TradePrint) -> None:
        if trade.symbol != self.symbol:
            raise ValueError("symbol_mismatch")
        if isinstance(trade.size, bool) or not isinstance(trade.size, int) or trade.size < 0:
            raise ValueError("nonnegative_integer_size_required")
        key = (trade.key.event_ns, trade.key.sequence)
        identity = (trade.key.source.sha256, trade.key.raw_id)
        if self._last_key is not None and key <= self._last_key:
            raise ValueError("strict_causal_order_required")
        if identity == self._last_raw_identity:
            raise ValueError("duplicate_raw_print")
        level = price_bin(trade.price)
        minute = trade.key.event_ns // MINUTE_NS * MINUTE_NS
        if minute != self.minute_start_ns:
            self.minute_start_ns = minute
            self._bins = {}
            self._total = 0
        self._last_key = key
        self._last_raw_identity = identity
        if trade.size:
            self._bins[level] = self._bins.get(level, 0) + trade.size
            self._total += trade.size

    def snapshot(self, *, prefix_complete: bool | None) -> BodySnapshot:
        minute = self.minute_start_ns if self.minute_start_ns is not None else 0
        ordered = tuple(sorted(self._bins.items()))
        if not ordered:
            return BodySnapshot(minute, None, None, None, 0, 0, (), prefix_complete)
        poc = max(range(len(ordered)), key=lambda i: (ordered[i][1], ordered[i][0]))
        low = high = poc
        included = ordered[poc][1]
        while included * 10 < self._total * 7:
            down = ordered[low - 1][1] if low else -1
            up = ordered[high + 1][1] if high + 1 < len(ordered) else -1
            if up >= down:
                high += 1
                included += ordered[high][1]
            else:
                low -= 1
                included += ordered[low][1]
        return BodySnapshot(
            minute, ordered[low][0], ordered[high][0], ordered[poc][0],
            included, self._total, ordered, prefix_complete,
        )


def body_covers(body: BodySnapshot, trigger: Decimal) -> bool:
    """Compare exact trigger to body bounds, not a rounded trigger bin."""
    if body.low_bin is None or body.high_bin is None:
        return False
    return Decimal(body.low_bin) <= trigger * PRICE_SCALE <= Decimal(body.high_bin)
