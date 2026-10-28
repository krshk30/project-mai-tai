"""Session-scoped observation membership, separate from permission to buy."""
from dataclasses import dataclass, field
from typing import Mapping


@dataclass
class KeepWatchSession:
    anchor_ms: int = 0
    first_seen_ms: dict[str, int] = field(default_factory=dict)
    tradeable: set[str] = field(default_factory=set)
    awaiting_sell: set[str] = field(default_factory=set)
    awaiting_sell_since_ms: dict[str, int] = field(default_factory=dict)
    peak_retained: int = 0

    def update(self, anchor_ms: int, selected: set[str], now_ms: int,
               line_states: Mapping[str, str], *, retain_entries: bool = False,
               excluded: frozenset[str] = frozenset()) -> None:
        if anchor_ms != self.anchor_ms:
            self.anchor_ms = anchor_ms
            self.first_seen_ms.clear()
            self.tradeable.clear()
            self.awaiting_sell.clear()
            self.awaiting_sell_since_ms.clear()
            self.peak_retained = 0
        for symbol in selected - self.tradeable:
            if symbol in self.first_seen_ms and line_states.get(symbol) != "short":
                self.wait_for_sell(symbol, now_ms)
            self.first_seen_ms.setdefault(symbol, now_ms)
        # Explicit service policy, never authority from a subscription/position.
        self.tradeable = ((self.tradeable | selected) if retain_entries else set(selected)) - excluded
        self.peak_retained = max(self.peak_retained, len(self.first_seen_ms))

    def wait_for_sell(self, symbol: str, now_ms: int) -> None:
        self.awaiting_sell.add(symbol)
        self.awaiting_sell_since_ms[symbol] = now_ms

    def observe_line(self, symbol: str, line_state: str, closed_at_ms: int) -> None:
        if (symbol in self.tradeable and line_state == "short"
                and closed_at_ms > self.awaiting_sell_since_ms.get(symbol, 0)):
            self.awaiting_sell.discard(symbol)
            self.awaiting_sell_since_ms.pop(symbol, None)

    def allows_entry(self, symbol: str) -> bool:
        return symbol in self.tradeable and symbol not in self.awaiting_sell

    def retained(self, anchor_ms: int) -> set[str]:
        return set(self.first_seen_ms) if anchor_ms == self.anchor_ms else set()
