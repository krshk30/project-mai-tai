"""B1-B6 on merged PM source; later tape, ledger races and clocks are controlled."""
import asyncio
from copy import deepcopy
from decimal import Decimal
import json
from uuid import UUID

import pytest
from sqlalchemy import event as sql_event, select

from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback
from project_mai_tai.db.models import BrokerOrder, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, old_buy_proven_clear
from tests.unit.test_rpg1_runtime import begin, feedback, runtime
from tests.unit.test_rpgstuck1_startup import RECORDED, real_bot_startup, real_oms_startup, startup_harness


@pytest.mark.asyncio
async def test_b1_recorded_reto_unknown_primary_clear_webull_places_webull_only(monkeypatch):
    h = await startup_harness(monkeypatch, with_deferred=True)
    await real_bot_startup(monkeypatch, h)
    await real_oms_startup(monkeypatch, h)
    h.strategy._entries_held = True
    await feedback(h)
    await feedback(h)
    h.strategy._entries_held = False
    state = h.strategy.watchlist_state("RETO")
    token = UUID("ff6464ff-d65e-5657-8f47-1dc8d7b053c3")
    old = HandoffJournal(h.factory).read(token)
    assert old["phase"] == "held_unknown" and old["reads"] == 30
    state.resting_schwab_generation = old["old"]["metadata"]["rpg_resting_generation"]
    state.resting_schwab_quantity = int(Decimal(old["old"]["quantity"]))
    primary = (state.resting_schwab_generation, state.resting_schwab_quantity)
    h.strategy._cw_v2_resting_track(state, None)
    assert not h.strategy.drain_pending_intents()
    mirrors = h.strategy.drain_webull_direct_intents()
    assert len(mirrors) == 1
    mirror = mirrors[0]
    assert mirror.symbol == "RETO" and mirror.quantity > 0
    assert (state.resting_schwab_generation, state.resting_schwab_quantity) == primary
    h.service._latest_quotes_by_symbol["RETO"] = {"ask": Decimal("3.06"), "received_at": h.clock[0]}
    await h.service.process_trade_intent(TradeIntentEvent(source_service="schwab-1m-v2", produced_at=h.clock[0],
        payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name="live:orb",
            symbol="RETO", side="buy", intent_type="open", quantity=mirror.quantity,
            reason=mirror.reason, metadata=mirror.metadata)))
    assert len(h.adapter.opens) == 1 and h.adapter.opens[0].broker_account_name == "live:orb"
    assert (state.resting_schwab_generation, state.resting_schwab_quantity) == primary
    assert HandoffJournal(h.factory).read(token)["phase"] == "held_unknown"
    h.strategy._cw_v2_resting_track(state, None)
    assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["accepted", "rejected"])
@pytest.mark.parametrize("symbol", ["APUS", "VEEA", "RETO"])
async def test_b2_any_generation_broker_row_vetoes_recorded_local_proof(monkeypatch, status, symbol):
    h = await startup_harness(monkeypatch, with_deferred=True)
    with h.factory() as session:
        intent = next(row for row in session.scalars(select(TradeIntent))
            if row.symbol == symbol and (row.payload or {}).get("refusal_code") == "webull_mirror_precheck_deferred")
        h.service.store.get_or_create_order(session, intent=intent, strategy_id=intent.strategy_id,
            broker_account_id=intent.broker_account_id, client_order_id="CONTROLLED-wire-" + symbol,
            symbol=symbol, side="buy", quantity=intent.quantity, metadata=intent.payload["metadata"],
            status=status, order_type="STOP_LIMIT", time_in_force="day")
        session.commit()
    token = next(token for token, job in HandoffJournal(h.factory).jobs()
        if job["old"]["symbol"] == symbol and job["old"]["broker_account_name"] == "live:orb"
        and job["phase"] == "held_unknown")
    await h.service._rpg_advance(token)
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "held_unknown" and not old_buy_proven_clear(job)
    assert not h.adapter.opens and not h.adapter.cancels


@pytest.mark.asyncio
@pytest.mark.parametrize("origin,code,clear", [
    ("skipped_before_submit", "webull_mirror_precheck_deferred", True),
    ("broker", "webull_mirror_precheck_deferred", False),
    ("client_abort", "webull_mirror_precheck_deferred", False),
    ("skipped_before_submit", "webull_mirror_fresh_price_wait", False),
    ("skipped_before_submit", "PRICE_AGGRESSIVE", False),
])
async def test_b3_only_recorded_precheck_origin_and_code_prove_persisted_no_wire(monkeypatch, origin, code, clear):
    rows = deepcopy(RECORDED["deferred_intents"])
    row = next(row for row in rows if row["symbol"] == "RETO")
    row["payload"].update(refusal_origin=origin, refusal_code=code)
    h = await startup_harness(monkeypatch, with_deferred=True, deferred_rows=rows)
    token = UUID("ba108172-04f6-5659-892b-a0fc10d22b15")
    await h.service._rpg_advance(token)
    job = HandoffJournal(h.factory).read(token)
    assert old_buy_proven_clear(job) is clear
    assert job["phase"] == ("clear" if clear else "held_unknown")
    assert not h.adapter.opens and not h.adapter.cancels and not h.adapter.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["entry", "gap"])
