"""NCPL recorded state/clock; cancellation acknowledgements are explicit controls."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from project_mai_tai.v2_flip_entry_ownership import (
    FlipEntryOwnershipRecord, FlipPositionBook, FlipPositionClose, FlipPositionLeg,
)
from project_mai_tai.v2_removed_wait import RemovedWaitProof, assess_removed_wait

ROOT = Path(__file__).parents[2] / "docs/review-artifacts/retryleft1"
NCPL = json.loads((ROOT / "ncpl-own-read.json").read_text())
ARTL = json.loads((ROOT / "artl-own-read.json").read_text())
APUS = json.loads((ROOT / "apus-both-filled-own-read.json").read_text())
PRIMARY, WEBULL = "live:schwab_1m_v2", "live:orb"
AT = datetime(2026, 10, 7, 19, 10, 18, 69000, tzinfo=UTC)


def recorded(*, phase="provisional", account=PRIMARY, pm=False, max_retries=0):
    owner = copy.deepcopy(NCPL["owners"][-1]["payload"])
    owner["phase"] = phase
    row = owner["position_ids"][PRIMARY]
    record = FlipEntryOwnershipRecord(
        "NCPL", int(owner["opportunity_id"]), phase, int(owner["flip_bar_ts"]),
        int(owner["provisional_started_ms"]), (account,), {account: row},
        {account: int(owner["position_entry_ms"][PRIMARY])},
        int(owner["retry_segment_id"]), owner["retry_closes_at_place"],
    )
    return strategy_for_record(record, AT, pm=pm, max_retries=max_retries, quantities=(364, 182))


def strategy_for_record(record, at, *, pm=False, max_retries=0, quantities):
    """Current policy control over a recorded episode; no new bars or prints."""
    strategy = SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
        strategy_schwab_1m_v2_retry_one_enabled=True,
        strategy_schwab_1m_v2_retry_one_max_retries=max_retries,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
        strategy_schwab_1m_v2_removed_wait_clear_enabled=True,
        strategy_schwab_1m_v2_keep_rest_after_buy_enabled=True,
        strategy_schwab_1m_v2_account_name=PRIMARY,
        strategy_schwab_1m_v2_webull_account_name=WEBULL,
    ))
    clock = [int(at.timestamp() * 1000)]
    strategy._now_ms = lambda: clock[0]
    strategy.configure_fanout_identity_persistence(lambda *_: None)
    strategy.configure_flip_entry_ownership(
        lambda *_: None, restored={record.symbol: record}, active_segments={record.symbol: record.opportunity_id},
        retry_budget_persist=lambda *_: None,
        restored_retry_budgets={record.symbol: (record.retry_segment_id, int(record.phase == "consumed"))},
    )
    writes = []
    strategy.configure_removed_wait(lambda r, active: writes.append((r, active)),
                                    restored={}, readable=True)
    state = strategy.watchlist_state(record.symbol)
    state.fanout_segment_id = record.opportunity_id
    state.atr_short_flip_bar_ts = record.retry_segment_id
    state.resting_active = True
    state.resting_is_broker_order = not pm
    state.webull_resting_active = not pm
    state.resting_schwab_quantity, state.resting_webull_quantity = quantities
    state.cw_armed = pm
    return strategy, state, record, clock, writes


def close_book(strategy, state, record, clock, reason="CONFIRMATION_EXIT"):
    strategy.apply_flip_position_book(FlipPositionBook(
        clock[0], True, {}, closes_by_symbol={state.symbol: (
            FlipPositionClose(record.fill_accounts[0], next(iter(record.position_ids.values())), reason),
        )},
    ))


def test_recorded_ncpl_close_cancels_sibling_at_191018_before_next_bar(caplog):
    strategy, state, record, clock, writes = recorded()
    caplog.set_level("INFO")
    close_book(strategy, state, record, clock)
    primary, = strategy.drain_pending_intents()
    mirror, = strategy.drain_webull_direct_intents()
    assert primary.intent_type == mirror.intent_type == "cancel"
    assert primary.quantity == 364 and mirror.quantity == 182
    for draft in (primary, mirror):
        assert draft.metadata["reason"] == "retry_budget_exhausted"
        assert draft.metadata["fanout_segment_id"] == str(record.opportunity_id)
        assert draft.metadata["clearwait_buy_only"] == "true"
    sell = next(f for f in NCPL["fills"] if f["side"] == "sell")
    assert 0 <= (AT - datetime.fromisoformat(sell["filled_at"])).total_seconds() < 60
    assert not state.resting_active and not state.webull_resting_active
    assert state.flip_owner_phase == "consumed" and state.retry_one_closes_in_segment == 1
    assert "account=live:orb qty=182 reason=retry_budget_exhausted" in caplog.text
    assert writes[0][1] is True


def test_recorded_artl_close_cancels_schwab_at_first_owner_check_under_zero_retry_policy():
    # Historical owner journal is absent: reconstruct current policy, not historical consumption.
    assert ARTL["durable_owner_record"]["status"] == "UNMEASURED"
    row = ARTL["closed_managed_row"]
    at = datetime.fromisoformat(ARTL["historical_owner_transitions"]["close_followup_log_at"])
    entry_ms = int(datetime.fromisoformat(row["entry_time"]).timestamp() * 1000)
    cycle = ARTL["causal_sell_cycle"]["sell_flip_bar_ts_ms"]
    opportunity = int(ARTL["entry_opportunity"]["segment_id"])
    assert cycle != opportunity
    record = FlipEntryOwnershipRecord("ARTL", opportunity, "provisional", 0,
        entry_ms, (WEBULL,), {WEBULL: row["id"]}, {WEBULL: entry_ms}, cycle, 0)
    strategy, state, record, clock, _ = strategy_for_record(
        record, at, quantities=(2, 1))
    close_book(strategy, state, record, clock)
    primary, = strategy.drain_pending_intents()
    mirror, = strategy.drain_webull_direct_intents()
    assert primary.intent_type == mirror.intent_type == "cancel"
    assert primary.quantity == 2 and mirror.quantity == 1
    assert primary.metadata["fanout_slot_id"] == ARTL["entry_opportunity"]["slot_id"]
    assert primary.metadata["reason"] == "retry_budget_exhausted"
    close_at = datetime.fromisoformat(ARTL["causal_close"]["close_filled_at"])
    assert (at - close_at).total_seconds() == pytest.approx(3.647)
    assert at < datetime.fromisoformat(ARTL["causal_sell_cycle"]["next_bar_probe_log_utc"])
    assert at < datetime.fromisoformat(ARTL["sibling_status_evidence"]["broker_cancel_event_at"])
    assert state.flip_owner_phase == "consumed" and state.retry_one_closes_in_segment == 1


def test_recorded_apus_both_filled_before_primary_close_is_not_a_waiting_sibling():
    assert APUS["sibling_waiting_at_primary_close"] is False
    timeline = {key: datetime.fromisoformat(value) for key, value in APUS["timeline_utc"].items()}
    assert timeline["sibling_entry_fill"] < timeline["primary_close_fill"]
    primary = APUS["owner_managed_rows"]["primary"]
    sibling = APUS["owner_managed_rows"]["sibling"]
    entry_times = {account: int(datetime.fromisoformat(row["entry_time"]).timestamp() * 1000)
                   for account, row in ((PRIMARY, primary), (WEBULL, sibling))}
    # This is a routing control; historical retry-cycle ownership is not retained.
    record = FlipEntryOwnershipRecord("APUS", int(APUS["entry_opportunity"]["segment_id"]),
        "provisional", 0, entry_times[PRIMARY], (PRIMARY, WEBULL),
        {PRIMARY: primary["id"], WEBULL: sibling["id"]}, entry_times, 0, 0)
    strategy, state, record, clock, _ = strategy_for_record(
        record, timeline["primary_close_fill"], quantities=(2, 1))
    strategy.apply_flip_position_book(FlipPositionBook(clock[0], True,
        {"APUS": (FlipPositionLeg(WEBULL, sibling["id"], entry_times[WEBULL], 1),)},
        closes_by_symbol={"APUS": (FlipPositionClose(PRIMARY, primary["id"], "OCO_RESOLVED_FLAT"),)}))
    assert not strategy._removed_wait_requests
    assert strategy.drain_pending_intents() == strategy.drain_webull_direct_intents() == []
    clock[0] = int(timeline["sibling_manual_close_fill"].timestamp() * 1000)
    strategy.apply_flip_position_book(FlipPositionBook(clock[0], True, {},
        closes_by_symbol={"APUS": tuple(FlipPositionClose(account, row["id"], "OCO_RESOLVED_FLAT")
                           for account, row in ((PRIMARY, primary), (WEBULL, sibling)))}))
    assert not strategy._removed_wait_requests
    assert strategy.drain_pending_intents() == strategy.drain_webull_direct_intents() == []


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("reason", ["CONFIRMATION_EXIT", "OCO_RESOLVED_FLAT", "CW_HARD_STOP", "ATR_FLIP"])
def test_recorded_ncpl_close_routing_controls_both_sessions_and_exit_reasons(account, pm, reason):
    # Venue/session permutations of the recorded NCPL episode, not new market prints.
    strategy, state, record, clock, _ = recorded(account=account, pm=pm)
    close_book(strategy, state, record, clock, reason)
    assert len(strategy.drain_pending_intents()) == len(strategy.drain_webull_direct_intents()) == 1
    assert not state.cw_armed


def test_keeprest_flip_without_close_never_cancels_waiting_buy():
    from tests.unit.test_keeprest1 import seeded, track
    strategy, state, clock = seeded()
    track(strategy, state, flip="BUY")
    assert strategy.drain_pending_intents() == strategy.drain_webull_direct_intents() == []
    assert state.resting_active


def test_recorded_ncpl_no_close_proof_or_unreadable_book_does_not_cancel():
    strategy, state, _, clock, _ = recorded()
    for readable in (False, True):
        strategy.apply_flip_position_book(FlipPositionBook(clock[0], readable, {}))
    assert not strategy._removed_wait_requests
    assert strategy.drain_pending_intents() == strategy.drain_webull_direct_intents() == []


def test_recorded_ncpl_bound_without_durable_close_keeps_wait():
    strategy, state, _, clock, _ = recorded(phase="bound")
    strategy.apply_flip_position_book(FlipPositionBook(clock[0], True, {}))
    assert not strategy._removed_wait_requests
    assert state.resting_active and state.webull_resting_active


def test_recorded_ncpl_retry_allowed_does_not_cancel_sibling():
    strategy, state, record, clock, _ = recorded(max_retries=1)
    close_book(strategy, state, record, clock)
    assert strategy.drain_pending_intents() == strategy.drain_webull_direct_intents() == []
    assert state.flip_owner_phase == "idle"


def test_recorded_ncpl_bound_owner_kept_but_leftover_cancelled():
    strategy, state, record, clock, _ = recorded(phase="bound")
    close_book(strategy, state, record, clock, "CW_HARD_STOP")
    assert state.flip_owner_phase == "bound"
    assert len(strategy.drain_webull_direct_intents()) == 1


def test_recorded_ncpl_consumed_restart_emits_barrier_once():
    strategy, state, record, clock, _ = recorded(phase="consumed")
    close_book(strategy, state, record, clock)
    request = strategy._removed_wait_requests[state.symbol]
    restarted, restored_state, _, _, _ = recorded(phase="consumed")
    restarted.configure_removed_wait(lambda *_: None, restored={state.symbol: request}, readable=True)
    assert state.symbol not in restarted._removed_scanner_symbols
    assert len(restarted.drain_pending_intents()) == len(restarted.drain_webull_direct_intents()) == 1
    close_book(restarted, restored_state, record, clock)
    assert restarted.drain_webull_direct_intents() == []


def test_recorded_ncpl_both_filled_or_later_sell_segment_no_leftover_cancel():
    strategy, state, record, clock, _ = recorded()
    state.flip_owner_fill_accounts.add(WEBULL)
    close_book(strategy, state, record, clock)
    assert not strategy._removed_wait_requests
    strategy, state, record, clock, _ = recorded(phase="consumed")
    state.retry_one_segment_id += 60_000
    close_book(strategy, state, record, clock)
    assert not strategy._removed_wait_requests


def receipts(strategy, request, now, *, status="cancelled", account=WEBULL, orders=(), events=()):
    slot = next(iter(strategy._pending_intents)).metadata
    intents = [SimpleNamespace(
        id=a, intent_type="cancel", status=status if a == account else "cancelled",
        broker_account_id=a, created_at=now - timedelta(seconds=40),
        updated_at=now - timedelta(seconds=30),
        payload={"metadata": {**slot, "clearwait_removal_token": request.token}},
    ) for a in (PRIMARY, WEBULL)]
    return assess_removed_wait(request, now=now, accounts={a: a for a in (PRIMARY, WEBULL)},
                               intents=intents, orders=orders, filled_order_ids=set(), order_events=events,
                               snapshots=[SimpleNamespace(snapshot_type="v2_removed_wait",
                                       payload=request.payload(active=True))], has_position=False)


@pytest.mark.parametrize("status", ["accepted", "rejected", "failed"])
def test_retry_leftover_unknown_cancel_stays_blocking(status):
    strategy, state, record, clock, _ = recorded()
    close_book(strategy, state, record, clock)
    request = strategy._removed_wait_requests[state.symbol]
    now = AT + timedelta(seconds=40)
    assert not receipts(strategy, request, now, status=status).clear


@pytest.mark.parametrize("event_type,event_source,cancel_outcome,clear", [
    ("cancelled", "broker", "terminal_confirmed", True),
    ("rejected", "broker", "terminal_confirmed", False),
    ("cancelled", "client", "terminal_confirmed", False),
    ("cancelled", "broker", "could_not_tell", False),
    ("cancelled", "broker", "already_absent", False),
])
def test_recorded_ncpl_terminal_receipt_requires_matching_positive_broker_proof(
        event_type, event_source, cancel_outcome, clear):
    strategy, state, record, clock, _ = recorded()
    close_book(strategy, state, record, clock)
    request = strategy._removed_wait_requests[state.symbol]
    now = AT + timedelta(seconds=40)
    metadata = strategy._pending_intents[0].metadata
    orders = [SimpleNamespace(id=data["id"], intent_id=data["intent_id"],
        broker_order_id=data["broker_order_id"], status=data["status"],
        payload=metadata, updated_at=now - timedelta(seconds=30))
        for data in NCPL["orders"] if data["side"] == "buy"]
    mirror = next(row for row in orders if row.status == "cancelled")
    event = SimpleNamespace(order_id=mirror.id, event_type=event_type, event_source=event_source,
        event_at=now - timedelta(seconds=30), payload={"cancel_outcome": cancel_outcome})
    proof = receipts(strategy, request, now, orders=orders, events=[event])
    assert proof.clear is clear
    assert state.flip_owner_position_ids == record.position_ids


def test_retry_cancel_receipts_never_retire_owner_or_allow_second_entry():
    strategy, state, record, clock, writes = recorded()
    close_book(strategy, state, record, clock)
    request = strategy._removed_wait_requests[state.symbol]
    now = AT + timedelta(seconds=40)
    proof = receipts(strategy, request, now)
    assert proof.clear
    clock[0] = proof.observed_at_ms
    strategy.apply_removed_wait_proofs([proof])
    assert state.symbol not in strategy._removed_wait_requests
    assert state.flip_owner_phase == "consumed"
    assert state.flip_owner_position_ids == record.position_ids
    assert state.flip_owner_fill_accounts == set(record.fill_accounts)
    assert writes[-1] == (request, False)
    assert not strategy._strict_first_rest_admitted(state, slot="first")
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    close_book(strategy, state, record, clock)
    assert strategy.drain_webull_direct_intents() == []
    # Scanner-removal proof must not serve as a cancellation-only receipt.
    strategy._removed_wait_requests[state.symbol] = request
    strategy.apply_removed_wait_proofs([replace(proof, reason="terminal_unfilled_removed_wait")])
    assert state.symbol in strategy._removed_wait_requests


def test_retry_receipt_wrong_token_or_stale_keeps_barrier():
    strategy, state, record, clock, _ = recorded()
    close_book(strategy, state, record, clock)
    request = strategy._removed_wait_requests[state.symbol]
    strategy.apply_removed_wait_proofs([
        RemovedWaitProof(replace(request, token="stale"), clock[0], True, "retry_leftovers_cancelled_owner_kept"),
        RemovedWaitProof(request, clock[0] - 600_000, True, "retry_leftovers_cancelled_owner_kept"),
    ])
    assert state.symbol in strategy._removed_wait_requests


def test_recorded_ncpl_barrier_persists_and_restores_without_clearing_filled_owner():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from project_mai_tai.db.models import Base
    from project_mai_tai.v2_removed_wait import RemovedWaitStore
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    store = RemovedWaitStore(sessionmaker(engine))
    strategy, state, record, clock, _ = recorded()
    strategy.configure_removed_wait(store.record, restored={}, readable=True)
    close_book(strategy, state, record, clock)
    restored = store.restore()
    request = strategy._removed_wait_requests[state.symbol]
    assert restored == {state.symbol: request}
    assert request.purpose == "retry_exhausted"
    restarted, restored_state, _, _, _ = recorded(phase="consumed")
    restarted.configure_removed_wait(store.record, restored=restored, readable=True)
    assert len(restarted.drain_webull_direct_intents()) == 1
    assert restored_state.flip_owner_phase == "consumed"
    store.record(request, False)
    assert store.restore() == {}


def test_recorded_ncpl_cancel_barrier_selects_only_its_waiting_buy_not_protective_sell():
    from uuid import UUID, uuid4
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from project_mai_tai.db.models import Base, BrokerOrder
    from project_mai_tai.oms.store import OmsStore

    strategy, state, record, clock, _ = recorded()
    close_book(strategy, state, record, clock)
    primary, = strategy.drain_pending_intents()
    mirror, = strategy.drain_webull_direct_intents()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    strategy_id = uuid4()
    accounts = {PRIMARY: uuid4(), WEBULL: uuid4()}
    with sessions() as session:
        rows = {}
        for data in NCPL["orders"]:
            # Rewind only the recorded sibling lifecycle, not a market price.
            status = "accepted" if data["account"] == WEBULL else data["status"]
            row = BrokerOrder(id=UUID(data["id"]), strategy_id=strategy_id,
                broker_account_id=accounts[data["account"]], symbol=state.symbol,
                client_order_id=data["client_order_id"], broker_order_id=data["broker_order_id"],
                side=data["side"], quantity=data["quantity"], order_type="STOP_LIMIT",
                time_in_force="day", status=status, payload={"fanout_segment_id": data["segment"]})
            session.add(row)
            rows[data["id"]] = row
        session.flush()
        store = OmsStore()
        assert store.find_open_order_for_cancel(session, strategy_id=strategy_id,
            broker_account_id=accounts[PRIMARY], symbol=state.symbol, metadata=primary.metadata) is None
        target = store.find_open_order_for_cancel(session, strategy_id=strategy_id,
            broker_account_id=accounts[WEBULL], symbol=state.symbol, metadata=mirror.metadata)
        assert target is rows["4a802052-09ee-43df-8af3-88f4c1f3f25c"]
        target.status = "cancelled"  # Explicit simulated broker acknowledgement.
        assert store.find_open_order_for_cancel(session, strategy_id=strategy_id,
            broker_account_id=accounts[WEBULL], symbol=state.symbol, metadata=mirror.metadata) is None
        protective = rows["c7a05592-0340-490d-8146-0a6e862a4965"]
        protective.status = "accepted"  # Stronger control: even a live sell is not cancelled.
        assert store.find_open_order_for_cancel(session, strategy_id=strategy_id,
            broker_account_id=accounts[PRIMARY], symbol=state.symbol,
            metadata={**primary.metadata, "target_client_order_id": protective.client_order_id}) is None


def test_scanner_removal_payload_and_barrier_remain_legacy_unchanged():
    from project_mai_tai.v2_removed_wait import RemovedWait
    strategy, state, record, clock, _ = recorded()
    request = RemovedWait(state.symbol, record.opportunity_id, "scanner-token", clock[0], (PRIMARY, WEBULL))
    assert "purpose" not in request.payload(active=True)
    strategy._queue_removed_wait_barriers(state, request)
    primary, = strategy.drain_pending_intents()
    mirror, = strategy.drain_webull_direct_intents()
    assert primary.quantity == strategy._atr_qty
    assert mirror.quantity == strategy._webull_fanout_qty
    assert primary.reason == "scanner removal cancellation barrier"
    assert mirror.reason == "scanner removal cancellation barrier (webull)"
    assert primary.metadata["reason"] == mirror.metadata["reason"] == "watchlist-removed"


def test_cancel_barrier_blocks_new_segment_until_receipts_even_clearwait_flag_off():
    strategy, state, record, clock, _ = recorded()
    close_book(strategy, state, record, clock)
    strategy._removed_wait_enabled = False
    state.retry_one_segment_id += 60_000
    state.atr_short_flip_bar_ts = state.retry_one_segment_id
    state.retry_one_closes_in_segment = 0
    state.flip_owner_phase = "idle"
    assert strategy._removed_wait_gate_closed(state.symbol)
    assert not strategy._strict_first_rest_admitted(state, slot="first")


@pytest.mark.asyncio
async def test_retry_cancel_receipts_polled_off_thread_without_removed_wait_flag(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
    strategy, state, record, clock, _ = recorded()
    close_book(strategy, state, record, clock)
    request = strategy._removed_wait_requests[state.symbol]
    strategy._removed_wait_enabled = False
    proof = receipts(strategy, request, AT + timedelta(seconds=40))
    clock[0] = proof.observed_at_ms
    service = object.__new__(SchwabV2BotService)
    service.strategy, service.settings = strategy, strategy.settings
    service._clearwait_open_emits = {}
    store = SimpleNamespace(proofs=lambda *_: (proof,))
    service._removed_wait_store = store
    threaded = AsyncMock(return_value=(proof,))
    monkeypatch.setattr(asyncio, "to_thread", threaded)
    await service._removed_wait_poll()
    threaded.assert_awaited_once_with(store.proofs, (request,), {PRIMARY, WEBULL})
    assert state.symbol not in strategy._removed_wait_requests
    assert state.flip_owner_phase == "consumed"
