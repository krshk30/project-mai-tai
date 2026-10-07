"""SXTC October 7 own database/log pull; broker acceptance is never invented.

Stored tickets/intents are exact. Clock/generation/fill/race controls explicitly
modify that recording. No test claims Schwab will accept an ineligible symbol.
"""
import json
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, Fill, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, SNAPSHOT_TYPE, rpg_buy_owned
from project_mai_tai.oms.mirror_retained_hold import SNAPSHOT_TYPE as MIRROR_SNAPSHOT
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from tests.unit.test_rpg1_runtime import runtime


RAW = Path(__file__).parents[2] / "docs/review-artifacts/rpgnowire1/raw"


def rows(name):
    return [json.loads(line) for line in (RAW / name).read_text().splitlines() if line.startswith("{")]


TICKETS = rows("sxtc-tickets-20261007.jsonl")
INTENTS = rows("sxtc-intents-20261007.jsonl")


async def sxtc(monkeypatch, *, terminal_phase="refused"):
    h = await runtime(monkeypatch, "webull", notional=300)
    h.clock[0] = datetime.fromisoformat("2026-10-07T14:12:00+00:00")
    h.events = {}
    with h.factory() as session:
        session.execute(delete(BrokerOrder))
        session.execute(delete(TradeIntent))
        for source in INTENTS:
            event = TradeIntentEvent(event_id=UUID(source["payload"]["event_id"]),
                source_service=source["payload"]["source_service"],
                produced_at=datetime.fromisoformat(source["created_at"]), payload=TradeIntentPayload(
                    strategy_code=source["strategy"], broker_account_name=source["account"],
                    symbol=source["symbol"], side=source["side"], intent_type=source["intent_type"],
                    quantity=Decimal(str(source["quantity"])), reason=source["reason"],
                    metadata=deepcopy(source["payload"]["metadata"])))
            account = h.service.store.ensure_broker_account(session, source["account"], provider=
                "webull" if source["account"] == "live:orb" else "schwab", environment="test")
            strategy = h.service.store.ensure_strategy(session, source["strategy"], name="v2")
            intent = h.service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
            intent.id = UUID(source["id"])
            intent.status, intent.payload = source["status"], deepcopy(source["payload"])
            h.events[str(intent.id)] = event
        for source in TICKETS:
            payload = deepcopy(source["payload"])
            # Controlled terminal transition of the recording, not a claim that
            # the captured held_unknown/price_wait tickets were already terminal.
            if terminal_phase is not None:
                payload["phase"] = terminal_phase
            session.add(DashboardSnapshot(id=UUID(source["id"]), snapshot_type=SNAPSHOT_TYPE,
                payload=payload))
        session.commit()
    return h


async def serial_tick(h, token):
    await h.service._handle_stream_message({"data": json.dumps({"event_type": "atr_reprice_tick", "token": str(token)})})


