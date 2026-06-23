"""Extended-hours order-routing metadata — shared leaf (import-clean, pure).

Single source of truth for the entry-side extended-hours handoff that the
strategy-engine bots (macd_30s / schwab_1m) have always sent and that the
isolated `schwab_1m_v2` bot now reuses verbatim. During regular trading hours
``order_routing_metadata`` returns ``{}`` (so the order stays market/NORMAL,
byte-identical to RTH today); in extended hours it stamps ``session=AM|PM`` +
``order_type=limit`` + ``limit_price`` so the order can actually fill pre/post.

Extracted from ``services.strategy_engine_app`` with NO behavior change — the
legacy module now imports these names from here. No I/O, no settings, stdlib
only, so any bot can import it without pulling in the strategy-engine module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

EASTERN_TZ = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _format_limit_price(value: float | str | Decimal | None) -> str | None:
    if value is None:
        return None
    try:
        return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")
    except Exception:
        return None


def extended_hours_session(now: datetime | None = None) -> str | None:
    current = (now or utcnow()).astimezone(EASTERN_TZ)
    regular_open = current.replace(hour=9, minute=30, second=0, microsecond=0)
    regular_close = current.replace(hour=16, minute=0, second=0, microsecond=0)
    if regular_open <= current < regular_close:
        return None
    return "AM" if current < regular_open else "PM"


def order_routing_metadata(*, price: str, side: str, now: datetime | None = None) -> dict[str, str]:
    session = extended_hours_session(now)
    if session is None:
        return {}
    return {
        "session": session,
        "order_type": "limit",
        "time_in_force": "day",
        "extended_hours": "true",
        "limit_price": price,
        "reference_price": price,
        "price_source": "ask" if side == "buy" else "bid",
    }
