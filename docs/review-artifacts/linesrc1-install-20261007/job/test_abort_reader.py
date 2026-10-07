"""[codex] Real canonical ORM proof; PostgreSQL size-only boundary controlled locally."""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from project_mai_tai.db.models import Base, BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill, Strategy, TradeIntent
import abort_reader as reader
from test_runner import NOW


@pytest.mark.parametrize("damage", [None,"no_wire","audit_missing","audit_broker","event_id","broker_id","fill",
    "intent_quantity","intent_account","intent_origin","other_strategy","second_order"])
def test_canonical_ordinary_abort_terminal_only_new_intent_old_ticket_never_released(monkeypatch,damage):
    engine=create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        account=BrokerAccount(name="live:orb",provider="webull",environment="live")
        foreign=BrokerAccount(name="foreign",provider="webull",environment="live")
        strategy=Strategy(code="schwab_1m_v2",name="controlled")
        session.add_all([account,foreign,strategy]); session.flush()
        code="rpg_old_buy_still_owned"
        md=dict(refusal_code=code,refusal_origin="client_abort",rpg_local_abort_no_wire="true",rpg_abort_event_id="CONTROLLED_EVENT")
        intent=TradeIntent(strategy_id=strategy.id,broker_account_id=account.id,symbol="RETO",side="buy",intent_type="open",
            quantity=Decimal("100"),reason="controlled",status="aborted",payload=dict(event_id="CONTROLLED_EVENT",refusal_code=code,refusal_origin="client_abort"))
        session.add(intent); session.flush()
        order=BrokerOrder(intent_id=intent.id,strategy_id=strategy.id,broker_account_id=account.id,client_order_id="CONTROLLED_NEW",
            symbol="RETO",side="buy",order_type="stop_limit",time_in_force="day",quantity=Decimal("100"),status="aborted",payload=deepcopy(md))
        old=BrokerOrder(strategy_id=strategy.id,broker_account_id=account.id,client_order_id="CONTROLLED_OLD",broker_order_id="OWNED",
            symbol="RETO",side="buy",order_type="stop_limit",time_in_force="day",quantity=Decimal("100"),status="accepted",payload={})
        session.add_all([order,old]); session.flush()
        audit=BrokerOrderEvent(order_id=order.id,event_type="aborted",event_source="client",event_at=NOW,payload=dict(reason=code,metadata=deepcopy(md)))
        if damage!="audit_missing": session.add(audit)
        if damage=="no_wire":
            order.payload={**md,"rpg_local_abort_no_wire":"false"}
            audit.payload=dict(reason=code,metadata=deepcopy(order.payload))
        elif damage=="audit_broker": audit.event_source="broker"
        elif damage=="event_id": intent.payload={**intent.payload,"event_id":"OTHER"}
        elif damage=="broker_id": order.broker_order_id="UNKNOWN_WIRE"
        elif damage=="fill": session.add(Fill(order_id=order.id,strategy_id=strategy.id,broker_account_id=account.id,
            symbol="RETO",side="buy",quantity=Decimal("0"),price=Decimal("1"),filled_at=NOW))
        elif damage=="intent_quantity": intent.quantity=Decimal("101")
        elif damage=="intent_account": intent.broker_account_id=foreign.id
        elif damage=="intent_origin": intent.payload={**intent.payload,"refusal_origin":"broker_reject"}
        elif damage=="other_strategy": strategy.code="other"
        elif damage=="second_order": session.add(BrokerOrder(intent_id=intent.id,strategy_id=strategy.id,broker_account_id=account.id,
            client_order_id="SECOND",symbol="RETO",side="buy",order_type="stop_limit",time_in_force="day",quantity=100,status="accepted",payload={}))
        session.commit()
        budget=Mock(); monkeypatch.setattr(reader,"evidence_budget",budget)
        rows=[dict(id=str(order.id),account=account.name,symbol="RETO",status="aborted"),
              dict(id=str(old.id),account=account.name,symbol="RETO",status="accepted")]
        pending=[dict(id=str(intent.id),account=account.name,symbol="RETO",status="aborted")]
        working,inflight,proof=reader.filter_rows(session,rows,pending)
        assert rows[1] in working and old.status=="accepted" and old.broker_order_id=="OWNED"
        assert budget.call_count==1
        if damage is None:
            assert working==[rows[1]] and inflight==[] and proof[0]["old_ticket_released"] is False
        elif damage=="second_order":
            assert inflight==pending
        else:
            assert working==rows and inflight==pending and proof==[]
    engine.dispose()


@pytest.mark.parametrize("size,count,journal,blocked",[(1_000_000,1024,4_000_000,False),
    (1_000_001,0,0,True),(0,1025,0,True),(0,0,4_000_001,True)])
def test_budget_aggregate_bounds_before_canonical_payload_load(size,count,journal,blocked):
    session=Mock(); session.execute.side_effect=[Mock(scalar_one=Mock(return_value=size)),Mock(one=Mock(return_value=(count,journal)))]
    if blocked:
        with pytest.raises(ValueError): reader.evidence_budget(session,uuid4())
    else: reader.evidence_budget(session,uuid4())
    assert not session.get.called
