"""Session-owned canonical candles; raw producer candidates remain separate.

Only KEEPWATCH uses this projection. A pin belongs to a session/minute, not a
subscription, request or transport (both real producers can arrive over REST).
"""

from dataclasses import dataclass
import math

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar


@dataclass(frozen=True)
class ProducerCandidate:
    symbol: str
    timestamp_ms: int
    producer: str
    open: float
    high: float
    low: float
    close: float
    volume: int | float

    def bar(self) -> ChartBar:
        return ChartBar(self.symbol, self.open, self.high, self.low, self.close,
                        self.volume, self.timestamp_ms, producer=self.producer)


class SessionProducerPins:
    """At most960 minutes, two latest immutable producer candidates per minute.

    Raw response SHA/IDs stay on KeepWatchHistory. Revision counts retain that
    an earlier candidate existed without an unbounded in-memory revision log.
    Stored DB rows have no producer attestation and cannot revise a producer pin.
    """

    def __init__(self, symbol: str, anchor_ms: int):
        self.symbol = symbol
        self.anchor_ms = anchor_ms
        self.producers: dict[int, str] = {}
        self.candidates: dict[int, dict[str, ProducerCandidate]] = {}
        self.revisions: dict[tuple[int, str], int] = {}
        self.stored: dict[int, tuple] = {}

    def _validate(self, bar, now_ms: int) -> None:
        stamp = bar.timestamp_ms
        if (bar.symbol != self.symbol or type(stamp) is not int or stamp % 60_000
                or type(now_ms) is not int or not self.anchor_ms <= now_ms < self.anchor_ms + 86_400_000
                or not self.anchor_ms <= stamp < self.anchor_ms + 57_600_000
                or stamp + 60_000 > now_ms
                or not all(math.isfinite(v) and v > 0
                           for v in (bar.open, bar.high, bar.low, bar.close))
                or not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
                or not math.isfinite(bar.volume) or bar.volume < 0):
            raise ValueError("unclosed, malformed or foreign producer candle")

    def accept(self, bar: ChartBar, *, now_ms: int) -> ChartBar:
        self._validate(bar, now_ms)
        if bar.producer not in {"massive", "schwab"}:
            raise ValueError("closed candle producer identity UNKNOWN")
        candidate = ProducerCandidate(bar.symbol, bar.timestamp_ms, bar.producer,
            bar.open, bar.high, bar.low, bar.close, bar.volume)
        versions = self.candidates.setdefault(bar.timestamp_ms, {})
        if versions.get(bar.producer) != candidate:
            versions[bar.producer] = candidate
            key = (bar.timestamp_ms, bar.producer)
            self.revisions[key] = self.revisions.get(key, 0) + 1
        self.producers.setdefault(bar.timestamp_ms, bar.producer)
        return self.current(bar.timestamp_ms)

    def current(self, stamp: int) -> ChartBar:
        return self.candidates[stamp][self.producers[stamp]].bar()

    def stored_bar(self, bar, *, now_ms: int) -> ChartBar:
        self._validate(bar, now_ms)
        self.stored[bar.timestamp_ms] = (
            bar.open, bar.high, bar.low, bar.close, bar.volume)
        if bar.timestamp_ms not in self.producers:
            raise ValueError("stored row has no attested producer pin")
        return self.current(bar.timestamp_ms)

    def matches(self, bar) -> bool:
        if bar.timestamp_ms not in self.producers:
            return False
        current = self.current(bar.timestamp_ms)
        return all(getattr(current, key) == getattr(bar, key)
                   for key in ("symbol", "timestamp_ms", "open", "high", "low", "close", "volume"))
