"""ORBPASTLEVEL1: bounded provider-aware ORB wire selection, not a fill model."""

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP

from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata
from project_mai_tai.buy_price_floor import buy_price_allowed


POLICY = "ORBPASTLEVEL1-v1"


def round_buy(value: Decimal, *, provider: str = "schwab") -> Decimal:
    if provider not in {"schwab", "webull"}:
        raise ValueError("orb_pastlevel_provider_invalid")
    if not value.is_finite() or value <= 0:
        raise ValueError("orb_pastlevel_invalid_price")
    cent_tick = value >= 1 if provider == "webull" else value > 1
    rounded = value.quantize(Decimal(".01") if cent_tick else Decimal(".0001"), rounding=ROUND_UP)
    # Rounding a sub-dollar price across $1 changes the legal tick.
    cent_tick = rounded >= 1 if provider == "webull" else rounded > 1
    return rounded.quantize(Decimal(".01"), rounding=ROUND_UP) if cent_tick else rounded


def select_entry(level: Decimal, quote: dict | None, now: datetime, *, max_age_ms: int,
                 provider: str = "schwab", symbol: str = "UNKNOWN") -> dict[str, str]:
    """Fresh asks choose the new shape; missing/stale asks retain legacy STOP_LIMIT."""
    if now.tzinfo is None or now.utcoffset() is None or max_age_ms <= 0:
        raise ValueError("orb_pastlevel_invalid_clock")
    if not buy_price_allowed(level, bot="orb_schwab", symbol=symbol):
        raise ValueError("PRICE1-REFUSED")
    trigger = round_buy(level, provider=provider)
    metadata = build_orb_schwab_bracket_metadata(level, tick_cap=True)
    cap = Decimal(metadata["limit_price"])
    if provider == "webull":
        # Preserve healthy legacy caps; only the strict Webull stop-limit gap
        # permits lifting a collapsed nominal band by one provider-legal tick.
        tick = Decimal(".01") if trigger >= 1 else Decimal(".0001")
        nominal = level * Decimal("1.005")
        cap = max(nominal.quantize(Decimal(".01") if nominal >= 1 else Decimal(".0001"), rounding=ROUND_DOWN),
                  round_buy(trigger + tick, provider=provider))
        if cap >= Decimal(metadata["bracket_target_price"]):
            raise ValueError("orb_pastlevel_cap_not_below_target")
        metadata.update(stop_price=format(trigger, "f"), reference_price=format(trigger, "f"),
                        limit_price=format(cap, "f"),
                        orb_target_price=metadata["bracket_target_price"],
                        orb_protect_price=metadata["bracket_stop_price"])
        # Webull opens are single-leg; post-fill protection owns the exit pair.
        metadata = {key: value for key, value in metadata.items()
                    if not key.startswith("bracket") and key != "native_oco_bracket"}
    metadata.update(
        orb_pastlevel_policy=POLICY, orb_buy_cap_nominal_pct="0.5",
        orb_buy_cap_effective_price=format(cap, "f"),
        orb_buy_cap_effective_pct=format((cap / trigger - 1) * 100, "f"),
        orb_quote_max_age_ms=str(max_age_ms), orb_quote_ask="", orb_quote_at="",
        orb_quote_selection="legacy_no_fresh_ask",
    )
    try:
        at = quote["received_at"]
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("orb_pastlevel_quote_clock")
        age = (now - at).total_seconds() * 1000
        ask = Decimal(str(quote["ask"]))
    except (KeyError, TypeError, AttributeError, InvalidOperation):
        metadata["orb_quote_unavailable_reason"] = "orb_pastlevel_quote_missing"
        return metadata
    if age < 0:
        raise ValueError("orb_pastlevel_quote_stale")
    if age > max_age_ms:
        metadata["orb_quote_unavailable_reason"] = "orb_pastlevel_quote_stale"
        return metadata
    rounded_ask = round_buy(ask, provider=provider)
    if rounded_ask > cap:
        raise ValueError("orb_pastlevel_ask_above_cap")
    past_trigger = ask > trigger if provider == "webull" else ask >= trigger
    entry_type = "LIMIT" if past_trigger else "STOP_LIMIT"
    metadata.update(
        order_type=entry_type,
        limit_price=format(cap if entry_type == "STOP_LIMIT" else rounded_ask, "f"),
        orb_quote_ask=format(ask, "f"), orb_quote_at=at.isoformat(),
        orb_quote_selection="fresh_ask",
    )
    if provider == "schwab":
        metadata["bracket_entry_type"] = entry_type
    # The legacy field describes the nominal plan, never the effective tick tolerance.
    metadata["orb_buy_cap_pct"] = "0.5"
    return metadata


def assert_same_selection(level: Decimal, quote: dict | None, now: datetime,
                          selected: dict[str, str], *, max_age_ms: int,
                          provider: str = "schwab", symbol: str = "UNKNOWN") -> None:
    current = select_entry(level, quote, now, max_age_ms=max_age_ms, provider=provider, symbol=symbol)
    if (selected.get("orb_quote_selection") != "legacy_no_fresh_ask"
            and current["orb_quote_selection"] == "legacy_no_fresh_ask"):
        # Applicability is decided before preview, not a late fallback that
        # could turn a previewed LIMIT into an unpreviewed STOP_LIMIT.
        raise ValueError(current["orb_quote_unavailable_reason"])
    # A newer quote may reaffirm the exact previewed wire shape/prices. Never
    # chase a different price/shape without a new explicit plan and preview.
    evidence = {"orb_quote_at", "orb_quote_ask", "orb_quote_unavailable_reason"}
    if any(current[key] != selected.get(key) for key in current if key not in evidence):
        raise ValueError("orb_pastlevel_quote_changed_wait_next_plan")
