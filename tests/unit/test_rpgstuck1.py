"""October 5 exact durable tickets/E3; future bars and broker answers are simulated."""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text

from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback
from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, Fill, OmsManagedPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, SNAPSHOT_TYPE
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from project_mai_tai.strategy_core.schwab_1m_v2 import logger as v2_logger
from tests.unit.test_rpg1_runtime import runtime, begin, feedback, tick_clock, _session_factory
from tests.unit.t43_recorded_audit_support import seed_recorded_abort, seed_recorded_intent

RECORDED = json.loads((Path(__file__).parents[1] / "fixtures/rpgstuck1_recorded.json").read_text())
PRIMARY = [row for row in RECORDED["tickets"] if row["payload"]["old"]["broker_account_name"] == "live:schwab_1m_v2"]


async def recorded_state(monkeypatch, symbol):
    h = await runtime(monkeypatch, "schwab", notional=600)
    jobs = [row for row in deepcopy(RECORDED["tickets"]) if row["payload"]["old"]["symbol"] == symbol]
    primary = next(row["payload"] for row in jobs if row["payload"]["old"]["broker_account_name"] == "live:schwab_1m_v2")
    auth = primary["authorization"]
    h.clock[0] = datetime.fromtimestamp(auth["at"], UTC)
    del h.strategy._symbol_states[h.state.symbol]
    h.state = h.strategy.watchlist_state(symbol)
    h.state.atr_state, h.state.atr_state_age = "short", 31
    h.state.fanout_segment_id = primary["segment_id"]
    h.state.atr_short_flip_bar_ts = int(primary["old"]["metadata"]["rpg_short_segment"])
    h.state.atr_trail = float(auth["event"]["payload"]["metadata"]["cw_flip_level"])
    # Controlled next-bar tape, volume and quote; these were not recorded market evidence.
    h.clock[0] += timedelta(seconds=60)
    h.state.bars.append(OHLCVBar(h.strategy._now_ms() - 60000, 5, 5.1, 4.9, 5, 50000))
    h.state.last_quote = Quote(symbol, 5.0, 5.1, 5.05, h.strategy._now_ms())
    h.strategy._rpg_handoffs = {row["id"]: row["payload"] for row in jobs}
    with h.factory() as session:
        for row in jobs:
            session.add(DashboardSnapshot(id=UUID(row["id"]), snapshot_type=SNAPSHOT_TYPE, payload=row["payload"]))
        session.commit()
    for row in jobs:
        job = row["payload"]
        if not job.get("replacement"):
            continue
        if job["replacement"]["broker_account_name"] == "live:schwab_1m_v2":
            seed_recorded_abort(h, job)
        else:
            audit = next(item for item in RECORDED["intents"]
                         if item["payload"].get("event_id") == job["replacement"]["metadata"]["rpg_event_id"])
            seed_recorded_intent(h, {**audit, "strategy": "schwab_1m_v2", "symbol": symbol,
                                    "side": "buy", "intent_type": "open"})
    await h.bot._rpg_handoff_pass()
    return h


def recorded_dispatch(h, row):
    job = deepcopy(row["payload"])
    token = row["id"]
    intent = next(item for item in RECORDED["intents"] if item["payload"].get("metadata", {}).get("rpg_handoff_token") == token)
    event = TradeIntentEvent.model_validate(job["authorization"]["event"])
    event.event_id = UUID(intent["payload"]["event_id"])
    event.payload.metadata = deepcopy(intent["payload"]["metadata"])
    job["phase"] = "submitting"
    h.clock[0] = datetime.fromtimestamp(job["authorization"]["at"], UTC)
    h.service.settings.oms_v2_emit_native_oco_bracket_enabled = True
    h.service._rpg_dispatch_token = token
    monkeyjournal = SimpleNamespace(read=lambda *a, **kw: job)
    h.service._rpg_journal = lambda: monkeyjournal
    return event, job


