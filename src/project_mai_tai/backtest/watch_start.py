"""Per-symbol WATCH-START for the replay (#618/#619, built 2026-07-31).

The live bot suppresses an entry whose ATR flip bar predates the moment the symbol joined its
watchlist -- "if the momentum scanner confirms a NEW stock it needs to wait for a fresh flip; the
stocks we've had since 07:00 don't have to" (operator, 2026-07-30). The bot holds that instant in
memory (`_watch_start_ms`, set when a symbol enters the selected set) and nothing persists it, so a
backtest has to reconstruct it.

The durable reconstruction is `scanner_confirmed_events` -- the CONFIRM / FADE / RETENTION_DROP feed
live since 2026-07-10. v2's watchlist is driven by the scanner's confirmed set, so a CONFIRM is the
symbol arriving in front of us and a FADE / RETENTION_DROP is it leaving.

⛔ THE FEED FLICKERS. A symbol can confirm, fade and re-confirm within a minute (SNDG did it three
times inside three minutes on 2026-07-30). The bot re-stamps `_watch_start_ms` on every re-join, so
the faithful answer is not "first CONFIRM of the day" -- it is the start of the membership window the
symbol was in AT THE MOMENT OF THE ARM. `watch_start_for` implements exactly that.

⛔ This is a RECONSTRUCTION, not the live value. The scanner's CONFIRM instant and v2's watchlist
re-selection are a poll apart, so expect second-scale disagreement with what the bot actually held.
It is the right reference to study; it is not proof of what the bot did on a given trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select

from ..db.models import ScannerConfirmedEvent

# A symbol arrives on CONFIRM and leaves on either of these.
_JOIN = "CONFIRM"
_LEAVE = ("FADE", "RETENTION_DROP")


@dataclass(frozen=True)
class WatchWindow:
    """One membership span. `end_ms` is None while the symbol never left before session end."""

    start_ms: int
    end_ms: int | None

    def contains(self, at_ms: int) -> bool:
        return self.start_ms <= at_ms and (self.end_ms is None or at_ms < self.end_ms)


def build_windows(events: list[tuple[str, int]]) -> list[WatchWindow]:
    """Collapse a symbol's time-ordered ``(event_type, epoch_ms)`` list into membership windows.

    A CONFIRM while already a member is ignored (it does not restart the clock -- the symbol never
    left). A LEAVE while not a member is ignored. Both happen in the real feed.
    """
    windows: list[WatchWindow] = []
    open_start: int | None = None
    for event_type, ts_ms in sorted(events, key=lambda e: e[1]):
        if event_type == _JOIN:
            if open_start is None:
                open_start = ts_ms
        elif event_type in _LEAVE and open_start is not None:
            windows.append(WatchWindow(open_start, ts_ms))
            open_start = None
    if open_start is not None:
        windows.append(WatchWindow(open_start, None))
    return windows


def load_watch_windows(
    session_factory,
    trade_date: date,
    symbol: str,
    *,
    realtime_confirms_only: bool = False,
) -> list[WatchWindow]:
    """Read one symbol's membership windows for a session day from `scanner_confirmed_events`.

    ``realtime_confirms_only`` removes the historical carry-forward rows whose ``CONFIRM``
    timestamp was copied into a later session.  Real scanner decisions are persisted within two
    minutes of their event timestamp; carry-forward rows are not.  Leave events remain in the
    stream so a real membership span still closes at the recorded fade/removal boundary.
    """
    with session_factory() as session:
        query = (
            select(ScannerConfirmedEvent.event_type, ScannerConfirmedEvent.event_at)
            .where(ScannerConfirmedEvent.trade_date == trade_date)
            .where(ScannerConfirmedEvent.symbol == symbol.upper())
            .order_by(ScannerConfirmedEvent.event_at)
        )
        if realtime_confirms_only:
            age_seconds = func.abs(
                func.extract(
                    "epoch",
                    ScannerConfirmedEvent.created_at - ScannerConfirmedEvent.event_at,
                )
            )
            query = query.where(
                (ScannerConfirmedEvent.event_type != _JOIN) | (age_seconds <= 120)
            )
        rows = session.execute(query).all()
    return build_windows([(str(t), int(at.timestamp() * 1000)) for t, at in rows])


def watch_start_for(windows: list[WatchWindow], at_ms: int) -> int | None:
    """The start of the membership window covering `at_ms`, else the most recent one that ENDED
    before it, else None (the symbol was never confirmed that day -- caller falls back to boot).

    Falling back to the most recent CLOSED window rather than None matters: an arm can land in a
    brief gap between a FADE and the re-CONFIRM a few seconds later, and treating that as "never
    watched" would wrongly exempt the segment from the cap instead of tightening it.
    """
    covering = [w for w in windows if w.contains(at_ms)]
    if covering:
        return max(w.start_ms for w in covering)
    prior = [w for w in windows if w.start_ms <= at_ms]
    if prior:
        return max(w.start_ms for w in prior)
    return None