@pytest.mark.asyncio
@pytest.mark.parametrize("source", TICKETS, ids=["Schwab-cache-refusal", "Webull-thirteen-held-replacements"])
@pytest.mark.parametrize("terminal_phase", ["refused", "expired"])
async def test_sxtc_serial_proof_feedback_releases_only_matching_v2_leg_no_saved_buy(monkeypatch, source, terminal_phase):
    h = await sxtc(monkeypatch, terminal_phase=terminal_phase)
    token = UUID(source["id"])
    state = h.strategy.watchlist_state("SXTC")
    state.fanout_segment_id = source["payload"]["segment_id"]
    state.resting_active = state.resting_is_broker_order = True
    state.resting_slot = "first"
    generation = source["payload"]["old"]["metadata"]["rpg_resting_generation"]
    state.resting_schwab_generation = state.resting_webull_generation = generation
    state.resting_schwab_quantity, state.resting_webull_quantity = 185, 92
    state.webull_resting_active = True
    h.strategy._rpg_handoffs = {row["id"]: deepcopy(row["payload"]) for row in TICKETS}
    account = source["payload"]["old"]["broker_account_name"]
    assert h.strategy._rpg_entry_owned(state, account=account, slot="first")
    await serial_tick(h, token)
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "refused" and job["local_no_wire"]
    assert job["release_reason"] == "old_local_no_wire_return_to_strategy"
    revision = job["revision"]
    await h.bot._rpg_handoff_pass()
    assert not h.strategy._rpg_entry_owned(state, account=account, slot="first")
    if account == "live:orb":
        assert state.resting_webull_quantity == 0 and state.resting_webull_generation == ""
        assert state.resting_schwab_quantity == 185 and state.resting_schwab_generation == generation
    else:
        assert state.resting_schwab_quantity == 0 and state.resting_schwab_generation == ""
        assert state.resting_webull_quantity == 92 and state.resting_webull_generation == generation
    assert not h.adapter.opens and not h.adapter.cancels and not h.adapter.reads
    for _ in range(3):
        await serial_tick(h, token)
        await h.bot._rpg_handoff_pass()
    assert not h.adapter.opens
    assert HandoffJournal(h.factory).read(token)["revision"] == revision
    captures = rows("sxtc-next-bar-20261007.jsonl")
    bar = next(row for row in captures if row.get("bar_time") == "2026-10-07T14:12:00+00:00")
    quote = next(row for row in captures if row.get("event_ts", "") >= "2026-10-07T14:13:03" and row.get("ask_price"))
    h.clock[0] = datetime.fromisoformat(quote["received_at"])
    state.bars.append(OHLCVBar(int(datetime.fromisoformat(bar["bar_time"]).timestamp()*1000),
        *[float(bar[key]) for key in ("open_price", "high_price", "low_price", "close_price")], bar["volume"]))
    probe = next(line for line in (RAW / "sxtc-v2-20261007.txt").read_text().splitlines() if "14:13:" in line)
    state.atr_state, state.atr_state_age = "short", int(probe.split("age=")[1].split()[0])
    state.atr_trail = float(probe.split("trail=")[1].split()[0])
    state.atr_short_flip_bar_ts = int(source["payload"]["old"]["metadata"]["rpg_short_segment"])
    state.last_quote = Quote("SXTC", float(quote["bid_price"]), float(quote["ask_price"]),
        float(quote.get("last_price") or bar["close_price"]), int(datetime.fromisoformat(quote["event_ts"]).timestamp()*1000))
    h.strategy._cw_v2_resting_track(state, None)
    primary = h.strategy.drain_pending_intents()
    mirror = h.strategy.drain_webull_direct_intents()
    selected = primary if account == "live:schwab_1m_v2" else mirror
    assert len([draft for draft in selected if draft.intent_type == "open"]) == 1
    assert not [draft for draft in (mirror if account == "live:schwab_1m_v2" else primary) if draft.intent_type == "open"]
    assert not h.adapter.opens  # Draft != acceptance: Schwab eligibility is unchanged.


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["generation", "account", "slot", "fanout_slot", "segment", "origin", "unclassified", "quantity"])
async def test_sxtc_exact_intent_identity_or_positive_refusal_missing_stays_owned(monkeypatch, mutation):
    h = await sxtc(monkeypatch)
    source = TICKETS[0]
    with h.factory() as session:
        intent = session.get(TradeIntent, UUID(INTENTS[0]["id"]))
        payload = deepcopy(intent.payload)
        if mutation in {"generation", "slot"}:
            payload["metadata"]["rpg_resting_generation" if mutation == "generation" else "cw_entry_slot"] = "foreign"
        elif mutation in {"fanout_slot", "segment"}:
            payload["metadata"]["fanout_slot_id" if mutation == "fanout_slot" else "fanout_segment_id"] = "foreign"
        elif mutation == "origin":
            payload["refusal_origin"] = "broker"
        elif mutation == "unclassified":
            payload["refusal_code"] = "unknown"
        elif mutation == "quantity":
            intent.quantity += 1
        else:
            intent.broker_account_id = h.service.store.ensure_broker_account(
                session, "foreign-account", provider="schwab", environment="test").id
            # Isolate account scope from the sibling's different quantity veto.
            # This is a counterfactual control, not a historical deletion.
            session.execute(delete(TradeIntent).where(TradeIntent.id != intent.id))
        intent.payload = payload
        session.commit()
    await serial_tick(h, UUID(source["id"]))
    job = HandoffJournal(h.factory).read(UUID(source["id"]))
    assert job["phase"] == "refused" and not job.get("local_no_wire")
    assert rpg_buy_owned(job)
    assert not h.adapter.opens


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["accepted", "rejected", "partially_filled"])
@pytest.mark.parametrize("generation", ["old", "replacement"])
@pytest.mark.parametrize("same_client", [True, False], ids=["exact-client", "other-client-same-generation"])
async def test_sxtc_later_broker_order_of_exact_generation_never_becomes_no_wire(monkeypatch, status, generation, same_client):
    h = await sxtc(monkeypatch)
    source = TICKETS[1]
    job = source["payload"]
    request = job["old"] if generation == "old" else job["replacement"]
    event = next(event for event in h.events.values() if
        event.payload.broker_account_name == request["broker_account_name"] and
        event.payload.metadata["rpg_resting_generation"] == request["metadata"]["rpg_resting_generation"])
    with h.factory() as session:
        intent = session.get(TradeIntent, next(UUID(key) for key, value in h.events.items() if value is event))
        h.service.store.get_or_create_order(session, intent=intent, strategy_id=intent.strategy_id,
            broker_account_id=intent.broker_account_id, client_order_id=h.service._build_client_order_id(event)
                if same_client else "controlled-other-client-same-generation",
            broker_order_id="controlled-later-dispatch", symbol="SXTC", side="buy", quantity=event.payload.quantity,
            metadata=dict(event.payload.metadata), status=status, order_type="STOP_LIMIT", time_in_force="day")
        session.commit()
    # Call the proof-only operation: the ordinary runtime retains its existing
    # cancellation/reconciliation behaviour for a genuinely wired order.
    assert h.service._rpg_release_unwired(UUID(source["id"]), HandoffJournal(h.factory).read(UUID(source["id"]))) is None
    assert HandoffJournal(h.factory).read(UUID(source["id"]))["phase"] == "refused"


