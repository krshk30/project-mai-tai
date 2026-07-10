"""Research-only capture of the momentum scanner's confirm/evict decisions.

Pure helper invoked from the strategy engine's ``process_snapshot_batch`` ONLY when
``scanner_confirmed_capture_enabled`` is set (default False -> this module is never
called, the live path is byte-identical). Writes three event types to
``scanner_confirmed_events``:

  * CONFIRM        — one per currently-confirmed candidate, carrying the confirm_path,
                     rank_score, and the shares_outstanding (``float_used``) the scanner
                     gated on. ``reconfirm_seq`` counts prior CONFIRM rows for the same
                     (trade_date, symbol) so re-confirmations are distinguishable.
  * FADE           — one per symbol the scanner just pruned from the confirmed set.
  * RETENTION_DROP — one per symbol that just fell out of the feed-retention set.

Never raises: the whole DB block is wrapped so a capture failure can never break the
scan loop. Natural-key dedupe (trade_date, symbol, event_type, event_at) via ON
CONFLICT DO NOTHING makes re-emits within a tick idempotent.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")

_INSERT_SQL = text(
    """
    INSERT INTO scanner_confirmed_events (
        trade_date, symbol, event_type, event_at, confirm_path, rank_score,
        force_watchlist, price, day_volume, float_used, change_pct, reconfirm_seq
    ) VALUES (
        :trade_date, :symbol, :event_type, :event_at, :confirm_path, :rank_score,
        :force_watchlist, :price, :day_volume, :float_used, :change_pct, :reconfirm_seq
    )
    ON CONFLICT (trade_date, symbol, event_type, event_at) DO NOTHING
    """
)

# reconfirm_seq is computed in Python (a separate SELECT) rather than a scalar subquery inside the
# INSERT VALUES: reusing :trade_date/:symbol in both the VALUES list and a subquery made Postgres
# deduce inconsistent parameter types (text vs varchar -> AmbiguousParameter). See PR follow-up.
_COUNT_CONFIRM_SQL = text(
    "SELECT count(*) FROM scanner_confirmed_events "
    "WHERE trade_date = :trade_date AND symbol = :symbol AND event_type = 'CONFIRM'"
)

# Latest event type for (trade_date, symbol), used to suppress repeat FADEs: the scanner's
# prune list re-surfaces a still-faded symbol every scan cycle, so a fade is only genuine when
# the symbol was last CONFIRMED (a confirmed->faded transition). A CONFIRM inserted earlier in
# the same transaction is visible here, so a same-cycle re-confirm+fade is still recorded.
_LATEST_EVENT_SQL = text(
    "SELECT event_type FROM scanner_confirmed_events "
    "WHERE trade_date = :trade_date AND symbol = :symbol "
    "ORDER BY event_at DESC, id DESC LIMIT 1"
)


def _to_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _to_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _parse_confirmed_at(raw: object, trade_date: date, fallback: datetime) -> datetime:
    """Parse a scanner confirmed_at like ``09:46:52 AM ET`` onto trade_date (ET).

    Falls back to ``fallback`` when the value is missing or unparseable.
    """
    text_value = str(raw or "").strip()
    if not text_value:
        return fallback
    cleaned = text_value.replace(" ET", "").replace("ET", "").strip()
    for fmt in ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            parsed.hour,
            parsed.minute,
            parsed.second,
            tzinfo=EASTERN,
        )
    return fallback


def capture_events(
    session_factory,
    *,
    trade_date: date,
    now: datetime,
    all_confirmed: Iterable[Mapping[str, object]],
    faded_symbols: Iterable[str],
    dropped_retention_symbols: Iterable[str],
) -> None:
    """Persist CONFIRM / FADE / RETENTION_DROP rows. Never raises."""
    if session_factory is None:
        return

    rows: list[dict[str, object]] = []

    for stock in all_confirmed or []:
        symbol = str(stock.get("ticker", "")).upper().strip()
        if not symbol:
            continue
        event_at = _parse_confirmed_at(stock.get("confirmed_at"), trade_date, now)
        rows.append(
            {
                "trade_date": trade_date,
                "symbol": symbol,
                "event_type": "CONFIRM",
                "event_at": event_at,
                "confirm_path": (str(stock.get("confirmation_path", "")) or None),
                "rank_score": _to_decimal(stock.get("rank_score")),
                "force_watchlist": bool(stock.get("force_watchlist")),
                "price": _to_decimal(stock.get("price")),
                "day_volume": _to_int(stock.get("volume")),
                "float_used": _to_int(stock.get("shares_outstanding")),
                "change_pct": _to_decimal(stock.get("change_pct")),
            }
        )

    for raw_symbol in faded_symbols or []:
        symbol = str(raw_symbol or "").upper().strip()
        if not symbol:
            continue
        rows.append(
            {
                "trade_date": trade_date,
                "symbol": symbol,
                "event_type": "FADE",
                "event_at": now,
                "confirm_path": None,
                "rank_score": None,
                "force_watchlist": None,
                "price": None,
                "day_volume": None,
                "float_used": None,
                "change_pct": None,
            }
        )

    for raw_symbol in dropped_retention_symbols or []:
        symbol = str(raw_symbol or "").upper().strip()
        if not symbol:
            continue
        rows.append(
            {
                "trade_date": trade_date,
                "symbol": symbol,
                "event_type": "RETENTION_DROP",
                "event_at": now,
                "confirm_path": None,
                "rank_score": None,
                "force_watchlist": None,
                "price": None,
                "day_volume": None,
                "float_used": None,
                "change_pct": None,
            }
        )

    if not rows:
        return

    try:
        with session_factory() as session:
            for params in rows:
                if params["event_type"] == "CONFIRM":
                    params["reconfirm_seq"] = int(
                        session.execute(
                            _COUNT_CONFIRM_SQL,
                            {"trade_date": params["trade_date"], "symbol": params["symbol"]},
                        ).scalar()
                        or 0
                    )
                else:
                    params["reconfirm_seq"] = 0
                if params["event_type"] == "FADE":
                    # Skip a repeat FADE: only record one when the symbol's latest event today
                    # is a CONFIRM (or there is none yet) — i.e. a genuine confirmed->faded edge.
                    latest_event = session.execute(
                        _LATEST_EVENT_SQL,
                        {"trade_date": params["trade_date"], "symbol": params["symbol"]},
                    ).scalar()
                    if latest_event in ("FADE", "RETENTION_DROP"):
                        continue
                session.execute(_INSERT_SQL, params)
            session.commit()
    except Exception:
        logger.exception("scanner_confirmed_capture: failed to persist events")
