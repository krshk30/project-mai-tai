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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import text

from project_mai_tai.symbol_case import raw_symbol_is_uppercase

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")

# ⭐ A scanner `confirmed_at` is TIME-ONLY ("09:46:52 AM ET"), so it has to be re-dated onto the
# trade date. When the scanner carries a stale confirm across sessions, yesterday's time gets
# stamped onto TODAY and can land in the FUTURE (live 2026-07-28: BIYA/ENTX rows). A confirm cannot
# legitimately be later than the moment we observed it, so a future stamp is PROOF the value is
# stale — we fall back to the observation time rather than writing a fictional one into the durable
# backtest feed (#415), which reads these rows as ground truth.
#
# The tolerance covers clock skew between the scanner and this process ONLY. It is deliberately
# small: a real confirm is always in the past, so anything beyond a skew-sized window is staleness,
# not a timing artefact.
FUTURE_CONFIRM_TOLERANCE_SECS = 60.0


def session_confirm_clock(raw: object, now: datetime) -> dict:
    """Proposed 04ET research-key clock; UNKNOWN never manufactures now.

    No source/session policy, scanner membership or trading state is changed.
    Old calendar parser below remains exact for the default-OFF writer.
    """
    if not isinstance(now, datetime) or now.utcoffset() is None:
        return dict(status="UNKNOWN", reason="observation_clock_unknown", trade_date=None, event_at=None)
    observed = now.astimezone(EASTERN)
    day = observed.date() - timedelta(days=1) if observed.hour < 4 else observed.date()
    failure = dict(status="UNKNOWN", reason="confirmed_clock_unknown", trade_date=day, event_at=None)
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 64:
        return failure
    cleaned = raw.strip().replace(" ET", "").replace("ET", "").strip()
    parsed = None
    for fmt in ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return failure
    event_day = day if parsed.hour >= 4 else day + timedelta(days=1)
    candidate = datetime(event_day.year, event_day.month, event_day.day,
                         parsed.hour, parsed.minute, parsed.second, tzinfo=EASTERN)
    roundtrip = candidate.astimezone(timezone.utc).astimezone(EASTERN)
    if roundtrip.replace(tzinfo=None) != candidate.replace(tzinfo=None):
        return dict(failure, reason="nonexistent_local_confirm_clock")
    if candidate.utcoffset() != candidate.replace(fold=1).utcoffset():
        return dict(failure, reason="ambiguous_local_confirm_clock")
    if (candidate.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds() > FUTURE_CONFIRM_TOLERANCE_SECS:
        return dict(failure, reason="future_confirm_clock_unknown")
    return dict(status="PROVEN_CLOCK", reason="scanner_04et_session_key",
                trade_date=day, event_at=candidate)


def shadow_session_keys(*, now: datetime, all_confirmed) -> dict:
    """Bounded, JSON-compatible would-write keys; NEVER opens a session/DB.

    This does not mute LOW_VOLUME or activate the corrected writer. Main owns
    Settings/call-site and L4 ledger integration; unknown clock is not no event.
    """
    rows = []
    for stock in all_confirmed:
        if len(rows) >= 1000:
            raise ValueError("scanner_shadow_frame_overflow_no_truncation")
        symbol = stock.get("ticker")
        if (not raw_symbol_is_uppercase(symbol) or len(symbol) > 32
                or any(not (c.isascii() and (c.isalnum() or c in ".-")) for c in symbol)):
            raise ValueError("scanner_shadow_symbol_unknown")
        clock = session_confirm_clock(stock.get("confirmed_at"), now)
        rows.append(dict(symbol=symbol, status=clock["status"], reason=clock["reason"],
            trade_date=clock["trade_date"].isoformat() if clock["trade_date"] else None,
            event_at=clock["event_at"].isoformat() if clock["event_at"] else None))
    return dict(schema="SMARTALERT1_SCANNER_SESSION_SHADOW_V1", shadow_only=True,
                observed_at=now.isoformat() if isinstance(now, datetime) and now.utcoffset() is not None else None,
                writes=0, rows=rows, unknown_is_not_absence=True)

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
        candidate = datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            parsed.hour,
            parsed.minute,
            parsed.second,
            tzinfo=EASTERN,
        )
        if (candidate - fallback).total_seconds() > FUTURE_CONFIRM_TOLERANCE_SECS:
            # Stale carry-over re-dated onto today. Say so loudly and use the observation time --
            # a future-dated CONFIRM silently poisons every window the backtest feed derives.
            logger.warning(
                "[SCANNER-CAPTURE-STALE] confirmed_at=%r re-dates to %s which is AFTER the "
                "observation time %s — treating as a stale carry-over and using the observation "
                "time instead",
                text_value, candidate.isoformat(), fallback.isoformat(),
            )
            return fallback
        return candidate
    return fallback


def capture_events(
    session_factory,
    *,
    trade_date: date,
    now: datetime,
    all_confirmed: Iterable[Mapping[str, object]],
    faded_symbols: Iterable[str],
    dropped_retention_symbols: Iterable[str],
    session_day_enabled: bool = False,
) -> None:
    """Persist CONFIRM / FADE / RETENTION_DROP rows. Never raises."""
    if session_factory is None:
        return

    effective_date = trade_date
    if session_day_enabled is True:
        session_clock = session_confirm_clock("04:00", now)
        if session_clock["trade_date"] is None:
            logger.warning("[SCANNER-CAPTURE-UNKNOWN] reason=observation_clock_unknown")
            return
        effective_date = session_clock["trade_date"]
    elif session_day_enabled is not False:
        logger.warning("[SCANNER-CAPTURE-UNKNOWN] reason=session_day_flag_unknown")
        return

    rows: list[dict[str, object]] = []

    for stock in all_confirmed or []:
        if not raw_symbol_is_uppercase(stock.get("ticker")):
            continue
        symbol = str(stock.get("ticker", "")).upper().strip()
        if not symbol:
            continue
        if session_day_enabled:
            clock = session_confirm_clock(stock.get("confirmed_at"), now)
            if clock["status"] != "PROVEN_CLOCK":
                logger.warning("[SCANNER-CAPTURE-UNKNOWN] symbol=%r reason=%s", symbol[:32], clock["reason"])
                continue  # Unproved event time, NOT proof that no CONFIRM occurred.
            event_at = clock["event_at"]
        else:
            event_at = _parse_confirmed_at(stock.get("confirmed_at"), trade_date, now)
        rows.append(
            {
                "trade_date": effective_date,
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
        if not raw_symbol_is_uppercase(raw_symbol):
            continue
        symbol = str(raw_symbol or "").upper().strip()
        if not symbol:
            continue
        rows.append(
            {
                "trade_date": effective_date,
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
        if not raw_symbol_is_uppercase(raw_symbol):
            continue
        symbol = str(raw_symbol or "").upper().strip()
        if not symbol:
            continue
        rows.append(
            {
                "trade_date": effective_date,
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