@pytest.mark.asyncio
async def test_sxtc_stale_journal_revision_cannot_release(monkeypatch):
    h = await sxtc(monkeypatch)
    token = UUID(TICKETS[0]["id"])
    journal = HandoffJournal(h.factory)
    old = journal.read(token)
    journal.change(token, old["revision"], no_rebuy=True)
    assert h.service._rpg_release_unwired(token, old) is None
    assert journal.read(token)["no_rebuy"]


def retained_record():
    line = (RAW / "sxtc-retained-before-rollback-20261007.txt").read_text().strip()
    row_id, payload = line.split("|", 1)
    return UUID(row_id), json.loads(payload)


@pytest.mark.asyncio
async def test_recorded_sxtc_retained_hold_queue_fenced_then_new_v2_generation_admitted(monkeypatch):
    h = await sxtc(monkeypatch)
    h.service.settings = h.service.settings.model_copy(update={"oms_v2_webull_mirror_retained_hold_enabled": True})
    row_id, data = retained_record()
    # Controlled queued-copy race of the actual saved held event; no invented tape.
    data.update(phase="queued", token="controlled-stale-serial-token")
    queued = TradeIntentEvent.model_validate(data["event"])
    queued.payload.metadata["mirrorhold_token"] = data["token"]
    with h.factory() as session:
        session.add(DashboardSnapshot(id=row_id, snapshot_type=MIRROR_SNAPSHOT, payload=data))
        session.commit()
    h.service._mirrorhold_durable_owner_ids = {row_id}
    h.service._mirrorhold_project(data)
    token = UUID(TICKETS[1]["id"])
    await serial_tick(h, token)
    with h.factory() as session:
        transferred = deepcopy(session.get(DashboardSnapshot, row_id).payload)
    assert transferred["phase"] == "prepared" and transferred["token"] == ""
    assert transferred["wire_submissions"] == data["wire_submissions"] == 0
    assert HandoffJournal(h.factory).read(token)["phase"] == "refused"
    await h.service._handle_stream_message({"data": queued.model_dump_json()})
    assert not h.adapter.opens
    fresh = queued.model_copy(deep=True)
    fresh.event_id = uuid4()
    fresh.produced_at = h.clock[0]
    for key in ("mirrorhold_token", "rpg_handoff_token", "rpg_event_id", "webull_deferred_resubmit_attempt"):
        fresh.payload.metadata.pop(key, None)
    fresh.payload.metadata["rpg_resting_generation"] = "controlled-next-normal-v2-generation"
    assert h.service._mirrorhold_admit(fresh)
    with h.factory() as session:
        promoted = h.service._mirrorhold_upsert(session, fresh, no_wire=True)
        assert promoted.payload["phase"] == "held"
        assert promoted.payload["wire_submissions"] == 0
        assert promoted.payload["event"]["event_id"] == str(fresh.event_id)
        session.commit()
    assert not h.service._mirrorhold_admit(queued)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["dispatching", "uncertain", "filled", "wire_count", "wire_client", "generation"])
