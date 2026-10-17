"""Select one ORB venue from the existing SEC router without dispatching orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from datetime import time
from zoneinfo import ZoneInfo

from project_mai_tai.momentum_live_sec import CountryRoute, CountryRouter

ORB_ROUTE_QUANTITY = Decimal("10")


@dataclass(frozen=True)
class OrbCountrySelection:
    symbol: str
    broker_account_name: str
    country: CountryRoute
    quantity: Decimal = field(default=ORB_ROUTE_QUANTITY, init=False)


def select_orb_country_route(
    router: CountryRouter,
    symbol: str,
    now: datetime,
    *,
    schwab_account: str,
    webull_account: str,
) -> OrbCountrySelection:
    """Retain the router's decision; a missing binding never falls back to a sibling."""
    if not symbol or symbol != symbol.strip().upper():
        raise ValueError("orb_country_symbol_invalid")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("orb_country_clock_invalid")
    country = router.route_for(symbol, now)
    if country.broker is None:
        raise ValueError("orb_country_entry_window_closed")
    if country.broker not in {"schwab", "webull"}:
        raise ValueError("orb_country_broker_invalid")
    account = schwab_account if country.broker == "schwab" else webull_account
    if not account or account != account.strip() or not account.startswith("live:"):
        raise ValueError("orb_country_account_missing")
    if schwab_account and schwab_account == webull_account:
        raise ValueError("orb_country_account_collision")
    return OrbCountrySelection(symbol, account, country)


def country_metadata(selection: OrbCountrySelection) -> dict[str, str]:
    return {
        "orb_country_route": "true",
        "orb_country_broker": str(selection.country.broker),
        "orb_country_reason": selection.country.reason,
        "orb_country_status": selection.country.status,
        "orb_country_as_of": selection.country.as_of.isoformat() if selection.country.as_of else "",
    }


def selection_from_metadata(symbol: str, account: str, metadata: dict) -> OrbCountrySelection | None:
    if metadata.get("orb_country_route") != "true":
        return None
    stamp = metadata.get("orb_country_as_of")
    reason = metadata["orb_country_reason"]
    return OrbCountrySelection(symbol, account, CountryRoute(
        metadata["orb_country_broker"], reason, metadata["orb_country_status"],
        True if reason == "sec_us" else False if reason == "sec_non_us" else None,
        None, datetime.fromisoformat(stamp) if stamp else None,
    ))


def load_working_routes(factory) -> dict[str, OrbCountrySelection | None]:
    from sqlalchemy import or_, select
    from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Strategy, VirtualPosition

    with factory() as session:
        rows = session.execute(select(BrokerOrder, BrokerAccount.name).join(Strategy).join(
            BrokerAccount, BrokerAccount.id == BrokerOrder.broker_account_id,
        ).outerjoin(VirtualPosition, (
            (VirtualPosition.strategy_id == BrokerOrder.strategy_id)
            & (VirtualPosition.broker_account_id == BrokerOrder.broker_account_id)
            & (VirtualPosition.symbol == BrokerOrder.symbol)
        )).where(Strategy.code == "orb_schwab", BrokerOrder.side == "buy", or_(
            BrokerOrder.status.is_(None),
            BrokerOrder.status.not_in({"filled", "cancelled", "rejected", "aborted", "expired"}),
            VirtualPosition.quantity > 0,
            BrokerOrder.payload["orbroute1_protection"]["phase"].as_string().in_(
                {"submitting", "accepted", "working", "unknown"}),
        )).order_by(BrokerOrder.submitted_at, BrokerOrder.id)).all()
        routes = {}
        owners = {}
        for order, account in rows:
            selected = selection_from_metadata(order.symbol, account, order.payload or {})
            previous_owner = owners.get(order.symbol)
            if previous_owner is not None and previous_owner != account:
                raise ValueError("orb_country_multiple_route_owners")
            owners[order.symbol] = account
            routes[order.symbol] = selected
        return routes


def routed_orb_metadata(selection: OrbCountrySelection, level: Decimal) -> dict[str, str]:
    from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata

    metadata = build_orb_schwab_bracket_metadata(level)
    if selection.country.broker == "webull":
        metadata["orb_target_price"] = metadata["bracket_target_price"]
        metadata["orb_protect_price"] = metadata["bracket_stop_price"]
        for key in tuple(metadata):
            if key.startswith("bracket") or key == "native_oco_bracket":
                metadata.pop(key)
    metadata.update(country_metadata(selection))
    metadata.update(orb_intended_break_level=format(level, "f"), session="NORMAL", extended_hours="false")
    return metadata


