"""Recorded local-refusal tickets; later broker acknowledgements are simulated."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, SNAPSHOT_TYPE
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from tests.unit.test_rpg1_runtime import begin, feedback, runtime
from tests.unit.test_all_on_pm import ALL_ON, completed_seeded_line  # noqa: F401

RECORDED = json.loads((Path(__file__).parents[1] / "fixtures/t43_recorded_tickets_20261006.json").read_text())
REFUSED = [job for job in RECORDED["tickets"] if job["old"]["symbol"] == "AIXI"
           or job.get("replacement_reasons") == ["rpg_stale_strategy_authorization"]]
MARKET = json.loads((Path(__file__).parents[1] / "fixtures/t43_named_market_context_20261006.json").read_text())


async def recovered_pair(monkeypatch, recorded, *, all_on=False):
    h = await runtime(monkeypatch, "schwab", notional=600,
                      strategy_overrides=ALL_ON if all_on else None)
    h.strategy.drain_pending_intents()
    h.strategy.drain_webull_direct_intents()
    h.clock[0] = datetime.fromtimestamp(recorded["authorization"]["at"], UTC) + timedelta(seconds=60)
    state = h.strategy.watchlist_state(recorded["old"]["symbol"])
    h.state = state
    state.atr_state, state.atr_state_age = "short", 45
    state.atr_trail = float(recorded["authorization"]["event"]["payload"]["metadata"]["cw_flip_level"])
    state.fanout_segment_id = recorded["segment_id"]
    state.atr_short_flip_bar_ts = int(recorded["old"]["metadata"]["rpg_short_segment"])
    state.bars.append(OHLCVBar(h.strategy._now_ms() - 60000, 1.37, 1.39, 1.35, 1.37, 127705))
    state.last_quote = Quote(state.symbol, 1.36, 1.37, 1.37, h.strategy._now_ms())
    # Tape beyond the saved probe is controlled; sibling remains an accepted
    # working rest, not its later final state in the durable read.
    failed_webull = recorded["old"]["broker_account_name"] == "live:orb"
    failed_leg = "webull" if failed_webull else "schwab"
    surviving_leg = "schwab" if failed_webull else "webull"
    setattr(state, f"resting_{failed_leg}_generation", recorded["old"]["metadata"]["rpg_resting_generation"])
    setattr(state, f"resting_{failed_leg}_quantity", int(Decimal(recorded["old"]["quantity"])))
    setattr(state, f"resting_{surviving_leg}_generation", "CONTROLLED-surviving-generation")
    setattr(state, f"resting_{surviving_leg}_quantity", 204)
    setattr(state, f"resting_{surviving_leg}_wire_stop", 1.4639)
    setattr(state, f"resting_{surviving_leg}_wire_limit", 1.4712)
    state.webull_resting_active = not failed_webull
    state.resting_active = state.resting_is_broker_order = True
    state.resting_level, state.resting_trigger = state.atr_trail, 1.4639
    token = recorded["replacement"]["metadata"]["rpg_handoff_token"]
    h.strategy.rpg_handoff_authorization(token, deepcopy(recorded))
    return h, surviving_leg


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", REFUSED, ids=lambda j: j["old"]["symbol"])
async def test_t43_recorded_refusal_next_bar_replaces_only_absent_leg(monkeypatch, recorded):
    h, surviving = await recovered_pair(monkeypatch, recorded)
    before = tuple(getattr(h.state, f"resting_{surviving}_{field}")
                   for field in ("generation", "quantity", "wire_stop", "wire_limit"))
    h.strategy._cw_v2_resting_track(h.state, None)
    primary = h.strategy.drain_pending_intents()
    mirrors = h.strategy.drain_webull_direct_intents()
    failed_webull = recorded["old"]["broker_account_name"] == "live:orb"
    assert len(primary) == (0 if failed_webull else 1)
    assert len(mirrors) == (1 if failed_webull else 0)
    assert tuple(getattr(h.state, f"resting_{surviving}_{field}")
                 for field in ("generation", "quantity", "wire_stop", "wire_limit")) == before
    assert (primary or mirrors)[0].intent_type == "open"
    h.strategy._cw_v2_resting_track(h.state, None)
    assert not h.strategy.drain_pending_intents()
    assert not h.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", REFUSED, ids=lambda j: j["old"]["symbol"])
async def test_t43_absent_leg_recovery_composes_all_on_and_proven_flip_owner(monkeypatch, recorded):
    h, surviving = await recovered_pair(monkeypatch, recorded, all_on=True)
    assert all(getattr(h.strategy.settings, flag) is True for flag in ALL_ON)
    # Controlled restored owner/position evidence, not a live DB attribution.
    h.strategy._flip_owned_first_entry_enabled = True
    h.strategy._flip_owner_restore_readable = True
    persisted = []
    h.strategy._flip_owner_persist = lambda *args: persisted.append(args)
    h.state.flip_owner_phase = "resting"
    h.state.flip_owner_opportunity_id = h.state.fanout_segment_id
    h.state.flip_owner_evidence_readable = h.state.retry_one_budget_readable = True
    h.state.flip_owner_evidence_at_ms = h.strategy._now_ms()
    h.state.retry_one_segment_id = h.state.atr_short_flip_bar_ts
    before = tuple(getattr(h.state, f"resting_{surviving}_{field}")
                   for field in ("generation", "quantity", "wire_stop", "wire_limit"))
    h.strategy._cw_v2_resting_track(h.state, None)
    primary, mirror = h.strategy.drain_pending_intents(), h.strategy.drain_webull_direct_intents()
    failed_webull = recorded["old"]["broker_account_name"] == "live:orb"
    assert len(primary) == (0 if failed_webull else 1)
    assert len(mirror) == (1 if failed_webull else 0)
    assert persisted and h.state.flip_owner_phase == "resting"
    assert tuple(getattr(h.state, f"resting_{surviving}_{field}")
                 for field in ("generation", "quantity", "wire_stop", "wire_limit")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["old_unknown", "replacement_unknown", "filled", "new_generation", "new_segment", "same_bar"])
async def test_t43_uncertain_or_filled_leg_is_never_recovered(monkeypatch, blocked):
    recorded = deepcopy(REFUSED[0])
    h, _ = await recovered_pair(monkeypatch, recorded)
    job = next(iter(h.strategy._rpg_handoffs.values()))
    if blocked == "old_unknown":
        job.pop("cleared_at", None)
        job.pop("local_no_wire", None)
    elif blocked == "replacement_unknown":
        job["phase"] = "submit_unknown"
    elif blocked == "filled":
        job["replacement_filled"] = True
    elif blocked == "new_segment":
        job["segment_id"] += 1
    elif blocked == "same_bar":
        h.state.bars[-1] = OHLCVBar(int(job["completed_at"] // 60 * 60000) - 60000,
                                  1.37, 1.39, 1.35, 1.37, 127705)
    else:
        leg = "webull" if job["old"]["broker_account_name"] == "live:orb" else "schwab"
        setattr(h.state, f"resting_{leg}_generation", "CONTROLLED-newer-live-generation")
    h.strategy._cw_v2_resting_track(h.state, None)
    assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_t43_stale_pre_wire_gets_new_v2_authorization_not_an_age_waiver(monkeypatch, broker):
    h = await runtime(monkeypatch, broker)
    token, _ = await begin(h, broker)
    original = h.service._finalize_v2_entry_quantity
    delayed_once = False

    def delay(event, intent):
        nonlocal delayed_once
        result = original(event, intent)
        if not delayed_once:
            delayed_once = True
            h.clock[0] += timedelta(seconds=1.09)  # Recorded OLOX completion-age boundary.
        return result

    monkeypatch.setattr(h.service, "_finalize_v2_entry_quantity", delay)
    async def v2_callback():
        while not HandoffJournal(h.factory).read(token).get("pre_wire_authorization_nonce"):
            await asyncio.sleep(0.01)
        await h.bot._rpg_handoff_pass()
    task = asyncio.create_task(v2_callback())
    try:
        await feedback(h)
        assert HandoffJournal(h.factory).read(token).get("pre_wire_authorization_nonce")
        await asyncio.wait_for(task, 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "placed" and len(h.adapter.opens) == 1
    assert job["authorization"]["pre_wire_authorization_nonce"] == job["pre_wire_authorization_nonce"]
    assert h.clock[0].timestamp() - job["authorization"]["at"] <= 1
    assert h.adapter.opens[0].metadata["stop_price"] != h.old.metadata["stop_price"]


@pytest.mark.asyncio
async def test_t43_unreadable_reauthorization_is_audited_client_abort_never_venue_reject(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    token, _ = await begin(h, "schwab")
    original = h.service._finalize_v2_entry_quantity
    def delay(event, intent):
        result = original(event, intent)
        h.clock[0] += timedelta(seconds=1.09)
        return result
    monkeypatch.setattr(h.service, "_finalize_v2_entry_quantity", delay)
    await feedback(h)
    assert not h.adapter.opens
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "refused"
    assert job["replacement_reasons"] == ["rpg_strategy_reauthorization_unreadable"]
    # The old cancelled intent is terminal too in a live cancel/report sequence;
    # the isolated readback helper intentionally leaves that ledger untouched.
    with h.factory() as session:
        old_order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == h.old.client_order_id))
        session.get(TradeIntent, old_order.intent_id).status = "cancelled"
        session.commit()
    assert h.bot._fetch_position_maps() == ({}, {})
    with h.factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.status == "aborted"))
        assert order is not None and order.broker_order_id is None
        assert order.payload["reject_reason"] == "rpg_strategy_reauthorization_unreadable"
        intent = session.get(TradeIntent, order.intent_id)
        assert intent.status == "aborted" and intent.payload["refusal_origin"] == "client_abort"
        audit = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id))
        assert audit.event_type == "aborted" and audit.event_source == "client"
        assert audit.payload["reason"] == order.payload["reject_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [True, False, "no_event_id"])
async def test_t43_ordinary_open_local_abort_is_terminal_only_for_its_new_audited_intent(monkeypatch, marker):
    h = await runtime(monkeypatch, "schwab")
    token, _ = await begin(h, "schwab")
    event = TradeIntentEvent(source_service="schwab-1m-v2", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=h.old.broker_account_name,
        symbol=h.old.symbol, side="buy", intent_type="open", quantity=Decimal(2),
        reason="CONTROLLED ordinary placement while RPG owns old buy"))
    result = await h.service.process_trade_intent(event)
    assert result[0].payload.status == "aborted"
    assert result[0].payload.reason == "rpg_old_buy_still_owned"
    assert not h.adapter.opens
    with h.factory() as session:
        order = session.get(BrokerOrder, result[0].payload.order_db_id)
        assert not order.broker_order_id and not order.payload.get("rpg_handoff_token")
        if marker is False:
            order.payload = {**order.payload, "rpg_local_abort_no_wire": "false"}
            audit = session.scalar(select(BrokerOrderEvent).where(
                BrokerOrderEvent.order_id == order.id, BrokerOrderEvent.event_type == "aborted"))
            audit.payload = {**audit.payload, "metadata": {
                **audit.payload["metadata"], "rpg_local_abort_no_wire": "false"}}
        elif marker == "no_event_id":
            order.payload = {key: value for key, value in order.payload.items() if key != "rpg_abort_event_id"}
            intent = session.get(TradeIntent, order.intent_id)
            intent.payload = {key: value for key, value in intent.payload.items() if key != "event_id"}
            audit = session.scalar(select(BrokerOrderEvent).where(
                BrokerOrderEvent.order_id == order.id, BrokerOrderEvent.event_type == "aborted"))
            audit.payload = {**audit.payload, "metadata": {
                key: value for key, value in audit.payload["metadata"].items() if key != "rpg_abort_event_id"}}
        old_order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == h.old.client_order_id))
        session.get(TradeIntent, old_order.intent_id).status = "cancelled"
        session.commit()
    maps = h.bot._fetch_position_maps()
    assert maps == (({}, {}) if marker is True else ({h.old.symbol: 2}, {}))
    # Terminal truth for the NEW never-sent intent cannot clear the OLD ticket.
    assert HandoffJournal(h.factory).read(token)["phase"] == "clear"
    assert h.strategy._rpg_entry_owned(h.state, account=h.old.broker_account_name)


@pytest.mark.asyncio
async def test_t43_fresh_reauthorization_still_refuses_a_changed_canonical_price(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    token, _ = await begin(h, "schwab")
    original = h.service._finalize_v2_entry_quantity
    def delay(event, intent):
        result = original(event, intent)
        h.clock[0] += timedelta(seconds=1.09)
        return result
    monkeypatch.setattr(h.service, "_finalize_v2_entry_quantity", delay)
    async def v2_callback():
        while not HandoffJournal(h.factory).read(token).get("pre_wire_authorization_nonce"):
            await asyncio.sleep(0.01)
        h.state.atr_trail += 0.10  # Explicit controlled price change while OMS is waiting.
        await h.bot._rpg_handoff_pass()
    task = asyncio.create_task(v2_callback())
    try:
        await feedback(h)
        await asyncio.wait_for(task, 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "refused" and not h.adapter.opens
    assert job["replacement_reasons"] == ["rpg_current_price_size_or_identity_changed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_t43_real_broker_refusal_is_not_relabelled_as_a_local_abort(monkeypatch, broker):
    h = await runtime(monkeypatch, broker)
    token, _ = await begin(h, broker)
    h.adapter.refusal = "CONTROLLED venue refusal"
    await feedback(h)
    assert len(h.adapter.opens) == 1 and HandoffJournal(h.factory).read(token)["phase"] == "refused"
    with h.factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.status == "rejected"))
        assert order is not None and order.payload["reject_reason"] == "SIMULATOR CONTROLLED venue refusal"
        intent = session.get(TradeIntent, order.intent_id)
        assert intent.status == "rejected" and intent.payload["refusal_origin"] == "broker_reject"


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("proof", ["complete", "no_audit", "audit_source", "foreign_generation", "filled", "cancelled", "expired"])
async def test_t43_committed_broker_rejection_repairs_only_absent_leg_after_crash(monkeypatch, broker, proof):
    h = await runtime(monkeypatch, broker)
    token, _ = await begin(h, broker)
    h.adapter.refusal = "CONTROLLED venue refusal"
    submit = h.adapter.submit_order

    async def at_test_clock(request):
        return [replace(report, reported_at=h.clock[0]) for report in await submit(request)]

    monkeypatch.setattr(h.adapter, "submit_order", at_test_clock)
    await feedback(h)
    journal = HandoffJournal(h.factory)
    job = journal.read(token)
    assert job["phase"] == "refused" and len(h.adapter.opens) == 1
    job = journal.change(token, job["revision"], phase="submit_unknown", reason="CONTROLLED interrupted completion",
                         completed_at=None, dispatch_reads=30)
    with h.factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == job["replacement"]["client_order_id"]))
        audit = session.scalar(select(BrokerOrderEvent).where(
            BrokerOrderEvent.order_id == order.id, BrokerOrderEvent.event_type == "rejected"))
        if proof == "no_audit":
            session.delete(audit)
        elif proof == "audit_source":
            audit.event_source = "client"
        elif proof == "foreign_generation":
            order.payload = {**order.payload, "rpg_resting_generation": "CONTROLLED foreign generation"}
        elif proof == "filled":
            session.add(Fill(order_id=order.id, strategy_id=order.strategy_id, broker_account_id=order.broker_account_id,
                             symbol=order.symbol, side="buy", quantity=Decimal(1), price=Decimal("3.10"), payload={}))
        elif proof in {"cancelled", "expired"}:
            order.status = proof
        session.commit()
    reads = len(h.adapter.reads)
    recovered = await h.service._rpg_reconcile_dispatch(token, job)
    assert len(h.adapter.reads) == reads and len(h.adapter.opens) == 1
    if proof == "complete":
        assert recovered["reason"] == "replacement_refused"
        assert recovered["completed_at"] == h.clock[0].timestamp()
    else:
        assert recovered.get("reason") != "replacement_refused"
    h.strategy.rpg_handoff_authorization(str(token), recovered)
    failed = "schwab" if broker == "schwab" else "webull"
    surviving = "webull" if broker == "schwab" else "schwab"
    # Controlled next bar/sibling state; only the refused broker leg is absent.
    h.state.resting_active = h.state.resting_is_broker_order = True
    h.state.resting_level = h.state.atr_trail
    h.state.resting_trigger = h.state.atr_trail * 1.005
    setattr(h.state, f"resting_{failed}_quantity", 0)
    setattr(h.state, f"resting_{failed}_generation", job["replacement"]["metadata"]["rpg_resting_generation"])
    setattr(h.state, f"resting_{surviving}_quantity", 1)
    setattr(h.state, f"resting_{surviving}_generation", "CONTROLLED working sibling")
    setattr(h.state, f"resting_{surviving}_wire_stop", 3.07)
    setattr(h.state, f"resting_{surviving}_wire_limit", 3.09)
    h.state.webull_resting_active = surviving == "webull"
    before = tuple(getattr(h.state, f"resting_{surviving}_{field}") for field in
                   ("generation", "quantity", "wire_stop", "wire_limit"))
    h.clock[0] += timedelta(seconds=60)
    h.state.bars.append(OHLCVBar(h.strategy._now_ms() - 60000, 2.9, 3.0, 2.9, 2.95, 40151))
    h.state.last_quote = replace(h.state.last_quote, quote_time_ms=h.strategy._now_ms())
    h.strategy._cw_v2_resting_track(h.state, None)
    primary, mirror = h.strategy.drain_pending_intents(), h.strategy.drain_webull_direct_intents()
    assert len(primary) == (1 if proof == "complete" and broker == "schwab" else 0)
    assert len(mirror) == (1 if proof == "complete" and broker == "webull" else 0)
    assert tuple(getattr(h.state, f"resting_{surviving}_{field}") for field in
                 ("generation", "quantity", "wire_stop", "wire_limit")) == before
    h.strategy._cw_v2_resting_track(h.state, None)
    assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
async def test_t43_reauthorization_rechecks_orb_ownership_after_releasing_shared_lock(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    h.service.settings = h.service.settings.model_copy(update={"orb_live_schwab_orders_enabled": True})
    token, _ = await begin(h, "schwab")
    original = h.service._finalize_v2_entry_quantity

    def delay(event, intent):
        result = original(event, intent)
        h.clock[0] += timedelta(seconds=1.09)
        return result

    monkeypatch.setattr(h.service, "_finalize_v2_entry_quantity", delay)

    async def concurrent_orb_and_v2():
        while not HandoffJournal(h.factory).read(token).get("pre_wire_authorization_nonce"):
            await asyncio.sleep(0.01)
        # Explicit controlled competitor committed during the unlocked wait.
        with h.factory() as session:
            strategy = h.service.store.ensure_strategy(session, "orb_schwab", name="ORB")
            account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == h.old.broker_account_name))
            event = TradeIntentEvent(source_service="orb-schwab", payload=TradeIntentPayload(
                strategy_code="orb_schwab", broker_account_name=account.name,
                symbol=h.old.symbol, side="buy", intent_type="open", quantity=Decimal(2),
                reason="CONTROLLED concurrent ORB admission"))
            intent = h.service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
            h.service.store.get_or_create_order(session, intent=intent, strategy_id=strategy.id,
                broker_account_id=account.id, client_order_id="CONTROLLED-orb-during-nonce",
                broker_order_id="CONTROLLED-orb-wire", symbol=h.old.symbol, side="buy",
                quantity=Decimal(2), metadata={}, status="accepted")
            session.commit()
        await h.bot._rpg_handoff_pass()

    task = asyncio.create_task(concurrent_orb_and_v2())
    try:
        await feedback(h)
        await asyncio.wait_for(task, 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "refused" and not h.adapter.opens
    assert job["replacement_reasons"] == ["v2_orb_schwab_buy_order_open"]
    with h.factory() as session:
        orb = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == "CONTROLLED-orb-during-nonce"))
        assert orb.status == "accepted" and orb.quantity == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["complete", "no_audit", "audit_source", "foreign_token", "foreign_generation", "broker_id", "unknown_origin"])
async def test_t43_committed_abort_recovers_interrupted_dispatch_only_with_exact_no_wire_proof(monkeypatch, proof):
    h = await runtime(monkeypatch, "schwab")
    token, _ = await begin(h, "schwab")
    original = h.service._finalize_v2_entry_quantity

    def delay(event, intent):
        result = original(event, intent)
        h.clock[0] += timedelta(seconds=1.09)
        return result

    monkeypatch.setattr(h.service, "_finalize_v2_entry_quantity", delay)
    await feedback(h)
    journal = HandoffJournal(h.factory)
    job = journal.read(token)
    assert job["phase"] == "refused" and not h.adapter.opens
    # Simulate the crash after the committed audit, before coordinator completion.
    job = journal.change(token, job["revision"], phase="submit_unknown", reason="submit_interrupted",
                         completed_at=None, dispatch_reads=30)
    with h.factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.status == "aborted"))
        audit = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id))
        if proof == "no_audit":
            session.delete(audit)
        elif proof == "audit_source":
            audit.event_source = "broker"
        elif proof == "foreign_token":
            order.payload = {**order.payload, "rpg_handoff_token": "foreign-token"}
        elif proof == "foreign_generation":
            order.payload = {**order.payload, "rpg_resting_generation": "foreign-generation"}
        elif proof == "broker_id":
            order.broker_order_id = "CONTROLLED-possibly-wired"
        elif proof == "unknown_origin":
            order.payload = {**order.payload, "refusal_origin": "unknown"}
        session.commit()
    reads_before = len(h.adapter.reads)
    recovered = await h.service._rpg_reconcile_dispatch(token, job)
    assert len(h.adapter.reads) == reads_before and not h.adapter.opens
    with h.factory() as session:
        old_order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == h.old.client_order_id))
        session.get(TradeIntent, old_order.intent_id).status = "cancelled"
        session.commit()
    maps = h.bot._fetch_position_maps()
    if proof == "complete":
        assert maps == ({}, {})
        assert recovered["phase"] == "refused" and recovered["reason"] == "replacement_refused"
        assert recovered["replacement_reasons"] == ["rpg_strategy_reauthorization_unreadable"]
        assert recovered["completed_at"] == h.clock[0].timestamp()
        h.strategy.rpg_handoff_authorization(str(token), recovered)
        assert not h.strategy._rpg_entry_owned(h.state, account=h.old.broker_account_name)
    else:
        assert recovered["phase"] == "submit_unknown"
        assert maps[0].get(h.old.symbol, 0) > 0 and not maps[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", REFUSED, ids=lambda j: j["old"]["symbol"])
async def test_t43_named_authorization_submit_check_race_uses_current_v2_decision(monkeypatch, recorded):
    h = await runtime(monkeypatch, "schwab")
    h.service.settings = h.service.settings.model_copy(update={
        "strategy_schwab_1m_v2_entry_notional_usd": 600,
        "strategy_schwab_1m_v2_webull_entry_notional_usd": 300,
    })
    h.strategy.settings = h.bot.settings = h.service.settings
    h.strategy._entry_notional_schwab = Decimal(600)
    h.strategy._entry_notional_webull = Decimal(300)
    h.strategy.drain_pending_intents()
    h.strategy.drain_webull_direct_intents()
    job = deepcopy(recorded)
    token = UUID(job["replacement"]["metadata"]["rpg_handoff_token"])
    job.update(phase="submitting", reason="replacement_claimed", completed_at=None)
    context = MARKET[job["old"]["symbol"]]
    state = h.strategy.watchlist_state(job["old"]["symbol"])
    state.atr_state, state.atr_state_age = "short", 45
    state.atr_trail = context["trail"]  # Retained six-decimal ATR probe, not the rounded metadata line.
    state.fanout_segment_id = job["segment_id"]
    state.atr_short_flip_bar_ts = int(job["old"]["metadata"]["rpg_short_segment"])
    bar_ms = int(datetime.fromisoformat(context["bar_time"]).timestamp() * 1000)
    state.bars.append(OHLCVBar(bar_ms, *(context[key] for key in ("open", "high", "low", "close", "volume"))))
    state.last_quote = Quote(state.symbol, context["bid"], context["ask"], context["ask"],
                            int(datetime.fromisoformat(context["quote_time"]).timestamp() * 1000))
    h.clock[0] = datetime.fromtimestamp(job["submit_started_at"], UTC)
    with h.factory() as session:
        session.add(DashboardSnapshot(id=token, snapshot_type=SNAPSHOT_TYPE, payload=job))
        session.commit()
    # Reuse the recorded dispatched metadata/event id, not a regenerated order.
    event = TradeIntentEvent(event_id=UUID(job["replacement"]["metadata"]["rpg_event_id"]),
        source_service="schwab-1m-v2", payload=TradeIntentPayload(
            **{key: job["replacement"][key] for key in
               ("strategy_code", "broker_account_name", "symbol", "side", "intent_type", "quantity", "reason", "metadata")}))
    h.service._rpg_dispatch_token = str(token)
    with h.factory() as session:
        assert h.service._rpg_open_refusal(event, session=session) is None
        h.clock[0] = datetime.fromtimestamp(recorded["completed_at"], UTC)
        assert h.service._rpg_open_refusal(event, session=session) == "rpg_stale_strategy_authorization"

        async def current_v2():
            while not HandoffJournal(h.factory).read(token).get("pre_wire_authorization_nonce"):
                await asyncio.sleep(0.01)
            await h.bot._rpg_handoff_pass()

        task = asyncio.create_task(current_v2())
        try:
            assert await h.service._rpg_fresh_open_refusal(event, session=session) is None
            await asyncio.wait_for(task, 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    refreshed = HandoffJournal(h.factory).read(token)["authorization"]
    assert refreshed["pre_wire_authorization_nonce"]
    assert refreshed["at"] == h.strategy._now_ms() / 1000  # v2 decision-clock precision is milliseconds.
    assert refreshed["event"]["payload"]["quantity"] == event.payload.model_dump(mode="json")["quantity"]
    # This pins authorization only. AIXI's capture ask is below today's distance
    # limit; it is not evidence that the unchanged Webull precheck would wire it.
    assert not h.adapter.opens