@pytest.mark.parametrize("proven", [True, False])
async def test_b4_hold_authorization_waits_for_unproven_old_but_expires_proven_old(monkeypatch, gate, proven):
    h = await runtime(monkeypatch, "schwab")
    if not proven:
        h.adapter.override = AtrBuyReadback("working", "CONTROLLED old order still working", Decimal(0))
    token, _ = await begin(h, "schwab")
    if gate == "entry":
        h.strategy._entries_held = True
    else:
        h.strategy._gap_hold_enabled = h.state.gap_hold_active = True
    job = HandoffJournal(h.factory).read(token)
    assert old_buy_proven_clear(job) is proven
    auth = h.strategy.rpg_handoff_authorization(str(token), job)
    assert auth["reason"] == "entry_or_gap_hold"
    assert auth["verdict"] == ("expired" if proven else "wait")
    await feedback(h)
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == ("expired" if proven else "waiting")
    assert not h.adapter.opens


@pytest.mark.asyncio
async def test_b5_held_unknown_startup_has_no_one_hz_xadd_or_evidence_queries(monkeypatch):
    h = await startup_harness(monkeypatch)
    statements = []
    with h.factory() as session:
        engine = session.get_bind()
    sql_event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    stop = asyncio.Event()
    task = asyncio.create_task(h.service._run_rpg_retry_loop(stop))
    try:
        await asyncio.wait_for(h.service._rpg_retry_started().wait(), timeout=5)
        rows, h.service.redis.entries = h.service.redis.entries, []
        initial = [data for _, data in rows if data.get("event_type") == "atr_reprice_tick"]
        assert len(initial) == 4  # Three exact-old Webull tickets and the strict RETO probe.
        for data in initial:
            await h.service._handle_stream_message({"data": json.dumps(data)})
        quiet_queries = len(statements)
        await asyncio.sleep(12)
        assert len(statements) == quiet_queries
        assert not h.service.redis.entries
        for _, job in HandoffJournal(h.factory).jobs():
            if job["old"]["symbol"] == "RETO" or job["old"]["broker_account_name"] == "live:orb":
                assert job["phase"] in {"held_unknown", "refused"}
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


