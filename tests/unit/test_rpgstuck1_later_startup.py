"""All 14 captured tickets plus the exact accounted Fill; no whole-day claim."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.atr_buy_readback import schwab_buy_readback
from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, Fill
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, _request, old_buy_proven_clear, replacement_terminal_zero, rpg_buy_owned
from tests.unit.test_rpg1_runtime import feedback
from tests.unit.test_rpgstuck1_startup import BROKER, real_bot_startup, real_oms_startup, startup_harness, tokenless_open

LATER = json.loads((Path(__file__).parents[1] / "fixtures/rpgstuck1_startup_later.json").read_text())
LOCAL = {"faa55c1f", "ee3d0d07", "ba108172", "4be7cb2d"}


@pytest.mark.asyncio
@pytest.mark.parametrize("outside_window", [False, True], ids=["current-window", "after-2000"])
async def test_later_all14_real_startup_actual_fill_and_mi_no_wire_no_stored_or_duplicate_buys(monkeypatch, outside_window):
    h = await startup_harness(monkeypatch, with_deferred=True, recorded=LATER)
    if outside_window:
        h.clock[0] = datetime.fromisoformat("2026-10-06T00:05:00+00:00")
    await real_bot_startup(monkeypatch, h)
    assert set(h.strategy._rpg_handoffs) == {row["id"] for row in LATER["tickets"]}
    assert len(h.strategy._rpg_handoffs) == 14
    assert h.strategy.watchlist_state("SCKT").cw_resting_taken
    with h.factory() as session:
        orders = list(session.scalars(select(BrokerOrder)))
        fills = list(session.scalars(select(Fill)))
        assert len(orders) == 11 and len(fills) == 1
        assert str(fills[0].id) == LATER["fills"][0]["id"]
        assert fills[0].quantity == Decimal(280) and fills[0].price == Decimal("1.06")
        before = (fills[0].id, fills[0].order_id, fills[0].quantity, fills[0].price, deepcopy(fills[0].payload))
    old = next(row["payload"]["old"] for row in LATER["tickets"] if row["id"].startswith("ff6464ff"))
    body = next(row["body"] for row in BROKER["orders"] if row["body"]["status"] == "REJECTED")
    h.adapter.override = schwab_buy_readback(_request(old), body)
    await real_oms_startup(monkeypatch, h)
    jobs = dict(HandoffJournal(h.factory).jobs())
    for token, job in jobs.items():
        if str(token)[:8] in LOCAL:
            assert job["phase"] == "refused"
            assert job["release_reason"] == "old_local_no_wire_return_to_strategy"
            assert job["local_no_wire"]
            state = h.strategy.watchlist_state(job["old"]["symbol"])
            await h.bot._rpg_handoff_pass()
            assert not h.strategy._rpg_entry_owned(state, account="live:orb")
            assert h.service._rpg_open_refusal(tokenless_open(state.symbol, "live:orb")) is None
        elif str(token).startswith("d86d5d38"):
            assert job["phase"] == "filled" and job["replacement_filled"]
        else:
            assert job["phase"] == "refused" and old_buy_proven_clear(job)
    # The current-window census is finished under a controlled hold; the late
    # startup uses its actual configured window, not a timeout or ticket purge.
    h.strategy._entries_held = not outside_window
    await feedback(h)
    await feedback(h)
    jobs = dict(HandoffJournal(h.factory).jobs())
    assert len(jobs) == 14
    for token, job in jobs.items():
        if str(token)[:8] in LOCAL:
            assert job["phase"] == "refused"
            assert job["release_reason"] == "old_local_no_wire_return_to_strategy"
        assert old_buy_proven_clear(job)
    assert not h.adapter.opens and not h.adapter.cancels and len(h.adapter.reads) == 1
    h.strategy._entries_held = False
    for symbol in ("APUS", "VEEA", "RETO", "MI", "SCKT"):
        state = h.strategy.watchlist_state(symbol)
        if symbol == "SCKT":
            # Both legacy terminal rows use the same recorded successor proof;
            # the later exact Fill still consumes its first-entry opportunity.
            terminal = [job for job in jobs.values() if job["old"]["symbol"] == symbol
                        and job.get("reason") == "replacement_terminal_accounted"]
            assert len(terminal) == 2 and all(replacement_terminal_zero(job) for job in terminal)
            assert h.strategy._rpg_entry_owned(state)
            h.strategy._cw_v2_resting_track(state, None)
        else:
            assert not h.strategy._rpg_entry_owned(state)
    assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()
    assert h.strategy.watchlist_state("SCKT").cw_resting_taken
    with h.factory() as session:
        fills = list(session.scalars(select(Fill)))
        assert len(fills) == 1
        fill = fills[0]
        assert (fill.id, fill.order_id, fill.quantity, fill.price, fill.payload) == before
        assert len(list(session.scalars(select(BrokerOrder)))) == 11


@pytest.mark.asyncio
async def test_later_recorded_sckt_fill_overrides_stale_placed_feedback_without_rebuy(monkeypatch):
    controlled = deepcopy(LATER)
    row = next(row for row in controlled["tickets"] if row["id"].startswith("d86d5d38"))
    row["payload"].update(phase="placed", replacement_filled=False, reason="CONTROLLED stale pre-fill feedback")
    h = await startup_harness(monkeypatch, with_deferred=True, recorded=controlled)
    await real_bot_startup(monkeypatch, h)
    job = HandoffJournal(h.factory).read(UUID(row["id"]))
    assert job["phase"] == "filled" and job["replacement_filled"]
    state = h.strategy.watchlist_state("SCKT")
    assert state.cw_resting_taken
    for _ in range(5):
        await h.bot._rpg_handoff_pass()
        h.strategy._cw_v2_resting_track(state, None)
        assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()
    with h.factory() as session:
        assert len(list(session.scalars(select(Fill)))) == 1
    assert not h.adapter.opens and not h.adapter.cancels


@pytest.mark.asyncio
@pytest.mark.parametrize("counterfactual", ["broker", "client_abort", "nfq"])
async def test_later_mi_unknown_label_never_proves_no_wire_or_releases_at_closed_window(monkeypatch, counterfactual):
    controlled = deepcopy(LATER)
    initial = next(row for row in controlled["deferred_intents"]
        if row["symbol"] == "MI" and row["payload"].get("refusal_origin") == "skipped_before_submit")
    if counterfactual == "nfq":
        initial["payload"]["refusal_code"] = "webull_mirror_fresh_price_wait"
    else:
        initial["payload"]["refusal_origin"] = counterfactual
    h = await startup_harness(monkeypatch, with_deferred=True, recorded=controlled)
    h.clock[0] = datetime.fromisoformat("2026-10-06T00:05:00+00:00")
    await real_bot_startup(monkeypatch, h)
    await real_oms_startup(monkeypatch, h)
    await feedback(h)
    token = UUID("4be7cb2d-406b-56db-b597-3783f6e956ff")
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "held_unknown" and not old_buy_proven_clear(job)
    assert h.strategy._rpg_entry_owned(h.strategy.watchlist_state("MI"), account="live:orb")
    assert not h.adapter.opens and not h.adapter.cancels


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["6fb89c93", "889889cd"])
async def test_sckt_legacy_same_phase_later_exact_proof_releases_only_zero_not_filled(monkeypatch, prefix):
    h = await startup_harness(monkeypatch, with_deferred=True, recorded=LATER)
    token = UUID(next(row["id"] for row in LATER["tickets"] if row["id"].startswith(prefix)))
    journal = HandoffJournal(h.factory)
    original = journal.read(token)
    with h.factory() as session:
        order = session.scalar(select(BrokerOrder).where(
            BrokerOrder.client_order_id == original["replacement"]["client_order_id"]))
        successor = session.scalar(select(DashboardSnapshot).where(
            DashboardSnapshot.payload["original_order_id"].as_string() == str(order.id)))
        successor_id, recorded_proof = successor.id, deepcopy(successor.payload)
        successor.payload = {**successor.payload, "clear_recorded": False}
        session.commit()
    await h.bot._rpg_handoff_pass()
    unknown = journal.read(token)
    assert unknown["phase"] == "refused" and not replacement_terminal_zero(unknown)
    assert rpg_buy_owned(unknown)
    state = h.strategy.watchlist_state("SCKT")
    assert state.cw_resting_taken  # Separate exact recorded Fill is still sticky.
    with h.factory() as session:
        successor = session.get(DashboardSnapshot, successor_id)
        successor.payload = {**recorded_proof, "revision": successor.payload["revision"] + 1}
        session.commit()
    await h.bot._rpg_handoff_pass()
    proven = journal.read(token)
    assert proven["phase"] == unknown["phase"] and proven["revision"] > unknown["revision"]
    assert replacement_terminal_zero(proven) and not rpg_buy_owned(proven)
    filled = next(job for key, job in journal.jobs() if str(key).startswith("d86d5d38"))
    assert rpg_buy_owned(filled) and not replacement_terminal_zero(filled)
    h.strategy._cw_v2_resting_track(state, None)
    assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()
    assert not h.adapter.opens and not h.adapter.cancels and not h.adapter.reads
