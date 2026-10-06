"""Local-only safety probe, not a passing acceptance test or production receipt.

Use the existing SQLite/FakeRedis test harness and a mock adapter. A mock wire
followed by an exception leaves no opening rows; the current candidate assessor
nevertheless accepts the resulting no-target cancellation receipts.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, TradeIntent
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.oms.service import OmsRiskService

ROOT = Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class MockWireThenException:
    def __init__(self):
        self.requests = []

    async def submit_order(self, request):
        self.requests.append(request)
        raise RuntimeError("mock response lost after possible wire")

    async def fetch_order_update(self, request):
        return None

    async def list_account_positions(self, broker_account_name):
        return []


async def main():
    harness = load("clearwait1_probe_oms", "tests/unit/test_oms_intent_refusal_provenance.py")
    controls = load("clearwait1_probe_controls", "tests/unit/test_clearwait1_removed_wait.py")
    factory = harness._session_factory()
    adapter = MockWireThenException()
    service = harness._service(factory, adapter=adapter)
    request, rows = controls._recorded("AIXI", include_tickets=False)
    event = harness._v2_open(
        request.symbol,
        account=controls.PRIMARY,
        metadata={
            "reference_price": "1.00",
            "fanout_segment_id": str(request.opportunity_id),
            "fanout_slot": "resting",
            "fanout_slot_id": fanout_slot_id(
                strategy_code="schwab_1m_v2", symbol=request.symbol,
                segment_id=request.opportunity_id, slot="resting",
            ),
        },
    )
    failure = None
    with patch.object(OmsRiskService, "_market_is_fillable", return_value=True):
        try:
            await service.process_trade_intent(event)
        except RuntimeError as exc:
            failure = str(exc)
    with factory() as session:
        durable_intents = list(session.scalars(select(TradeIntent)).all())
        durable_orders = list(session.scalars(select(BrokerOrder)).all())
    assert len(adapter.requests) == 1, "probe did not reach the mock wire"
    assert failure == "mock response lost after possible wire"
    assert not durable_intents and not durable_orders
    request = replace(request, requested_at_ms=int(datetime.now(UTC).timestamp() * 1000))
    for account in request.account_names:
        cancel = harness._v2_open(
            request.symbol,
            account=account,
            metadata={
                "clearwait_removal_token": request.token,
                "clearwait_opportunity_id": str(request.opportunity_id),
                "clearwait_buy_only": "true",
            },
        )
        cancel.payload.intent_type = "cancel"
        await service.process_trade_intent(cancel)
    with factory() as session:
        cancel_receipts = list(session.scalars(select(TradeIntent)).all())
        accounts = {a.id: a.name for a in session.scalars(select(BrokerAccount)).all()}
        session.expunge_all()
    assert len(cancel_receipts) == 2
    assert all(i.payload.get("refusal_origin") == "skipped_before_submit"
               and i.payload.get("refusal_code") == "cancel_target_not_found"
               for i in cancel_receipts)
    assert len(adapter.requests) == 1, "no-target cancel unexpectedly called the mock adapter"
    rows["snapshots"][-1].payload = request.payload(active=True)
    rows["now"] = datetime.now(UTC) + timedelta(seconds=20)
    rows["accounts"] = accounts
    rows["orders"] = []
    if "order_events" in rows:
        rows["order_events"] = []
    rows["intents"] = cancel_receipts
    proof = controls.assess_removed_wait(request, **rows)
    print(json.dumps({
        "classification": "UNSAFE_COUNTEREXAMPLE" if proof.clear else "FAIL_CLOSED",
        "scope": "SQLite/FakeRedis/mock adapter only; no production connections",
        "mock_submit_calls": len(adapter.requests),
        "exception": failure,
        "durable_open_intents_after_rollback": len(durable_intents),
        "durable_orders_after_rollback": len(durable_orders),
        "actual_local_oms_no_target_cancel_receipts": len(cancel_receipts),
        "candidate_clear": proof.clear,
        "candidate_reason": proof.reason,
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