def commit_deferred(h, symbol):
    row = next(row for row in RECORDED["deferred_intents"] if row["symbol"] == symbol
        and row["payload"].get("refusal_origin") == "skipped_before_submit")
    opening = TradeIntentEvent.model_validate({**row["payload"], "payload": {
        "strategy_code": row["strategy"], "broker_account_name": row["account"],
        "symbol": row["symbol"], "side": "buy", "intent_type": "open",
        "quantity": row["quantity"], "reason": row["reason"], "metadata": row["payload"]["metadata"]}})
    with h.factory() as session:
        strategy = h.service.store.ensure_strategy(session, row["strategy"], name="v2")
        account = h.service.store.ensure_broker_account(session, row["account"], provider="webull", environment="test")
        intent = h.service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=opening)
        intent.id, intent.status, intent.payload = UUID(row["id"]), row["status"], deepcopy(row["payload"])
        session.commit()
    return h.service._build_rejected_event(opening, UUID(row["id"]), reason=row["payload"]["refusal_code"])


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_status", [None, "accepted", "rejected"], ids=["local-only", "accepted-wire", "rejected-wire"])
async def test_b5_matching_committed_generation_wakes_once_and_deduplicates_durable_hash(monkeypatch, wire_status):
    h = await startup_harness(monkeypatch)
    stop = asyncio.Event()
    task = asyncio.create_task(h.service._run_rpg_retry_loop(stop))
    try:
        await asyncio.wait_for(h.service._rpg_retry_started().wait(), timeout=5)
        h.service.redis.entries.clear()
        notification = commit_deferred(h, "RETO")
        if wire_status:
            with h.factory() as session:
                intent = session.get(TradeIntent, notification.payload.intent_db_id)
                h.service.store.get_or_create_order(session, intent=intent, strategy_id=intent.strategy_id,
                    broker_account_id=intent.broker_account_id, client_order_id="CONTROLLED-notified-wire",
                    symbol="RETO", side="buy", quantity=intent.quantity, metadata=intent.payload["metadata"],
                    status=wire_status, order_type="STOP_LIMIT", time_in_force="day")
                session.commit()
        wrong = notification.model_copy(deep=True)
        wrong.payload.metadata["rpg_resting_generation"] = "CONTROLLED-unrelated-generation"
        token, journal = UUID("ba108172-04f6-5659-892b-a0fc10d22b15"), HandoffJournal(h.factory)
        before_wrong = journal.read(token)
        await h.service._publish_order_event(wrong)
        assert journal.read(token) == before_wrong
        await asyncio.sleep(0)
        assert not any(data.get("event_type") == "atr_reprice_tick" for _, data in h.service.redis.entries)
        await h.service._publish_order_event(notification)
        await asyncio.sleep(0.05)
        ticks = [data for _, data in h.service.redis.entries if data.get("event_type") == "atr_reprice_tick"]
        assert len(ticks) == 1 and ticks[0]["token"] == "ba108172-04f6-5659-892b-a0fc10d22b15"
        journal, token = HandoffJournal(h.factory), UUID(ticks[0]["token"])
        before = journal.read(token)
        assert before.get("local_evidence_wakeup_hash") and before["phase"] == "held_unknown"
        await h.service._publish_order_event(notification)
        await asyncio.sleep(0.05)
        assert len([data for _, data in h.service.redis.entries if data.get("event_type") == "atr_reprice_tick"]) == 1
        assert journal.read(token)["revision"] == before["revision"]
        await h.service._handle_stream_message({"data": json.dumps(ticks[0])})
        job = journal.read(token)
        assert job["phase"] == ("held_unknown" if wire_status else "clear")
        assert old_buy_proven_clear(job) is (wire_status is None)
        assert not h.adapter.opens and not h.adapter.cancels
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_evidence", [False, True], ids=["still-unknown", "missed-publication"])
async def test_b5_startup_then180_empty_turns_never_reconciles_unknown_without_notification(monkeypatch, late_evidence):
    h = await startup_harness(monkeypatch)
    clock, scans, waits = [0.0], [], []
    h.bot.redis = h.service.redis
    scan = h.service._rpg_retry_jobs

    def measured_scan(**kwargs):
        scans.append((clock[0], kwargs["include_unknown"]))
        return scan(**kwargs)

    stop = asyncio.Event()

    async def controlled_pause(stop_event, seconds):
        waits.append(seconds)
        rows, h.service.redis.entries = h.service.redis.entries, []
        for _, data in rows:
            if data.get("event_type") == "atr_reprice_tick":
                await h.service._handle_stream_message({"data": json.dumps(data)})
        if clock[0] == 5 and late_evidence:
            commit_deferred(h, "RETO")  # Controlled lost publication; fallback must still read committed truth.
        clock[0] += 1
        if clock[0] > 180:
            stop_event.set()

    monkeypatch.setattr(h.service, "_rpg_retry_jobs", measured_scan)
    monkeypatch.setattr(h.service, "_rpg_retry_pause", controlled_pause)
    await h.service._run_rpg_retry_loop(stop)
    assert scans == [(0, True)] and waits == [None] * 181
    jobs = dict(HandoffJournal(h.factory).jobs())
    local = jobs[UUID("ba108172-04f6-5659-892b-a0fc10d22b15")]
    assert not old_buy_proven_clear(local) and local["phase"] == "held_unknown"
    # A crash between committed evidence and publication is recovered by the
    # next startup scan, not by a permanent timer or a manufactured clearance.
    await h.service._run_rpg_retry_loop(asyncio.Event())
    assert scans == [(0, True), (181, True)]
    local = HandoffJournal(h.factory).read(UUID("ba108172-04f6-5659-892b-a0fc10d22b15"))
    assert old_buy_proven_clear(local) is late_evidence
    assert local["phase"] == ("clear" if late_evidence else "held_unknown")
    for token in ("faa55c1f-262b-52e2-adcc-280ab1e5e2ff", "ee3d0d07-abea-5fe2-98c9-cace5c08d135"):
        assert jobs[UUID(token)]["phase"] == "held_unknown" and not old_buy_proven_clear(jobs[UUID(token)])
    assert not h.adapter.opens and not h.adapter.cancels and len(h.adapter.reads) == 1


@pytest.mark.asyncio
async def test_b5_failed_startup_scan_backs_off_instead_of_zero_delay_spin(monkeypatch):
    h = await startup_harness(monkeypatch)
    clock, attempts, delays = [0.0], [], []
    cadence = max(1.0, float(h.service.settings.oms_broker_sync_interval_seconds))

    def failed_scan(**kwargs):
        attempts.append(clock[0])
        raise RuntimeError("CONTROLLED unavailable evidence store")

    stop = asyncio.Event()

    async def controlled_pause(stop_event, seconds):
        assert seconds == cadence
        delays.append(seconds)
        clock[0] += seconds
        if len(delays) == 3:
            stop_event.set()

    monkeypatch.setattr(h.service, "_rpg_retry_jobs", failed_scan)
    monkeypatch.setattr(h.service, "_rpg_retry_pause", controlled_pause)
    await h.service._run_rpg_retry_loop(stop)
    assert attempts == [0, cadence, cadence * 2] and delays == [cadence] * 3
    assert not h.adapter.opens and not h.adapter.cancels and not h.adapter.reads
    with h.factory() as session:
        assert len(list(session.scalars(select(BrokerOrder)))) == 5
