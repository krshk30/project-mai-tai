"""B-full private controls; SOURCE ONLY, author never executes.

CONTROLLED metadata/venue responses. This is not a replay of the three cited
PCLA rows: their exact retained full preimages are still required from Main.
Reuse existing in-memory unit fixtures; no new runner/provider/database rig.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai import brokerdecides1_sync as census_source
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai import fanout_identity as identity_source
from project_mai_tai.fanout_identity import FANOUT_IDENTITY_KEYS, carry_fanout_identity
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.settings import Settings
from tests.unit.test_oms_store import build_test_session_factory
from test_oms_risk_service import FakeRedis, FakeWorkingOrderRefreshBrokerAdapter
from tests.unit.test_brokerdecides1_local_census import ACCOUNTS, attempt, census as census, order as census_order


def controlled_metadata():
    return {
        "clearwait_dispatch_token": "CONTROLLED-original-dispatch",
        "fanout_segment_id": "1793278800000",
        "fanout_slot": "resting",
        "fanout_slot_id": "CONTROLLED-original-slot",
        "fanout_attempt_id": "CONTROLLED-original-client",
        "fanout_predecessor_attempt_id": "CONTROLLED-previous-client",
        "fanout_identity_schema": "CONTROLLED-v1",
        "limit_price": "8.42",
    }


def event_for(account, *, intent_type="open", metadata=None):
    return TradeIntentEvent(source_service="CONTROLLED-b-full",
        payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=account,
            symbol="PCLA", side="buy", intent_type=intent_type, quantity=Decimal("36"),
            reason="CONTROLLED-reprice" if intent_type == "cancel" else "CONTROLLED-open",
            metadata=dict(metadata or {})))


def seed_order(session, store, provider):
    account = store.ensure_broker_account(session, "paper:b-full-" + provider,
                                          provider=provider, environment="test")
    strategy = store.ensure_strategy(session, "schwab_1m_v2", name="CONTROLLED")
    event = event_for(account.name, metadata=controlled_metadata())
    parent = store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
    order = store.get_or_create_order(session, intent=parent, strategy_id=strategy.id,
        broker_account_id=account.id, client_order_id="CONTROLLED-original-client",
        symbol="PCLA", side="buy", quantity=Decimal("36"), metadata=controlled_metadata(),
        broker_order_id="CONTROLLED-native-id", status="accepted", order_type="limit", time_in_force="day")
    assert order.payload["clearwait_dispatch_token"] == controlled_metadata()["clearwait_dispatch_token"]
    assert order.payload["fanout_segment_id"] == controlled_metadata()["fanout_segment_id"]
    # Typed nonbinding local state must follow each caller's original policy:
    # discarded by store replacement, retained by the already-overlay paths.
    order.payload = {**order.payload, "CONTROLLED_nested": {"truth": True, "count": 2},
                     "CONTROLLED_list": [1, "two"], "CONTROLLED_null": None}
    session.commit()
    session.refresh(order)  # Normalize SQLite's timezone representation before comparison.
    return strategy, account, parent, order


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("event_type,preserve_status", [
    ("accepted", False), ("cancelled", False), ("rejected", True), ("filled", False),
])
def test_sparse_report_overwrites_nonbindings_without_changing_parent_or_report_semantics(
        provider, event_type, preserve_status):
    sessions = build_test_session_factory()
    store = OmsStore()
    with sessions() as session:
        _, _, parent, order = seed_order(session, store, provider)
        before = deepcopy(order.payload)
        binding = (order.id, order.intent_id, order.strategy_id, order.broker_account_id,
                   order.client_order_id, order.symbol, order.side, order.quantity, order.submitted_at)
        parent_before = deepcopy(parent.payload)
        report = ExecutionReport(event_type, order.client_order_id, broker_order_id=order.broker_order_id,
            symbol=order.symbol, side="buy", intent_type="cancel", quantity=order.quantity,
            reason="CONTROLLED-refusal" if event_type == "rejected" else "CONTROLLED-reprice",
            metadata={"resting_entry_cancel": "true", "reason": "reprice"},
            reported_at=datetime(2026, 10, 29, 14, tzinfo=UTC), origin="broker")
        result = store.update_order_from_report(order, report=report,
            metadata=report.metadata, preserve_status=preserve_status)
        session.commit()
        session.refresh(order)
        assert result is order
        assert (order.id, order.intent_id, order.strategy_id, order.broker_account_id,
                order.client_order_id, order.symbol, order.side, order.quantity, order.submitted_at) == binding
        assert order.intent_id == parent.id and parent.payload == parent_before
        assert order.status == ("accepted" if preserve_status else event_type)
        expected = {**report.metadata, "clearwait_dispatch_token": before["clearwait_dispatch_token"],
                    "fanout_segment_id": before["fanout_segment_id"]}
        if event_type == "rejected":
            expected["reject_reason"] = "CONTROLLED-refusal"
        assert order.payload == expected
        assert order.broker_order_id == "CONTROLLED-native-id"


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("incoming", [
    {"limit_price": "8.43", "reason": "reprice"},
    {"clearwait_dispatch_token": "CONTROLLED-cancel-token", "fanout_segment_id": "999", "limit_price": "8.43"},
    {"clearwait_dispatch_token": "", "fanout_segment_id": "", "limit_price": "8.43"},
])
def test_existing_client_upsert_preserves_original_dispatch_and_parent(provider, incoming):
    sessions = build_test_session_factory()
    store = OmsStore()
    with sessions() as session:
        strategy, account, parent, order = seed_order(session, store, provider)
        before = deepcopy(order.payload)
        parent_before = order.intent_id
        cancel_parent = store.create_trade_intent(session, strategy=strategy, broker_account=account,
            event=event_for(account.name, intent_type="cancel", metadata=incoming))
        result = store.get_or_create_order(session, intent=cancel_parent, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id=order.client_order_id, symbol=order.symbol,
            side=order.side, quantity=order.quantity, metadata=incoming,
            broker_order_id=order.broker_order_id, status="accepted")
        session.commit()
        session.refresh(order)
        assert result.id == order.id and order.intent_id == parent_before == parent.id
        expected = {**incoming, "clearwait_dispatch_token": before["clearwait_dispatch_token"],
                    "fanout_segment_id": before["fanout_segment_id"]}
        assert order.payload == expected


def test_report_overwrite_keeps_only_dispatch_bindings_and_drops_stale_state():
    sessions = build_test_session_factory()
    store = OmsStore()
    with sessions() as session:
        _, _, _, target = seed_order(session, store, "webull")
        target.payload = {**target.payload, "reject_reason": "CONTROLLED-old-reject",
                          "bracket_stop_price": "8.20", "momlive_entry_never_dispatched": True}
        original = deepcopy(target.payload)
        incoming = {"reason": "CONTROLLED-new-report"}
        report = ExecutionReport("accepted", target.client_order_id, broker_order_id=target.broker_order_id,
                                 quantity=target.quantity, metadata=incoming)
        store.update_order_from_report(target, report=report, metadata=incoming)
        session.flush()
        assert target.payload == {**incoming, "clearwait_dispatch_token": original["clearwait_dispatch_token"],
                                  "fanout_segment_id": original["fanout_segment_id"]}
        assert not {"reject_reason", "bracket_stop_price", "momlive_entry_never_dispatched",
                    "fanout_slot", "fanout_slot_id"} & target.payload.keys()


def test_existing_client_overwrite_keeps_only_dispatch_bindings_and_drops_stale_state():
    sessions = build_test_session_factory()
    store = OmsStore()
    with sessions() as session:
        strategy, account, parent, target = seed_order(session, store, "webull")
        target.payload = {**target.payload, "reject_reason": "CONTROLLED-old-reject",
                          "bracket_stop_price": "8.20", "momlive_entry_never_dispatched": True}
        original = deepcopy(target.payload)
        incoming = {"limit_price": "8.43"}
        store.get_or_create_order(session, intent=parent, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id=target.client_order_id, symbol=target.symbol,
            side=target.side, quantity=target.quantity, metadata=incoming,
            broker_order_id=target.broker_order_id, status="accepted")
        assert target.payload == {**incoming, "clearwait_dispatch_token": original["clearwait_dispatch_token"],
                                  "fanout_segment_id": original["fanout_segment_id"]}
        assert not {"reject_reason", "bracket_stop_price", "momlive_entry_never_dispatched",
                    "fanout_slot", "fanout_slot_id"} & target.payload.keys()


def test_merge_preserves_exact_invalid_identity_and_json_types_without_fabricating_tokens():
    original = {"clearwait_dispatch_token": " ", "fanout_segment_id": 0,
                "nested": {"a": [False, None, 3]}, "flag": False}
    incoming = {"clearwait_dispatch_token": "CONTROLLED-new", "fanout_segment_id": "123", "reason": "reprice"}
    before = deepcopy(original)
    merged = identity_source.merge_broker_order_payload(original, incoming)
    assert original == before and merged["clearwait_dispatch_token"] == " "
    assert merged["fanout_segment_id"] == 0 and merged["nested"] == original["nested"]
    assert merged["flag"] is False and merged["reason"] == "reprice"
    assert "clearwait_dispatch_token" not in identity_source.merge_broker_order_payload({}, {"reason": "cancel"})


def test_dispatch_token_is_carried_through_sparse_report_with_target_authoritative():
    target = controlled_metadata()
    sparse = {"resting_entry_cancel": "true", "clearwait_dispatch_token": "CONTROLLED-cancel-token"}
    merged = carry_fanout_identity(sparse, target)
    assert merged["clearwait_dispatch_token"] == target["clearwait_dispatch_token"]
    assert merged["fanout_segment_id"] == target["fanout_segment_id"]
    assert merged["resting_entry_cancel"] == "true"


class SparseCancelAdapter(FakeWorkingOrderRefreshBrokerAdapter):
    """Existing unit adapter, sparse CONTROLLED cancel reply only."""

    async def submit_order(self, request):
        reports = await super().submit_order(request)
        if request.intent_type == "cancel":
            reports = [replace(report, metadata={"resting_entry_cancel": "true", "reason": "reprice"})
                       for report in reports]
        return reports


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("lost_binding", [None] + [
    (key, state) for key in ("clearwait_dispatch_token", "fanout_segment_id")
    for state in ("missing", "null", "empty")])
async def test_real_cancel_consumer_preserves_target_token_event_and_parent(provider, lost_binding):
    sessions = build_test_session_factory()
    store = OmsStore()
    with sessions() as session:
        strategy, account, parent, order = seed_order(session, store, provider)
        if lost_binding:
            key, state = lost_binding
            changed = dict(order.payload)
            if state == "missing":
                changed.pop(key)
            else:
                changed[key] = None if state == "null" else ""
            order.payload = changed
            session.commit()
        original = deepcopy(order.payload)
        parent_id, target_id, strategy_id, account_id = parent.id, order.id, strategy.id, account.id
        account_name, client = account.name, order.client_order_id
    adapter = SparseCancelAdapter()
    service = OmsRiskService(settings=Settings(_env_file=None, oms_adapter="simulated", oms_cancel_verify_enabled=False),
        redis_client=FakeRedis(), session_factory=sessions, broker_adapter=adapter)
    event = event_for(account_name, intent_type="cancel", metadata={
        "target_client_order_id": client, "resting_entry_cancel": "true", "reason": "reprice",
        "clearwait_dispatch_token": "CONTROLLED-cancel-token", "fanout_segment_id": "999"})
    with sessions() as session:
        cancel_parent = store.create_trade_intent(session, strategy=session.get(type(strategy), strategy_id),
            broker_account=session.get(type(account), account_id), event=event)
        await service._process_cancel_intent(session=session, strategy_id=strategy_id,
            broker_account_id=account_id, intent=cancel_parent, event=event)
        session.commit()
        target = session.get(BrokerOrder, target_id)
        records = session.scalars(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == target_id)).all()
        assert len(adapter.submit_requests) == 1 and adapter.submit_requests[0].intent_type == "cancel"
        assert target.intent_id == parent_id and target.status == "cancelled"
        for key in FANOUT_IDENTITY_KEYS:
            if key in original:
                assert target.payload[key] == original[key]
                if key in ("clearwait_dispatch_token", "fanout_segment_id") and original[key] in (None, ""):
                    assert key not in records[-1].payload["metadata"]
                    assert key not in adapter.submit_requests[0].metadata
                else:
                    assert records[-1].payload["metadata"][key] == original[key]
                    assert adapter.submit_requests[0].metadata[key] == original[key]
        if lost_binding and lost_binding[1] == "missing":
            assert lost_binding[0] not in target.payload
            assert lost_binding[0] not in adapter.submit_requests[0].metadata
            assert lost_binding[0] not in records[-1].payload["metadata"]
        assert session.get(TradeIntent, parent_id).intent_type == "open"
        assert cancel_parent.status == "cancelled"


@pytest.mark.parametrize("source", ["broker", None, "unknown", "client"])
def test_existing_census_terminal_proof_survives_sparse_cancel_but_unknown_stays_held(census, source, monkeypatch):
    """Actual local_evidence chain, existing CONTROLLED native-event fixture."""
    session, accounts, strategy = census
    journal = attempt(session)
    target = census_order(session, accounts, strategy, source=source)
    before = local_evidence(session, ACCOUNTS)
    report = ExecutionReport("cancelled", target.client_order_id, broker_order_id=target.broker_order_id,
        symbol=target.symbol, side="buy", intent_type="cancel", quantity=target.quantity,
        metadata={"resting_entry_cancel": "true", "reason": "reprice"},
        reported_at=target.updated_at, origin=source or "unknown")
    OmsStore().update_order_from_report(target, report=report, metadata=report.metadata)
    session.flush()
    fresh = local_evidence(session, ACCOUNTS)
    assert "WORX" in fresh[0] and not fresh[2]  # Existing 15s quiet guard is NOT waived.
    observed = census_source.now_ms()
    monkeypatch.setattr(census_source, "now_ms", lambda: observed + 16_000)
    # Explicit CONTROLLED elapsed interval; no native truth or clock claim.
    after = local_evidence(session, ACCOUNTS)
    assert after == before
    if source == "broker":
        assert not after[0] and ("WORX", "persisted-attempt") in after[2]
    else:
        assert "WORX" in after[0] and not after[2]
    assert journal.payload["attempt_token"] == "persisted-attempt"


def test_already_missing_dispatch_token_is_not_reconstructed_or_retired(census):
    session, accounts, strategy = census
    journal = attempt(session)
    target = census_order(session, accounts, strategy)
    old = dict(target.payload)
    old.pop("clearwait_dispatch_token")
    target.payload = old  # Explicit historical loss preimage, not a production repair.
    report = ExecutionReport("cancelled", target.client_order_id, broker_order_id=target.broker_order_id,
        symbol=target.symbol, quantity=target.quantity, metadata={"reason": "reprice"}, origin="broker")
    OmsStore().update_order_from_report(target, report=report, metadata=report.metadata)
    session.flush()
    inflight, _, terminals = local_evidence(session, ACCOUNTS)
    assert "WORX" in inflight and not terminals
    assert "clearwait_dispatch_token" not in target.payload
    assert journal.payload["kind"] == "attempt"


@pytest.mark.parametrize("provider_index", [0, 1], ids=["schwab", "webull"])
@pytest.mark.parametrize("key", ["clearwait_dispatch_token", "fanout_segment_id"])
@pytest.mark.parametrize("state", ["missing", "null", "empty"])
@pytest.mark.parametrize("path", ["report", "existing_client_upsert"])
def test_existing_unknown_binding_cannot_be_repaired_by_incoming_metadata(
        census, monkeypatch, provider_index, key, state, path):
    session, accounts, strategy = census
    journal = attempt(session)
    journal.payload = {**journal.payload, "account_name": ACCOUNTS[provider_index]}
    ordered_accounts = [accounts[provider_index], accounts[1 - provider_index]]
    target = census_order(session, ordered_accounts, strategy)
    changed = dict(target.payload)
    if state == "missing":
        changed.pop(key)
    else:
        changed[key] = None if state == "null" else ""
    target.payload = changed
    session.flush()
    assert "WORX" in local_evidence(session, ACCOUNTS)[0]
    # CONTROLLED cancel-supplied values can even match the journal. Matching
    # strings are not immutable original-placement provenance.
    incoming = {"clearwait_dispatch_token": "persisted-attempt", "fanout_segment_id": "100",
                "resting_entry_cancel": "true", "reason": "reprice"}
    parent_id = target.intent_id
    store = OmsStore()
    if path == "report":
        report = ExecutionReport("cancelled", target.client_order_id, broker_order_id=target.broker_order_id,
            symbol=target.symbol, quantity=target.quantity, metadata=incoming, origin="broker")
        store.update_order_from_report(target, report=report, metadata=incoming)
    else:
        store.get_or_create_order(session, intent=session.get(TradeIntent, parent_id),
            strategy_id=strategy.id, broker_account_id=target.broker_account_id,
            client_order_id=target.client_order_id, symbol=target.symbol, side=target.side,
            quantity=target.quantity, metadata=incoming, broker_order_id=target.broker_order_id,
            status="cancelled")
    session.flush()
    observed = census_source.now_ms()
    monkeypatch.setattr(census_source, "now_ms", lambda: observed + 16_000)
    inflight, _, terminals = local_evidence(session, ACCOUNTS)
    assert "WORX" in inflight and not terminals
    assert target.intent_id == parent_id and journal.payload["kind"] == "attempt"
    if state == "missing":
        assert key not in target.payload
    else:
        assert target.payload[key] == (None if state == "null" else "")


@pytest.mark.parametrize("key,bad", [
    (key, bad) for key in ("clearwait_dispatch_token", "fanout_segment_id")
    for bad in (None, "", False, 0, 1.5, {}, [], " ")
] + [("clearwait_dispatch_token", 123), ("clearwait_dispatch_token", " padded "),
     ("clearwait_dispatch_token", "x" * 129), ("fanout_segment_id", "00100"),
     ("fanout_segment_id", -1), ("fanout_segment_id", "100.0")])
def test_carrier_does_not_stringify_bad_target_binding_or_trust_cancel_identity(key, bad):
    target = {**controlled_metadata(), key: bad}
    incoming = {"clearwait_dispatch_token": "CONTROLLED-cancel-token", "fanout_segment_id": "100"}
    carried = carry_fanout_identity(incoming, target)
    assert key not in carried
    assert target[key] == bad


@pytest.mark.parametrize("key", ["clearwait_dispatch_token", "fanout_segment_id"])
def test_carrier_drops_cancel_identity_when_original_target_key_is_absent(key):
    target = controlled_metadata()
    target.pop(key)
    carried = carry_fanout_identity({key: "CONTROLLED-cancel-value"}, target)
    assert key not in carried
