import asyncio
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.momentum_live.actor import ActorPorts, LiveActor, OwnedObservation
from project_mai_tai.momentum_live.admission import EpisodeLedger, Refusal, Route
from project_mai_tai.momentum_live.lifecycle import OwnedPosition
from tests.unit.test_momlive1_admission import ledger, signal
from tests.unit.test_momlive1_oms_isolation import intent


def setup(*, filled=0, terminal=False, enabled=False, now=3_000_000_000):
    book = ledger()
    episode, _ = book.claim(signal(), now_ns=10)
    book.begin_submit(episode.episode_id, Route.SCHWAB, now_ns=100)
    position = None if not filled else OwnedPosition(episode.episode_id, "live:schwab_1m_v2", "SCNI",
        "owned-entry", "owned-broker-id", D(filled), D(3), 1_000_000_000)
    observation = OwnedObservation(D(filled), D(0), 1_000_000_000 if filled else None,
                                   terminal, False, position)
    ports = ActorPorts(lambda: enabled, lambda: now, AsyncMock(return_value=observation),
        AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), lambda symbol: {})
    entry = SimpleNamespace(admit=AsyncMock())
    actor = LiveActor(book, entry, ports, state_lock=asyncio.Lock())
    return actor, ports, episode, position


@pytest.mark.asyncio
async def test_restart_no_resubmit_unknown_entry_cancel_once_keeps_slot():
    actor, ports, episode, _ = setup()
    await actor.restore_before_serving()
    assert actor.restored
    actor.entry.admit.assert_not_awaited()
    ports.cancel_entry_once.assert_awaited_once_with(episode.episode_id)
    assert episode.cancel_requested and episode.state == "cancel_requested"
    assert episode.episode_id in actor.work.owners
    await actor._service_owned(episode.episode_id)
    ports.cancel_entry_once.assert_awaited_once()
    assert episode.episode_id in actor.work.owners


@pytest.mark.asyncio
async def test_partial_fill_protected_before_cancel_with_flag_off():
    actor, ports, episode, position = setup(filled=3)
    calls = []
    ports.protect_owned.side_effect = lambda owner: calls.append(("protect", owner.quantity))
    ports.cancel_entry_once.side_effect = lambda identity: calls.append(("cancel", identity))
    await actor.restore_before_serving()
    assert calls == [("protect", D(3)), ("cancel", episode.episode_id)]
    assert episode.filled == 3 and episode.exited == 0
    assert position.first_fill_ns == episode.first_fill_ns
    assert actor.dispatch.by_symbol == {"SCNI": (episode.episode_id,)}
    assert not actor.offer_intent(intent())


@pytest.mark.asyncio
async def test_authoritative_terminal_zero_entry_clears_owner_not_cancel_ack():
    actor, ports, episode, _ = setup(terminal=True)
    await actor.restore_before_serving()
    assert episode.state == "terminal_no_fill"
    assert actor.work.owners == set() and actor.dispatch.by_symbol == {}
    ports.cancel_entry_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_restart_held_identity_never_becomes_ready():
    actor, ports, episode, position = setup(filled=3)
    position.strategy = "schwab_1m_v2"
    with pytest.raises(Refusal, match="identity_mismatch"):
        await actor.restore_before_serving()
    assert not actor.restored
    assert episode.episode_id in actor.work.owners
    ports.protect_owned.assert_not_awaited()


@pytest.mark.asyncio
async def test_corrupt_missing_first_and_durable_send_protects_but_cannot_publish_ready():
    from dataclasses import replace
    actor, ports, episode, position = setup(filled=3, terminal=True)
    episode.first_submit_ns = None
    position.first_fill_ns = None
    ports.observe.return_value = replace(ports.observe.return_value, first_fill_ns=None)
    with pytest.raises(Refusal, match="owned_time_exit_anchor_UNKNOWN"):
        await actor.restore_before_serving()
    ports.protect_owned.assert_awaited_once_with(position)
    assert not actor.restored and episode.remaining == 3 and episode.episode_id in actor.work.owners


