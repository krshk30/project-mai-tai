"""ORBPASTLEVEL1: bounded Schwab wire selection, not a fill model."""

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_UP

from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata


POLICY = "ORBPASTLEVEL1-v1"


def round_buy(value: Decimal) -> Decimal:
    if not value.is_finite() or value <= 0:
        raise ValueError("orb_pastlevel_invalid_price")
    rounded = value.quantize(Decimal(".01") if value > 1 else Decimal(".0001"), rounding=ROUND_UP)
    # Rounding a sub-dollar price across $1 changes the legal tick.
    return rounded.quantize(Decimal(".01"), rounding=ROUND_UP) if rounded > 1 else rounded


def select_entry(level: Decimal, quote: dict | None, now: datetime, *, max_age_ms: int) -> dict[str, str]:
    """Raises on missing/stale/above-cap input; caller retains the unsent plan."""
    if now.tzinfo is None or now.utcoffset() is None or max_age_ms <= 0:
        raise ValueError("orb_pastlevel_invalid_clock")
    try:
        at = quote["received_at"]
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("orb_pastlevel_quote_clock")
        age = (now - at).total_seconds() * 1000
        ask = Decimal(str(quote["ask"]))
    except (KeyError, TypeError, AttributeError, InvalidOperation) as exc:
        raise ValueError("orb_pastlevel_quote_missing") from exc
    if not 0 <= age <= max_age_ms:
        raise ValueError("orb_pastlevel_quote_stale")
    rounded_ask = round_buy(ask)
    trigger = round_buy(level)
    metadata = build_orb_schwab_bracket_metadata(level, tick_cap=True)
    cap = Decimal(metadata["limit_price"])
    if rounded_ask > cap:
        raise ValueError("orb_pastlevel_ask_above_cap")
    entry_type = "STOP_LIMIT" if ask < trigger else "LIMIT"
    metadata.update(
        order_type=entry_type, bracket_entry_type=entry_type,
        limit_price=format(cap if entry_type == "STOP_LIMIT" else rounded_ask, "f"),
        orb_pastlevel_policy=POLICY, orb_buy_cap_nominal_pct="0.5",
        orb_buy_cap_effective_price=format(cap, "f"),
        orb_buy_cap_effective_pct=format((cap / trigger - 1) * 100, "f"),
        orb_quote_ask=format(ask, "f"), orb_quote_at=at.isoformat(),
        orb_quote_max_age_ms=str(max_age_ms),
    )
    # The legacy field describes the nominal plan, never the effective tick tolerance.
    metadata["orb_buy_cap_pct"] = "0.5"
    return metadata


def assert_same_selection(level: Decimal, quote: dict | None, now: datetime,
                          selected: dict[str, str], *, max_age_ms: int) -> None:
    current = select_entry(level, quote, now, max_age_ms=max_age_ms)
    # A newer quote may reaffirm the exact previewed wire shape/prices. Never
    # chase a different price/shape without a new explicit plan and preview.
    evidence = {"orb_quote_at", "orb_quote_ask"}
    if any(current[key] != selected.get(key) for key in current if key not in evidence):
        raise ValueError("orb_pastlevel_quote_changed_wait_next_plan")