@pytest.mark.asyncio
@pytest.mark.parametrize("row", PRIMARY, ids=lambda row: row["payload"]["old"]["symbol"])
async def test_r_t8_recorded_e3_authorization_matches_actual_cent_wire(monkeypatch, row):
    h = await runtime(monkeypatch, "schwab")
    event, job = recorded_dispatch(h, row)
    expected = ("5.27", "5.30") if event.payload.symbol == "APUS" else ("5.51", "5.54")
    assert tuple(event.payload.metadata[key] for key in ("stop_price", "limit_price")) == expected
    assert h.service._rpg_open_refusal(event) is None
    assert event.payload.quantity == Decimal(job["authorization"]["event"]["payload"]["quantity"])


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["stop_price", "limit_price", "quantity", "cw_flip_level", "cw_entry_slot", "fanout_segment_id", "fanout_slot_id", "account"])
@pytest.mark.parametrize("row", PRIMARY, ids=lambda row: row["payload"]["old"]["symbol"])
async def test_r_t8_canonical_real_difference_is_still_refused(monkeypatch, row, field):
    h = await runtime(monkeypatch, "schwab")
    event, _ = recorded_dispatch(h, row)
    if field == "quantity":
        event.payload.quantity += 1
    elif field == "account":
        event.payload.broker_account_name = "live:orb"
    elif field in {"stop_price", "limit_price"}:
        event.payload.metadata[field] = str(Decimal(event.payload.metadata[field]) + Decimal("0.01"))
    else:
        event.payload.metadata[field] += "-changed"
    assert h.service._rpg_open_refusal(event) in {
        "rpg_current_price_size_or_identity_changed", "rpg_replacement_identity_changed"}


@pytest.mark.asyncio
async def test_r_t1_actual_apus_refused_tickets_release_next_bar_both_accounts(monkeypatch):
    h = await recorded_state(monkeypatch, "APUS")
    submit = h.adapter.submit_order

    async def per_account_acceptance(request):
        return [replace(report, broker_order_id="simulated-" + request.broker_account_name)
                for report in await submit(request)]

    monkeypatch.setattr(h.adapter, "submit_order", per_account_acceptance)
    assert not h.strategy._rpg_entry_owned(h.state)
    h.strategy._cw_v2_resting_track(h.state, None)
    primary, = h.strategy.drain_pending_intents()
    mirror, = h.strategy.drain_webull_direct_intents()
    assert primary.quantity > 0 and mirror.quantity > 0
    for account, draft in [("live:schwab_1m_v2", primary), ("live:orb", mirror)]:
        event = TradeIntentEvent(source_service="schwab-1m-v2", produced_at=h.clock[0], payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2", broker_account_name=account, symbol=draft.symbol,
            side=draft.side, intent_type=draft.intent_type, quantity=draft.quantity,
            reason=draft.reason, metadata=draft.metadata))
        await h.service.process_trade_intent(event)
    assert {request.broker_account_name for request in h.adapter.opens} == {"live:schwab_1m_v2", "live:orb"}
    h.strategy._cw_v2_resting_track(h.state, None)
    assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()
    for _ in range(20):
        tick_clock(h, 60)
        h.state.bars.append(replace(h.state.bars[-1], timestamp_ms=h.strategy._now_ms() - 60000))
        h.strategy._cw_v2_resting_track(h.state, None)
        assert h.state.resting_active
        assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()
    assert len(h.adapter.opens) == 2


@pytest.mark.asyncio
async def test_r_t2_actual_veea_unknown_leg_does_not_block_cleared_primary_next_bar(monkeypatch):
    h = await recorded_state(monkeypatch, "VEEA")
    assert h.strategy._rpg_entry_owned(h.state)
    h.strategy._cw_v2_resting_track(h.state, None)
    primary, = h.strategy.drain_pending_intents()
    assert primary.symbol == "VEEA"
    assert not h.strategy.drain_webull_direct_intents()
    unknown = next(job for job in h.strategy._rpg_handoffs.values() if job["phase"] == "held_unknown")
    assert not unknown["local_no_wire"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["refused", "expired"])