def routed_orb_intent_refusal(event, settings, now: datetime) -> str | None:
    """Strict routed contract; cleanup stays reachable when new admission is OFF."""
    payload, metadata = event.payload, event.payload.metadata
    if event.source_service != "orb-schwab" or payload.strategy_code != "orb_schwab":
        return "orb_country_wrong_source"
    if not payload.symbol or payload.symbol != payload.symbol.strip().upper():
        return "orb_country_invalid_symbol"
    if now.tzinfo is None or now.utcoffset() is None:
        return "orb_country_invalid_clock"
    broker = metadata.get("orb_country_broker")
    account = (settings.strategy_schwab_1m_v2_account_name if broker == "schwab"
               else settings.orb_webull_account_name if broker == "webull" else "")
    if (not account or not account.startswith("live:") or payload.broker_account_name != account
            or settings.provider_for_account(account) != broker):
        return "orb_country_wrong_account"
    reason = metadata.get("orb_country_reason")
    if reason not in ({"sec_us"} if broker == "schwab" else {"sec_non_us", "sec_unknown"}):
        return "orb_country_invalid_provenance"
    local = now.astimezone(ZoneInfo("America/New_York"))
    if payload.intent_type == "cancel" and metadata.get("orb_schwab_reprice") != "true":
        expected = {key: metadata.get(key, "") for key in country_metadata(
            OrbCountrySelection(payload.symbol, account, CountryRoute(broker, reason, "", None, None, None))
        )}
        expected["orb_schwab_cancel"] = "true"
        return None if payload.side == "buy" and payload.quantity == 0 and metadata == expected else "orb_country_invalid_cancel"
    if payload.intent_type == "close":
        from uuid import UUID
        from project_mai_tai.orb_schwab_exits import ATR_REASON, BODY_REASON
        try:
            UUID(metadata["entry_order_id"])
            UUID(metadata["entry_fill_id"])
        except (KeyError, TypeError, ValueError):
            return "orb_country_exit_identity_missing"
        keys = set(country_metadata(OrbCountrySelection(
            payload.symbol, account, CountryRoute(broker, reason, "", None, None, None))))
        keys |= {"orb_schwab_exit", "entry_order_id", "entry_fill_id"}
        if (payload.side != "sell" or not payload.quantity.is_finite()
                or not 0 < payload.quantity <= ORB_ROUTE_QUANTITY
                or payload.reason not in {BODY_REASON, ATR_REASON}
                or metadata.get("orb_schwab_exit") != "true" or set(metadata) != keys):
            return "orb_country_invalid_exit"
        return None if local.weekday() < 5 and time(9, 30) <= local.time() < time(16) else "orb_country_exit_window_closed"
    reprice = payload.intent_type == "cancel" and metadata.get("orb_schwab_reprice") == "true"
    if (not settings.orb_country_routing_enabled or not settings.orb_live_schwab_orders_enabled
            or not settings.orb_enabled):
        return "orb_country_disabled"
    if (payload.side != "buy" or payload.quantity != (0 if reprice else ORB_ROUTE_QUANTITY)
            or payload.intent_type != ("cancel" if reprice else "open")):
        return "orb_country_invalid_entry"
    allowed = (time(9, 29) <= local.time() < time(9, 30, 15)) if reprice else (
        time(9, 28) <= local.time() < time(9, 28, 15))
    if not reprice and "orb_deferred_macd_bar_close" in metadata:
        close = local.replace(hour=9, minute=28, second=0, microsecond=0)
        allowed = (time(9, 28) <= local.time() < time(9, 29, 30)
                   and metadata["orb_deferred_macd_bar_close"] == close.astimezone(UTC).isoformat())
    if local.weekday() >= 5 or not allowed:
        return "orb_country_entry_window_closed"
    try:
        level = Decimal(metadata["orb_intended_break_level"])
        selection = OrbCountrySelection(payload.symbol, account, CountryRoute(
            broker, reason, metadata["orb_country_status"], None, None,
            datetime.fromisoformat(metadata["orb_country_as_of"]) if metadata["orb_country_as_of"] else None))
        if selection.country.as_of is not None and (
            selection.country.as_of.tzinfo is None or selection.country.as_of.utcoffset() is None
        ):
            return "orb_country_invalid_provenance"
        expected = routed_orb_metadata(selection, level)
    except (KeyError, ValueError, ArithmeticError):
        return "orb_country_invalid_plan"
    if reprice:
        expected["orb_schwab_reprice"] = "true"
    if "orb_deferred_macd_bar_close" in metadata:
        expected["orb_deferred_macd_bar_close"] = metadata["orb_deferred_macd_bar_close"]
    return None if metadata == expected else "orb_country_invalid_plan"
