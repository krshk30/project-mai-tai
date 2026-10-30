"""Bounded, non-causal flip diagnostics. No clocks, I/O, or trading decisions here.

C1 2026-10-29 21:33: provider OHLCV never identifies its deciding print.
Only the existing accepted-trade callback may supply observations. Missing tick
capture, timestamp provenance, or retained correspondence remains UNKNOWN.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
import re

UNKNOWN = "UNKNOWN"
MAX_SYMBOLS = 128
MAX_TRADES_PER_SYMBOL = 256


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _price(value: object) -> str:
    if isinstance(value, bool):
        return UNKNOWN
    try:
        number = Decimal(str(value))
        return str(number) if number.is_finite() and number > 0 else UNKNOWN
    except (InvalidOperation, ValueError, TypeError):
        return UNKNOWN


def _conditions(value: object) -> object:
    # Missing conditions are unknown, never interpreted as normal-sale codes.
    if isinstance(value, (list, tuple)) and len(value) <= 32:
        if all(type(v) is int or (isinstance(v, str) and
               re.fullmatch(r"[A-Za-z0-9 ,.-]{0,64}", v)) for v in value):
            return tuple(value)
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9 ,.-]{1,64}", value):
        return value
    return UNKNOWN


@dataclass(frozen=True)
class ObservedTrade:
    exchange_ms: int
    received_ns: int
    price: str
    size: int | str
    conditions: object
    service: str
    raw_hash: str


class FlipPrintDiagnostic:
    """At most 128 symbols x 256 arrival-ordered trades; LRU eviction is explicit.

    "Last" is callback arrival order, not exchange-time sorting. The UTC epoch
    receipt clock is sampled once at service callback entry. It is NOT socket,
    provider, exchange, or durable-write receipt time. Storage is diagnostic-only.
    """

    def __init__(self) -> None:
        self._trades: OrderedDict[str, deque[ObservedTrade]] = OrderedDict()

    def observe_accepted_tick(self, tick: object, received_ns: int) -> None:
        """No eligibility rerun. Reject envelope fallback as exchange-time proof."""
        symbol = tick.symbol
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9.^/-]{1,32}", symbol):
            return
        if tick.kind != "trade" or not _positive_int(received_ns):
            return
        service = tick.service
        field = {"LEVELONE_EQUITIES": "35", "TIMESALE_EQUITY": "1"}.get(service)
        if field is None or not isinstance(tick.raw, dict):
            return
        raw_event = tick.raw.get(field)
        # Match the streamer's numeric-field conversion without accepting a
        # missing/zero field whose event_ts_ms came from envelope timestamp.
        if isinstance(raw_event, bool):
            return
        try:
            numeric = float(raw_event)
            if not math.isfinite(numeric) or numeric <= 0 or not numeric.is_integer():
                return
            exchange_ms = int(numeric)
        except (ValueError, TypeError, OverflowError):
            return
        if exchange_ms != tick.event_ts_ms:
            return
        price = _price(tick.price)
        if price == UNKNOWN:
            return
        size = tick.size if type(tick.size) is int and tick.size >= 0 else UNKNOWN
        raw_hash = tick.raw_hash
        if not isinstance(raw_hash, str) or not re.fullmatch(r"[0-9a-f]{40}", raw_hash):
            raw_hash = UNKNOWN
        observation = ObservedTrade(exchange_ms, received_ns, price, size,
            _conditions(tick.raw.get("conditions", tick.raw.get("c"))), service, raw_hash)
        queue = self._trades.get(symbol)
        if queue is None:
            if len(self._trades) >= MAX_SYMBOLS:
                self._trades.popitem(last=False)
            queue = deque(maxlen=MAX_TRADES_PER_SYMBOL)
            self._trades[symbol] = queue
        self._trades.move_to_end(symbol)
        queue.append(observation)

    def line(self, *, symbol: str, flip: str, bar_ms: int, close: object,
             phase: str, state_only: bool) -> str | None:
        if phase != "live" or state_only or flip not in ("BUY", "SELL"):
            return None
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9.^/-]{1,32}", symbol):
            return None
        if not _positive_int(bar_ms) or bar_ms % 60000:
            return None
        end_ms = bar_ms + 60000
        observed: object = UNKNOWN
        queue = self._trades.get(symbol, ())
        # Strict before-end on BOTH clocks. A late print cannot rewrite the
        # observation for this bar, even if its exchange timestamp is old.
        trade = next((t for t in reversed(queue)
                      if t.exchange_ms < end_ms and t.received_ns < end_ms * 1000000), None)
        if trade is not None:
            close_price = _price(close)
            observed = {
                "label": "observed_not_bar_cause",
                "exchange_ts_ms": trade.exchange_ms,
                "received_ts_ns": trade.received_ns,
                "received_clock": "UTC_epoch_service_tick_callback_entry",
                "exchange_clock": "explicit_stream_content_trade_timestamp_ms",
                "price": trade.price, "size": trade.size,
                "conditions": trade.conditions, "service": trade.service,
                "raw_hash": trade.raw_hash,
                "exchange_seconds_before_minute_end": str(Decimal(end_ms - trade.exchange_ms) / 1000),
                "received_seconds_before_minute_end": str(Decimal(end_ms * 1000000 - trade.received_ns) / 1000000000),
                "bar_close_minus_price": (str(Decimal(close_price) - Decimal(trade.price))
                                          if close_price != UNKNOWN else UNKNOWN),
            }
        fields = {
            "symbol": symbol, "flip": flip, "bar_start_ms": bar_ms,
            "bar_end_ms": end_ms, "bar_close": _price(close),
            "deciding_print_ts": UNKNOWN, "deciding_print_size": UNKNOWN,
            "deciding_print_conditions": UNKNOWN,
            "deciding_print_seconds_before_minute_end": UNKNOWN,
            "reason": "bar_has_no_print_association",
            "observed_last_trade": observed,
            "observed_label": "observed_not_bar_cause",
            "observed_scope": "existing_accepted_trade_branch_bounded_retention",
            "observed_missing_reason": ("none" if trade is not None else
                                        "no_retained_explicit_trade_before_end_on_both_clocks"),
            "capture_completeness": UNKNOWN,
        }
        return "[V2-FLIP-PRINT-DIAGNOSTIC] " + json.dumps(fields, separators=(",", ":"), sort_keys=True)