async def test_retained_unknown_dispatch_fill_or_other_generation_cannot_transfer(monkeypatch, control):
    h = await sxtc(monkeypatch)
    row_id, data = retained_record()
    if control in {"dispatching", "uncertain", "filled"}:
        data["phase"] = control
    elif control == "wire_count":
        data["wire_submissions"] = 1
    elif control == "wire_client":
        data["wire_clients"] = ["controlled-wired-client"]
    else:
        data["event"]["payload"]["metadata"]["rpg_resting_generation"] = "newer-generation"
    with h.factory() as session:
        session.add(DashboardSnapshot(id=row_id, snapshot_type=MIRROR_SNAPSHOT, payload=data))
        session.commit()
    token = UUID(TICKETS[1]["id"])
    assert h.service._rpg_release_unwired(token, HandoffJournal(h.factory).read(token)) is None
    assert HandoffJournal(h.factory).read(token)["phase"] == "refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_generation", [False, True])
async def test_actual_retired_sxtc_row_requires_exact_current_generation(monkeypatch, wrong_generation):
    h = await sxtc(monkeypatch)
    recorded = rows("sxtc-retained-after-rollback-20261007.jsonl")[0]
    assert recorded["payload"]["phase"] == "retired"
    assert recorded["payload"]["reason"] == "operator_rollback_20261007"
    assert recorded["snapshot_type"] == "oms_webull_mirror_retained_hold__archived_20261007"
    # Replay the recorded retired payload as an active-type retained owner to
    # prove the pre-archive state. This is not today's archived production state.
    data = deepcopy(recorded["payload"])
    if wrong_generation:
        data["event"]["payload"]["metadata"]["rpg_resting_generation"] = "different-held-generation"
    with h.factory() as session:
        session.add(DashboardSnapshot(id=UUID(recorded["id"]), snapshot_type=MIRROR_SNAPSHOT, payload=data))
        session.commit()
    token = UUID(TICKETS[1]["id"])
    released = h.service._rpg_release_unwired(token, HandoffJournal(h.factory).read(token))
    assert (released is None) == wrong_generation
    with h.factory() as session:
        assert session.get(DashboardSnapshot, UUID(recorded["id"])).payload == data


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_hint", ["webull_wire_submitted_at_utc", "broker_order_id", "webull_local_no_wire", "filled_quantity"])
async def test_pre_submit_label_with_contradictory_wire_or_fill_evidence_stays_owned(monkeypatch, wire_hint):
    h = await sxtc(monkeypatch)
    with h.factory() as session:
        intent = session.get(TradeIntent, UUID(INTENTS[0]["id"]))
        payload = deepcopy(intent.payload)
        payload["metadata"][wire_hint] = "false" if wire_hint == "webull_local_no_wire" else "1"
        intent.payload = payload
        session.commit()
    token = UUID(TICKETS[0]["id"])
    await serial_tick(h, token)
    assert HandoffJournal(h.factory).read(token)["phase"] == "refused"


@pytest.mark.asyncio
async def test_later_accepted_client_order_without_generation_still_blocks_no_wire(monkeypatch):
    h = await sxtc(monkeypatch)
    event = h.events[INTENTS[0]["id"]]
    with h.factory() as session:
        intent = session.get(TradeIntent, UUID(INTENTS[0]["id"]))
        md = dict(event.payload.metadata)
        md.pop("rpg_resting_generation")
        h.service.store.get_or_create_order(session, intent=intent, strategy_id=intent.strategy_id,
            broker_account_id=intent.broker_account_id, client_order_id=h.service._build_client_order_id(event),
            broker_order_id="controlled-wired-without-generation", symbol="SXTC", side="buy", quantity=185,
            metadata=md, status="accepted", order_type="STOP_LIMIT", time_in_force="day")
        session.commit()
    token = UUID(TICKETS[0]["id"])
    assert h.service._rpg_release_unwired(token, HandoffJournal(h.factory).read(token)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True], ids=["fill-generation", "fill-metadata-generation"])