@pytest.mark.parametrize("proof", ["cleared_at", "local_no_wire", "none", "fill"])
async def test_terminal_release_requires_old_order_clear_proof(monkeypatch, phase, proof):
    h = await recorded_state(monkeypatch, "APUS")
    for job in h.strategy._rpg_handoffs.values():
        job["phase"] = phase
        job.pop("cleared_at", None)
        job.pop("local_no_wire", None)
        if proof == "cleared_at":
            job[proof] = h.clock[0].timestamp()
        elif proof == "local_no_wire":
            job[proof] = True
        elif proof == "fill":
            job["cleared_at"], job["no_rebuy"] = h.clock[0].timestamp(), True
    assert h.strategy._rpg_entry_owned(h.state) == (proof in {"none", "fill"})
    h.service._rpg_journal = lambda: SimpleNamespace(jobs=lambda **kwargs: list(h.strategy._rpg_handoffs.items()))
    opening = TradeIntentEvent(source_service="schwab-1m-v2", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name="live:schwab_1m_v2", symbol="APUS",
        side="buy", intent_type="open", quantity=Decimal(1), reason="CONTROLLED next bar", metadata={}))
    assert (h.service._rpg_open_refusal(opening) is not None) == (proof in {"none", "fill"})


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["buy_flip", "slot", "gap_hold", "boot_hold"])
async def test_r_t8_b6_b8_cleared_ticket_ends_at_flip_slot_or_hold(monkeypatch, gate):
    h = await runtime(monkeypatch, "schwab")
    token, _ = await begin(h, "schwab")
    if gate == "buy_flip":
        h.state.atr_state = "long"
    elif gate == "slot":
        h.state.cw_resting_taken = True
    elif gate == "gap_hold":
        h.strategy._gap_hold_enabled = h.state.gap_hold_active = True
    else:
        h.strategy._entries_held = True
    await feedback(h)
    await feedback(h)
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "expired" and job["cleared_at"]
    assert not h.strategy._rpg_entry_owned(h.state, account=h.old.broker_account_name)
    assert not h.adapter.opens


@pytest.mark.asyncio
async def test_r_t3_twenty_bars_admission_logs_once_but_gates_are_fresh_each_call(monkeypatch, caplog):
    h = await recorded_state(monkeypatch, "APUS")
    h.strategy._flip_owned_first_entry_enabled = True
    h.strategy._flip_owner_restore_readable = True
    h.strategy._retry_one_enabled = False
    h.state.flip_owner_phase = "idle"
    monkeypatch.setattr(h.strategy, "_flip_owner_evidence_fresh", lambda state: True)
    caplog.set_level("INFO", logger="project_mai_tai.strategy_core.schwab_1m_v2")
    monkeypatch.setattr(v2_logger, "handlers", [caplog.handler])
    caplog.clear()
    for _ in range(20):
        h.state.flip_owner_open_positions = {}
        assert h.strategy._strict_first_rest_admitted(h.state, slot="first")
        h.state.flip_owner_open_positions = {"controlled-position": object()}
        for _ in range(10):
            assert not h.strategy._strict_first_rest_admitted(h.state, slot="first")
        tick_clock(h, 60)
    assert sum("[V2-FLIP-OWNER-ADMISSION]" in record.message for record in caplog.records) == 20
    assert h.strategy._flip_owner_counts["admission_evaluated"] >= 220


@pytest.mark.asyncio
async def test_r_t2_distance_hold_is_local_only_before_wire_and_old_queue_retires(monkeypatch):
    h = await runtime(monkeypatch, "webull")
    with h.factory() as session:
        session.execute(delete(BrokerOrder))
        session.commit()
    md = h.opening.payload.metadata
    md.update(webull_shape_market_price="2.50", webull_shape_market_at_utc=h.clock[0].isoformat())
    assert h.service._defer_webull_resting_mirror_before_submit(h.opening)
    slot = md["fanout_slot_id"]
    assert h.service._webull_mirror_deferred_by_slot[slot].local_no_wire
    submit = h.adapter.submit_order

    async def per_account_acceptance(request):
        return [replace(report, broker_order_id="simulated-" + request.broker_account_name)
                for report in await submit(request)]

    monkeypatch.setattr(h.adapter, "submit_order", per_account_acceptance)
    primary = h.opening.model_copy(deep=True)
    primary.event_id = uuid4()
    primary.payload.broker_account_name = "live:schwab_1m_v2"
    primary.payload.metadata.pop("fanout_leg", None)
    await h.service.process_trade_intent(primary)
    assert h.service._webull_mirror_deferred_by_slot[slot].local_no_wire
    queued = h.opening.model_copy(deep=True)
    queued.payload.metadata.update(webull_deferred_resubmit="true", webull_deferred_resubmit_attempt="1")
    token, _ = await begin(h, "webull")
    job = HandoffJournal(h.factory).read(token)
    assert job["local_no_wire"] and job["reason"] == "distance_proven_no_wire"
    assert not h.adapter.cancels and not h.adapter.reads
    await h.service._handle_stream_message({"data": queued.model_dump_json()})
    assert len(h.adapter.opens) == 1
    await feedback(h)
    assert len(h.adapter.opens) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["wire", "generation", "account", "slot"])
