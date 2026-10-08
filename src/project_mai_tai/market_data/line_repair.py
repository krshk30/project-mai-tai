"""Bounded source-repair budget; never schedules a trading operation."""
from dataclasses import dataclass


@dataclass
class LineRepair:
    epoch: int
    membership: int
    reason: str
    first_closed_at_ms: int = 0
    attempts: int = 0
    next_attempt_ms: int = 0
    published: bool = False
    fallback: bool = False

    def due(self, now_ms: int) -> bool:
        return not self.published and self.attempts < 5 and now_ms >= self.next_attempt_ms

    def started(self, now_ms: int) -> None:
        self.attempts += 1
        self.next_attempt_ms = now_ms + 60_000

    def fallback_due(self, now_ms: int) -> bool:
        return bool(not self.published and not self.fallback and self.first_closed_at_ms
                    and now_ms >= self.first_closed_at_ms + 60_000)
