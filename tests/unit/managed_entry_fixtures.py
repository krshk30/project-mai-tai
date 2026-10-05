"""Explicit entry bindings for exit-path fixtures that predate OWNMIX1."""
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Strategy, TradeIntent


def bind_managed_entry(session, row, *, entry=None):
    if entry is None:
        strategy = session.scalar(select(Strategy).where(Strategy.code == row.strategy_code))
        account = session.scalar(select(BrokerAccount).where(
            BrokerAccount.name == row.broker_account_name))
        intent = TradeIntent(
            strategy_id=strategy.id, broker_account_id=account.id, symbol=row.symbol,
            side="buy", intent_type="open", quantity=Decimal(row.original_quantity),
            reason="explicit fixture entry", status="filled", payload={})
        session.add(intent)
        session.flush()
        identity = str(uuid4())
        entry = BrokerOrder(
            intent_id=intent.id, strategy_id=strategy.id, broker_account_id=account.id,
            client_order_id="fixture-entry-" + identity, broker_order_id="fixture-parent-" + identity,
            symbol=row.symbol, side="buy", order_type="market", time_in_force="day",
            quantity=Decimal(row.original_quantity), status="filled",
            payload={"fanout_leg": "webull", "native_oco_bracket": "false"}
            if account.provider == "webull" else {"native_oco_bracket": "true"},
        )
        session.add(entry)
        session.flush()
    if entry.intent_id is None:
        intent = TradeIntent(
            strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            symbol=row.symbol, side="buy", intent_type="open", quantity=entry.quantity,
            reason="explicit fixture entry", status="filled", payload={},
        )
        session.add(intent)
        session.flush()
        entry.intent_id = intent.id
    row.entry_order_id = entry.id
    row.entry_client_order_id = entry.client_order_id
    session.flush()
    return entry


def set_protect_base(service, sessions, account, symbol, base):
    with sessions() as session:
        row = service.store.get_open_managed_position(
            session, broker_account_name=account, symbol=symbol)
        entry = session.get(BrokerOrder, row.entry_order_id)
        entry.payload = {**entry.payload, "fanout_leg": "webull",
                         "native_oco_bracket": "false",
                         "webull_protect_base_client_order_id": base}
        session.commit()
    service._webull_protect_base[(account, symbol)] = base