async def test_distance_hold_cannot_be_assumed_local_after_dispatch_or_identity_change(monkeypatch, mismatch):
    h = await runtime(monkeypatch, "webull")
    with h.factory() as session:
        session.execute(delete(BrokerOrder))
        session.commit()
    h.opening.payload.metadata.update(webull_shape_market_price="2.50", webull_shape_market_at_utc=h.clock[0].isoformat())
    assert h.service._defer_webull_resting_mirror_before_submit(h.opening)
    held = h.service._webull_mirror_deferred_by_slot[h.opening.payload.metadata["fanout_slot_id"]]
    if mismatch == "wire":
        held.local_no_wire = False
    elif mismatch == "account":
        held.event.payload.broker_account_name = "different-account"
    else:
        held.event.payload.metadata["rpg_resting_generation" if mismatch == "generation" else "cw_entry_slot"] = "changed"
    token, _ = await begin(h, "webull")
    assert HandoffJournal(h.factory).read(token)["phase"] == "held_unknown"
    assert not h.adapter.opens and not h.adapter.cancels


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain_dispatch", [False, True])
async def test_r_t5_actual_veea_persisted_precheck_proof_recovers_after_restart(monkeypatch, uncertain_dispatch):
    h = await recorded_state(monkeypatch, "VEEA")
    source = next(row for row in RECORDED["intents"] if row["account"] == "live:orb" and
                  row["payload"]["metadata"].get("rpg_resting_generation") == "cf1c5bef-1928-49aa-91e2-506bf44478be")
    with h.factory() as session:
        strategy = h.service.store.ensure_strategy(session, "schwab_1m_v2", name="v2")
        account = h.service.store.ensure_broker_account(session, "live:orb", provider="webull", environment="test")
        event = TradeIntentEvent(event_id=UUID(source["payload"]["event_id"]), source_service="schwab-1m-v2",
            payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name="live:orb", symbol="VEEA",
                side="buy", intent_type="open", quantity=Decimal(source["quantity"]), reason=source["reason"],
                metadata=deepcopy(source["payload"]["metadata"])))
        intent = h.service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
        intent.status, intent.payload = "rejected", deepcopy(source["payload"])
        if uncertain_dispatch:
            event.event_id = uuid4()
            unknown = h.service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
            unknown.status = "submitted"
        session.commit()
    token = UUID(next(key for key, job in h.strategy._rpg_handoffs.items() if job["phase"] == "held_unknown"))
    await h.service._rpg_advance(token)
    recovered = HandoffJournal(h.factory).read(token)
    if uncertain_dispatch:
        assert recovered["phase"] == "held_unknown" and not recovered["local_no_wire"]
        assert not h.adapter.cancels and not h.adapter.reads and not h.adapter.opens
        return
    assert recovered["phase"] == "clear" and recovered["local_no_wire"]
    assert recovered["old"]["client_order_id"] == "schwab_1m_v2-VEEA-open-2d004a27acd7"
    assert not h.adapter.cancels and not h.adapter.reads
    await feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == "placed"
    assert h.adapter.opens[-1].broker_account_name == "live:orb"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unknown", "working", "cancelled_empty", "fills"])
async def test_r_t5_uncertain_webull_dispatch_reconciles_exact_client_without_resubmit(monkeypatch, outcome):
    h = await runtime(monkeypatch, "webull")
    token, _ = await begin(h, "webull")
    submit = h.adapter.submit_order

    async def uncertain(request):
        if request.intent_type == "open":
            raise TimeoutError("CONTROLLED lost answer")
        return await submit(request)

    monkeypatch.setattr(h.adapter, "submit_order", uncertain)
    await feedback(h)
    journal = HandoffJournal(h.factory)
    job = journal.read(token)
    assert job["phase"] == "submit_unknown"
    replacement = job["replacement"]["client_order_id"]
    h.adapter.override = AtrBuyReadback(outcome, "CONTROLLED exact client detail",
        Decimal(1) if outcome == "fills" else None if outcome == "unknown" else Decimal(0),
        Decimal("3.05") if outcome == "fills" else None,
        terminal_cancel=outcome == "cancelled_empty", broker_order_id="simulated-replacement")
    await h.service._rpg_advance(token)
    assert h.adapter.reads[-1].client_order_id == replacement
    assert journal.read(token)["phase"] == {"unknown": "submit_unknown", "working": "placed",
                                            "cancelled_empty": "refused", "fills": "filled"}[outcome]
    await h.service._rpg_advance(token)
    assert not h.adapter.opens
    return h