async def test_positive_fill_without_matching_order_generation_still_blocks_no_wire(monkeypatch, nested):
    h = await sxtc(monkeypatch)
    source = INTENTS[0]
    with h.factory() as session:
        intent = session.get(TradeIntent, UUID(source["id"]))
        order = h.service.store.get_or_create_order(session, intent=intent, strategy_id=intent.strategy_id,
            broker_account_id=intent.broker_account_id, client_order_id="controlled-other-fill-parent",
            broker_order_id="controlled-fill-parent", symbol="SXTC", side="buy", quantity=185,
            metadata={}, status="filled", order_type="STOP_LIMIT", time_in_force="day")
        payload = {"rpg_resting_generation": source["payload"]["metadata"]["rpg_resting_generation"]}
        session.add(Fill(order_id=order.id, strategy_id=intent.strategy_id, broker_account_id=intent.broker_account_id,
            symbol="SXTC", side="buy", quantity=1, price=Decimal("3.17"),
            payload={"metadata": payload} if nested else payload))
        session.commit()
    token = UUID(TICKETS[0]["id"])
    await serial_tick(h, token)
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "refused" and not job["local_no_wire"]
    assert not h.adapter.opens


@pytest.mark.asyncio
async def test_archived_actual_retired_owner_is_not_reactivated(monkeypatch):
    h = await sxtc(monkeypatch)
    recorded = rows("sxtc-retained-after-rollback-20261007.jsonl")[0]
    with h.factory() as session:
        session.add(DashboardSnapshot(id=UUID(recorded["id"]), snapshot_type=recorded["snapshot_type"],
            payload=deepcopy(recorded["payload"])))
        session.commit()
    token = UUID(TICKETS[1]["id"])
    await serial_tick(h, token)
    assert HandoffJournal(h.factory).read(token)["release_reason"] == "old_local_no_wire_return_to_strategy"
    with h.factory() as session:
        row = session.get(DashboardSnapshot, UUID(recorded["id"]))
        assert row.snapshot_type == recorded["snapshot_type"] and row.payload == recorded["payload"]


@pytest.mark.asyncio
async def test_prepared_transfer_survives_restart_without_old_serial_dispatch(monkeypatch):
    h = await sxtc(monkeypatch)
    h.service.settings = h.service.settings.model_copy(update={"oms_v2_webull_mirror_retained_hold_enabled": True})
    row_id, data = retained_record()
    data.update(phase="queued", token="controlled-claimed-before-restart")
    stale = TradeIntentEvent.model_validate(data["event"])
    stale.payload.metadata["mirrorhold_token"] = data["token"]
    with h.factory() as session:
        session.add(DashboardSnapshot(id=row_id, snapshot_type=MIRROR_SNAPSHOT, payload=data))
        session.commit()
    await serial_tick(h, UUID(TICKETS[1]["id"]))
    # A new service object uses the same durable database, not the old projection.
    restarted = await runtime(monkeypatch, "webull", notional=300)
    restarted.service.session_factory = h.factory
    restarted.service.settings = h.service.settings
    await restarted.service._handle_stream_message({"data": stale.model_dump_json()})
    assert not restarted.adapter.opens
    with h.factory() as session:
        assert session.get(DashboardSnapshot, row_id).payload["phase"] == "prepared"
    fresh = stale.model_copy(deep=True)
    fresh.event_id, fresh.produced_at = uuid4(), h.clock[0]
    for key in ("mirrorhold_token", "rpg_handoff_token", "rpg_event_id", "webull_deferred_resubmit_attempt"):
        fresh.payload.metadata.pop(key, None)
    fresh.payload.metadata["rpg_resting_generation"] = "controlled-new-v2-after-restart"
    assert restarted.service._mirrorhold_admit(fresh)


