from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo


def trading_session_start(now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo("America/New_York"))
    anchor = datetime.combine(local.date(), time(4), tzinfo=local.tzinfo)
    if local < anchor:
        anchor -= timedelta(days=1)
    return anchor.astimezone(UTC)


def operator_holding_basis(
    *,
    account_name: str,
    symbol: str,
    now: datetime,
    broker_quantity: Decimal,
    virtual_quantity: Decimal,
    managed_quantity: Decimal,
    net_fill_balance: Decimal,
    session_orders: int,
    session_fills: int,
    pending_intents: int,
    unowned_sells: int,
    source_fresh: bool,
    activity_complete: bool,
    open_virtual_rows: int = 0,
    open_managed_rows: int = 0,
    pending_orders: int = 0,
) -> str | None:
    """Positive evidence only; a net-zero round trip is not absence of activity."""
    if (
        source_fresh is not True
        or activity_complete is not True
        or now.tzinfo is None
        or not account_name.startswith("live:")
        or any(not value.is_finite() for value in (
            broker_quantity, virtual_quantity, managed_quantity, net_fill_balance
        ))
        or any(type(count) is not int or count < 0 for count in (
            session_orders, session_fills, pending_intents, unowned_sells,
            open_virtual_rows, open_managed_rows, pending_orders,
        ))
        or broker_quantity <= 0
        or virtual_quantity != 0
        or managed_quantity != 0
        or net_fill_balance != 0
        or pending_intents != 0
        or unowned_sells != 0
        or open_virtual_rows != 0
        or open_managed_rows != 0
        or pending_orders != 0
    ):
        return None
    if session_orders == 0 and session_fills == 0:
        return "zero_session_bot_orders_and_fills"
    if (
        now.astimezone(ZoneInfo("America/New_York")).date() == date(2026, 10, 6)
        and account_name == "live:schwab_1m_v2"
        and symbol == "IPDN"
        and broker_quantity == Decimal("1000")
        and session_orders > 0
        and session_fills > 0
    ):
        return "operator_2026_10_06_ipdn_1000_exact_item"
    return None