@pytest.mark.asyncio
async def test_runtime_worker_reader_close_cannot_rollback_serial_writer():
    factory = _session_factory()
    with factory.kw["bind"].begin() as connection:
        connection.execute(text("CREATE TABLE isolation_probe (value INTEGER NOT NULL)"))
        connection.execute(text("INSERT INTO isolation_probe VALUES (0)"))
    with factory() as writer:
        writer.execute(text("UPDATE isolation_probe SET value=1"))
        writer_connection = writer.connection().connection.driver_connection

        def read_and_close():
            with factory() as reader:
                assert reader.connection().connection.driver_connection is not writer_connection
                assert reader.scalar(text("SELECT value FROM isolation_probe")) == 0

        await asyncio.to_thread(read_and_close)
        writer.commit()
    with factory() as reader:
        assert reader.scalar(text("SELECT value FROM isolation_probe")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", range(50))
async def test_r_t5_uncertain_webull_dispatch_to_fill_50_repeats(monkeypatch, repeat):
    h = await test_r_t5_uncertain_webull_dispatch_reconciles_exact_client_without_resubmit(
        monkeypatch, "fills")
    await asyncio.gather(*tuple(getattr(h.service, "_webull_protect_tasks", ())))
    journal = HandoffJournal(h.factory)
    token, job = journal.jobs()[0]
    assert job["phase"] == "filled" and job["replacement_filled"]
    reads = len(h.adapter.reads)
    for _ in range(3):
        await h.service._rpg_advance(token)
        await feedback(h)
        h.strategy._cw_v2_resting_track(h.state, None)
        assert not h.strategy.drain_pending_intents()
        assert not h.strategy.drain_webull_direct_intents()
    assert len(h.adapter.reads) == reads and not h.adapter.opens
    assert h.state.cw_resting_taken and not h.state.resting_active
    assert journal.read(token)["phase"] == "filled"
    with h.factory() as session:
        fills = list(session.scalars(select(Fill)))
        assert len(fills) == 1 and fills[0].quantity == 1 and fills[0].price == Decimal("3.05")
        assert session.get(BrokerOrder, fills[0].order_id).client_order_id == job["replacement"]["client_order_id"]
        positions = list(session.scalars(select(OmsManagedPosition)))
        assert len(positions) == 1 and positions[0].current_quantity == 1


@pytest.mark.asyncio
async def test_recorded_apus_9_7117_percent_distance_still_refuses_before_wire(monkeypatch):
    h = await runtime(monkeypatch, "webull")
    source = next(row for row in RECORDED["intents"] if row["account"] == "live:orb" and
                  row["payload"]["metadata"].get("webull_shape_market_price") == "4.76")
    event = TradeIntentEvent(source_service="schwab-1m-v2", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name="live:orb", symbol="APUS", side="buy",
        intent_type="open", quantity=Decimal(source["quantity"]), reason=source["reason"],
        metadata=deepcopy(source["payload"]["metadata"])))
    h.clock[0] = datetime.fromisoformat(event.payload.metadata["webull_shape_market_at_utc"])
    assert h.service._defer_webull_resting_mirror_before_submit(event)
    assert not h.adapter.opens


@pytest.mark.asyncio
async def test_distance_retry_persists_exact_client_before_an_uncertain_wire(monkeypatch):
    h = await runtime(monkeypatch, "webull")
    with h.factory() as session:
        session.execute(delete(BrokerOrder))
        session.commit()
    h.opening.payload.metadata.update(webull_shape_market_price="2.50", webull_shape_market_at_utc=h.clock[0].isoformat())
    assert h.service._defer_webull_resting_mirror_before_submit(h.opening)
    held = h.service._webull_mirror_deferred_by_slot[h.opening.payload.metadata["fanout_slot_id"]]
    held.queued, held.attempts = True, 1
    retry = h.opening.model_copy(deep=True)
    retry.event_id = uuid4()
    retry.payload.metadata.update(webull_deferred_resubmit="true", webull_deferred_resubmit_attempt="1")
    h.service._latest_quotes_by_symbol[h.state.symbol] = {"ask": 3.05, "received_at": h.clock[0]}

    async def lost_answer(request):
        with h.factory() as session:
            pending = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
            assert pending is not None and pending.status == "pending"
        assert not held.local_no_wire
        raise TimeoutError("CONTROLLED uncertain dispatch")

    monkeypatch.setattr(h.adapter, "submit_order", lost_answer)
    with pytest.raises(TimeoutError, match="CONTROLLED"):
        await h.service.process_trade_intent(retry)
    token, _ = await begin(h, "webull")
    assert HandoffJournal(h.factory).read(token)["phase"] == "waiting"
    assert not h.adapter.opens