@pytest.mark.asyncio
async def test_recorded_primary_no_wire_begin_cancel_retains_active_protocol(monkeypatch):
    h = await sxtc(monkeypatch)
    token = UUID(TICKETS[0]["id"])
    event = TradeIntentEvent(source_service="schwab-1m-v2", produced_at=h.clock[0],
        payload=TradeIntentPayload.model_validate({**TICKETS[0]["payload"]["old"], "intent_type": "cancel"}))
    with h.factory() as session:
        session.delete(session.get(DashboardSnapshot, token))
        session.commit()
    await h.service._handle_stream_message({"data": event.model_dump_json()})
    jobs = HandoffJournal(h.factory).jobs()
    primary = [job for _, job in jobs if job["old"]["broker_account_name"] == "live:schwab_1m_v2"]
    assert len(primary) == 1
    assert primary[0]["phase"] == "clear" and primary[0]["local_no_wire"]
    assert "release_reason" not in primary[0]
    assert rpg_buy_owned(primary[0])
    assert not h.adapter.opens and not h.adapter.cancels


@pytest.mark.asyncio
async def test_wired_unknown_primary_actual_serial_lane_remains_blocking(monkeypatch):
    h = await sxtc(monkeypatch, terminal_phase=None)
    event = h.events[INTENTS[0]["id"]]
    with h.factory() as session:
        intent = session.get(TradeIntent, UUID(INTENTS[0]["id"]))
        h.service.store.get_or_create_order(session, intent=intent, strategy_id=intent.strategy_id,
            broker_account_id=intent.broker_account_id, client_order_id=h.service._build_client_order_id(event),
            broker_order_id="controlled-accepted-old", symbol="SXTC", side="buy", quantity=185,
            metadata=dict(event.payload.metadata), status="accepted", order_type="STOP_LIMIT", time_in_force="day")
        session.commit()
    token = UUID(TICKETS[0]["id"])
    for _ in range(3):
        await serial_tick(h, token)
        await h.bot._rpg_handoff_pass()
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "held_unknown" and not job["local_no_wire"]
    assert h.strategy._rpg_entry_owned(h.strategy.watchlist_state("SXTC"), account="live:schwab_1m_v2")
    assert not h.adapter.opens


@pytest.mark.asyncio
async def test_no_wire_feedback_never_clears_a_newer_v2_generation_latch(monkeypatch):
    h = await sxtc(monkeypatch)
    state = h.strategy.watchlist_state("SXTC")
    state.fanout_segment_id = TICKETS[0]["payload"]["segment_id"]
    state.resting_active = state.resting_is_broker_order = True
    state.resting_slot = "first"
    state.resting_schwab_generation, state.resting_schwab_quantity = "controlled-newer-v2-generation", 185
    state.resting_webull_generation, state.resting_webull_quantity = "controlled-newer-v2-generation", 92
    h.strategy._rpg_handoffs = {row["id"]: deepcopy(row["payload"]) for row in TICKETS}
    for source in TICKETS:
        await serial_tick(h, UUID(source["id"]))
    await h.bot._rpg_handoff_pass()
    assert state.resting_schwab_generation == state.resting_webull_generation == "controlled-newer-v2-generation"
    assert state.resting_schwab_quantity == 185 and state.resting_webull_quantity == 92
    assert not h.adapter.opens


@pytest.mark.asyncio
@pytest.mark.parametrize("filled_guard", ["no_rebuy", "replacement_filled"])
async def test_positive_fill_latch_never_released_even_with_local_intent_proof(monkeypatch, filled_guard):
    h = await sxtc(monkeypatch)
    token = UUID(TICKETS[1]["id"])
    journal = HandoffJournal(h.factory)
    job = journal.read(token)
    job = journal.change(token, job["revision"], **{filled_guard: True})
    assert h.service._rpg_release_unwired(token, job) is None
    assert journal.read(token)[filled_guard] and rpg_buy_owned(journal.read(token))


