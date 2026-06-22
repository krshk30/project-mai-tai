"""Shared tick-timestamp normalization.

Market-data feeds label the field ``timestamp_ns`` but the magnitude varies by
source: Massive/Polygon LIVE websocket trades carry MILLISECONDS (13-digit),
Massive REST historical trades carry true NANOSECONDS (19-digit), and Schwab
carries ns. A naive ``value / 1e9`` turns a millisecond value into a 1970
timestamp — the bug that silently broke ORB (see ``orb_app._normalize_trade_ts_ns``
and ``strategy_engine_app._normalize_tick_timestamp_ns``, which both reimplement
this ladder). This is the canonical home; new consumers MUST normalize before
storing or bucketing so a 1970 timestamp can never be persisted.
"""

from __future__ import annotations

from datetime import UTC, datetime


def normalize_ts_ns(value: int | float | str | None) -> int | None:
    """Coerce a feed timestamp of unknown unit (s / ms / us / ns) to nanoseconds.

    Detects the unit by magnitude and scales up to ns. Returns None for missing
    or non-numeric input.
    """
    if value is None or value == "":
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v >= 1_000_000_000_000_000_000:  # already nanoseconds (>= ~2001 in ns)
        return v
    if v >= 1_000_000_000_000_000:  # microseconds
        return v * 1_000
    if v >= 1_000_000_000_000:  # milliseconds (Massive/Polygon live WS)
        return v * 1_000_000
    if v >= 1_000_000_000:  # seconds
        return v * 1_000_000_000
    return None


def ns_to_datetime(ns: int) -> datetime:
    """Convert normalized nanoseconds to a tz-aware UTC datetime."""
    return datetime.fromtimestamp(ns / 1e9, tz=UTC)