@pytest.mark.asyncio
async def test_broker_failure_after_restore_retains_exposure_and_worker_alive():
    actor, ports, episode, _ = setup(filled=3, terminal=True)
    await actor.restore_before_serving()
    ports.observe.side_effect = OSError("unavailable")
    actor._observations.clear()
    await actor._service_owned(episode.episode_id)
    assert episode.remaining == 3 and episode.episode_id in actor.work.owners
    ports.exit_owned.assert_not_awaited()


@pytest.mark.asyncio
async def test_earliest_first_fill_controls_time_exit_after_later_cumulative_report():
    actor, ports, episode, position = setup(filled=3, terminal=True, now=301_000_000_000)
    actor.book.reconcile(episode.episode_id, cumulative_filled=D(3), cumulative_exited=D(0),
        first_fill_ns=1_000_000_000, now_ns=2_000_000_000, terminal_entry=True)
    position.first_fill_ns = 11_000_000_000
    ports.observe.return_value = OwnedObservation(D(3), D(0), 11_000_000_000, True, False, position)
    ports.quote = lambda symbol: {"bid": D(3), "observed_ns": 301_000_000_000}
    await actor.restore_before_serving()
    ports.exit_owned.assert_awaited_once_with(position, "time_300s")
    assert position.first_fill_ns == 1_000_000_000


@pytest.mark.asyncio
async def test_stale_future_quotes_do_not_exit_held_owner():
    for stamp in (1, 302_000_000_000):
        actor, ports, episode, _ = setup(filled=3, terminal=True, now=301_000_000_000)
        ports.quote = lambda symbol: {"bid": D(4), "observed_ns": stamp}
        await actor.restore_before_serving()
        ports.exit_owned.assert_not_awaited()
        assert episode.remaining == 3


def test_offer_is_bounded_even_with_no_worker_and_off_blocks_only_entries():
    actor, _, _, _ = setup(enabled=True)
    actor.restored = True
    assert all(actor.offer_intent(intent()) for _ in range(64))
    assert not actor.offer_intent(intent())
    assert actor.inbox.qsize() == 64


@pytest.mark.asyncio
async def test_explicit_start_stop_owns_four_tasks_not_per_quote_tasks():
    actor, _, episode, _ = setup(filled=3, terminal=True)
    await actor.start()
    try:
        for _ in range(100):
            actor.dispatch.on_quote("SCNI")
        assert len(actor.tasks) == 4
        assert actor.work.queue.qsize() == 1
        assert actor.work.pending == {episode.episode_id}
    finally:
        await actor.close()
    assert actor.tasks == [] and not actor.restored


def test_real_exposure_restore_remains_independent_of_row_order():
    actor, _, episode, _ = setup(filled=3, terminal=True)
    book = EpisodeLedger.restore(actor.book.snapshot())
    assert episode.episode_id in book._active_by_symbol["SCNI"]


def test_closed_owner_queued_items_cannot_raise_from_new_owner_quote_callback():
    actor, _, _, _ = setup()
    actor.work.owners = {"closed-one", "closed-two"}
    assert actor.work.offer("closed-one") and actor.work.offer("closed-two")
    actor.work.owners = {"new-one", "new-two"}
    actor.dispatch.by_symbol = {"SCNI": ("new-one",)}
    actor.dispatch.on_quote("SCNI")
    assert actor.work.queue.qsize() == 2
    assert actor.work.pending == {"closed-one", "closed-two"}
    assert "new-one" not in actor.work.pending


@pytest.mark.asyncio
async def test_quote_coalescing_does_not_turn_into_broker_db_poll_storm():
    actor, ports, episode, _ = setup(filled=3, terminal=True)
    await actor.restore_before_serving()
    for _ in range(100):
        await actor._service_owned(episode.episode_id)
    assert ports.observe.await_count == 1
    assert ports.save.await_count == 1
    assert ports.protect_owned.await_count == 1