@pytest.mark.asyncio
@pytest.mark.parametrize("source", TICKETS, ids=["Schwab-held-unknown", "Webull-price-wait"])
async def test_recorded_sxtc_nonterminal_positive_proof_preserves_existing_serial_protocol(monkeypatch, source):
    h = await sxtc(monkeypatch, terminal_phase=None)
    token = UUID(source["id"])
    journal = HandoffJournal(h.factory)
    before = journal.read(token)
    assert before["phase"] in {"held_unknown", "price_wait"}
    # Positive exact durable intent proof is present, but phase is not terminal.
    assert h.service._rpg_release_unwired(token, before) is None
    assert journal.read(token) == before
    state = h.strategy.watchlist_state("SXTC")
    state.fanout_segment_id = before["segment_id"]
    state.resting_active = state.resting_is_broker_order = state.webull_resting_active = True
    state.resting_slot = "first"
    generation = before["old"]["metadata"]["rpg_resting_generation"]
    state.resting_schwab_generation = state.resting_webull_generation = generation
    state.resting_schwab_quantity, state.resting_webull_quantity = 185, 92
    h.strategy._rpg_handoffs = {row["id"]: deepcopy(row["payload"]) for row in TICKETS}
    await serial_tick(h, token)
    await h.bot._rpg_handoff_pass()
    after = journal.read(token)
    assert after["phase"] == ("clear" if before["phase"] == "held_unknown" else "price_wait")
    assert "release_reason" not in after
    assert after.get("replacement") == before.get("replacement")
    assert rpg_buy_owned(after)
    assert h.strategy._rpg_entry_owned(state, account=before["old"]["broker_account_name"], slot="first")
    assert state.resting_webull_generation == generation and state.resting_webull_quantity == 92
    if before["phase"] == "held_unknown":
        # Existing clear authorization retires the exact old primary latch;
        # the active ticket still owns that account and the saved replacement.
        assert state.resting_schwab_generation == "" and state.resting_schwab_quantity == 0
    else:
        assert state.resting_schwab_generation == generation and state.resting_schwab_quantity == 185
    assert not h.adapter.opens and not h.adapter.cancels and not h.adapter.reads
    assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "waiting", "fills_waiting", "clear", "held_unknown",
    "submitting", "submit_unknown", "price_wait", "placed", "filled"])
async def test_positive_no_wire_intent_never_synthesizes_terminal_phase(monkeypatch, phase):
    h = await sxtc(monkeypatch, terminal_phase=phase)
    token = UUID(TICKETS[1]["id"])
    journal = HandoffJournal(h.factory)
    before = journal.read(token)
    assert h.service._rpg_release_unwired(token, before) is None
    assert journal.read(token) == before
    assert rpg_buy_owned(before)
    assert not h.adapter.opens


@pytest.mark.asyncio
@pytest.mark.parametrize("source", TICKETS, ids=["Schwab-cache-refusal", "Webull-held-replacement"])
@pytest.mark.parametrize("terminal_phase", ["refused", "expired"])
async def test_terminal_sxtc_absence_reads_zero_and_label_without_positive_intent_never_release(monkeypatch, source, terminal_phase):
    h = await sxtc(monkeypatch, terminal_phase=terminal_phase)
    token = UUID(source["id"])
    journal = HandoffJournal(h.factory)
    before = journal.read(token)
    with h.factory() as session:
        # Deliberate loss-of-proof control, not a production cleanup. Keep the
        # exact ticket, reads=0 and no BrokerOrder while removing its positive proof.
        session.execute(delete(TradeIntent))
        session.commit()
    assert before["reads"] == 0
    assert h.service._rpg_release_unwired(token, before) is None
    assert journal.read(token) == before
    await serial_tick(h, token)
    assert "release_reason" not in journal.read(token)
    assert rpg_buy_owned(journal.read(token))
    assert not h.adapter.opens


@pytest.mark.asyncio
@pytest.mark.parametrize("source", TICKETS, ids=["Schwab-held-unknown", "Webull-price-wait"])
async def test_sxtc_terminal_copy_cannot_override_locked_nonterminal_phase(monkeypatch, source):
    h = await sxtc(monkeypatch, terminal_phase=None)
    token = UUID(source["id"])
    journal = HandoffJournal(h.factory)
    current = journal.read(token)
    copied = {**current, "phase": "refused"}
    assert h.service._rpg_release_unwired(token, copied) is None
    assert journal.read(token) == current
    assert not h.adapter.opens
