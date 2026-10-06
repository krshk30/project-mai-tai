"""Persist captured intent audits in isolated replay databases, never infer wire absence."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from uuid import UUID

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload

AUDITS = json.loads((Path(__file__).parents[1] / "fixtures/t43_exact_legacy_aborts_20261006.json").read_text())


def seed_recorded_abort(h, job):
    request = job["replacement"]
    row = next(row for row in AUDITS["intents"]
               if row["payload"]["event_id"] == request["metadata"]["rpg_event_id"])
    assert row["account"] == request["broker_account_name"]
    assert Decimal(row["quantity"]) == Decimal(request["quantity"])
    seed_recorded_intent(h, row)


def seed_recorded_intent(h, row):
    """Use the captured creation time when that capture has no update column."""
    event = TradeIntentEvent(event_id=UUID(row["payload"]["event_id"]),
        source_service=row["payload"]["source_service"],
        payload=TradeIntentPayload(strategy_code=row["strategy"], broker_account_name=row["account"],
            symbol=row["symbol"], side=row["side"], intent_type=row["intent_type"],
            quantity=Decimal(row["quantity"]), reason=row["reason"], metadata=row["payload"]["metadata"]))
    with h.factory() as session:
        strategy = h.service.store.ensure_strategy(session, row["strategy"], name="v2")
        account = h.service.store.ensure_broker_account(session, row["account"],
                                                       provider="simulated", environment="test")
        intent = h.service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
        intent.id, intent.status, intent.payload = UUID(row["id"]), row["status"], deepcopy(row["payload"])
        intent.created_at = datetime.fromisoformat(row["created_at"])
        intent.updated_at = datetime.fromisoformat(row.get("updated_at", row["created_at"]))
        session.commit()
