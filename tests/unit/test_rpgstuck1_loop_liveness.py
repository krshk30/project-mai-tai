"""Literal L5/L7 loop liveness; only retry-loop deliveries advance tickets."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import json
from uuid import UUID

import pytest

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, old_buy_proven_clear
from tests.unit.test_rpg1_runtime import begin, runtime
from tests.unit import test_rpgstuck1_all_on as all_on_tests
from tests.unit.test_rpgstuck1_all_on import AUTH_ROWS, all_on, census, price, stage, wire_adapters
from tests.unit.test_rpgstuck1_later_startup import LOCAL
from tests.unit.test_rpgstuck1_startup import real_bot_startup


@pytest.fixture
def fake_sdk(monkeypatch):
    return all_on_tests.fake_sdk.__wrapped__(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("progress", [False, True], ids=["four-tick-liveness", "then-loop-placed"])
async def test_l5_idle_real_retry_loop_actual_new_begin_wakes_four_ticks_in_four_seconds(monkeypatch, progress):
    h = await runtime(monkeypatch, "schwab", notional=600, strategy_overrides=all_on_tests.ALL_ON)
    all_on(h)
    journal = HandoffJournal(h.factory)
    stop = asyncio.Event()
    token, started = None, None
    ticks, scans, delays, ignored_bot_ticks = [], [], [], []
    original_scan = h.service._rpg_retry_jobs

    def scan(**kwargs):
        scans.append(kwargs["include_unknown"])
        return original_scan(**kwargs)

    async def pump(stop_event, seconds):
        nonlocal token, started
        delays.append(seconds)
        rows, h.service.redis.entries = h.service.redis.entries, []
        for _, data in rows:
            if data.get("event_type") != "atr_reprice_tick":
                continue
            ticks.append((data["token"], h.clock[0]))
            await h.service._handle_stream_message({"data": json.dumps(data)})
        if token is None:
            assert seconds is None and scans == [True] and not ticks and not journal.jobs()
            token, _ = await begin(h, "schwab")
            started = h.clock[0]
            assert journal.read(token)["phase"] == "clear"
        if h.clock[0] - started >= timedelta(seconds=5 if progress else 4):
            stop_event.set()
        else:
            h.clock[0] += timedelta(seconds=1)
            if progress and h.clock[0] - started == timedelta(seconds=5):
                price(h, h.state, "2.95")
                await h.bot._rpg_handoff_pass()
                ignored_bot_ticks.extend(data for _, data in h.service.redis.entries
                                         if data.get("event_type") == "atr_reprice_tick")
                h.service.redis.entries = []
        await asyncio.sleep(.1)

    monkeypatch.setattr(h.service, "_rpg_retry_jobs", scan)
    monkeypatch.setattr(h.service, "_rpg_retry_pause", pump)
    await h.service._run_rpg_retry_loop(stop)
    assert ticks[:4] == [(str(token), started + timedelta(seconds=i)) for i in range(1, 5)]
    assert len(ticks) == (5 if progress else 4)
    assert scans == [True] + [False] * len(ticks)
    assert delays == [None] + [1.0] * len(ticks)
    assert journal.read(token)["phase"] == ("placed" if progress else "clear"), journal.read(token).get("replacement_reasons")
    assert len(h.adapter.cancels) == len(h.adapter.reads) == 1
    assert len(h.adapter.opens) == int(progress)
    assert bool(ignored_bot_ticks) is progress


@pytest.mark.asyncio
@pytest.mark.parametrize("outside", [False, True], ids=["placed", "expired-after-2000"])
async def test_l7_recorded_four_local_unknown_jobs_real_loop_continues_after_clear(monkeypatch, outside):
    h = await census(monkeypatch)
    await real_bot_startup(monkeypatch, h)
    journal = HandoffJournal(h.factory)
    tokens = {token for token, _ in journal.jobs() if str(token)[:8] in LOCAL}
    assert len(tokens) == 4 and all(journal.read(token)["phase"] == "held_unknown" for token in tokens)
    stop = asyncio.Event()
    phases = {token: [] for token in tokens}
    ignored_bot_ticks, turns = [], []

    async def pump(stop_event, seconds):
        turns.append(seconds)
        rows, h.service.redis.entries = h.service.redis.entries, []
        for _, data in rows:
            if data.get("event_type") != "atr_reprice_tick":
                continue
            await h.service._handle_stream_message({"data": json.dumps(data)})
            token = UUID(data["token"])
            if token in tokens:
                phases[token].append(journal.read(token)["phase"])
        h.clock[0] += timedelta(seconds=1)
        if outside and len(turns) == 1:
            h.clock[0] = datetime.fromisoformat("2026-10-06T00:05:00+00:00")
        for state in h.strategy._symbol_states.values():
            price(h, state, "2.95")  # CONTROLLED eligible future tape.
        # Real strategy authorization is written, but its own xadd cannot rescue
        # a broken retry wake. Only deliveries captured above are ever handled.
        await h.bot._rpg_handoff_pass()
        ignored_bot_ticks.extend(data for _, data in h.service.redis.entries
                                 if data.get("event_type") == "atr_reprice_tick")
        h.service.redis.entries = []
        if len(turns) == 8:
            stop_event.set()
        await asyncio.sleep(.1)

    monkeypatch.setattr(h.service, "_rpg_retry_pause", pump)
    await h.service._run_rpg_retry_loop(stop)
    expected = "expired" if outside else "placed"
    # A committed acceptance may wake one same-generation proof evaluation;
    # it must not replay the opening or start a periodic terminal scan.
    for token in tokens:
        assert phases[token][:2] == ["clear", expected]
        assert phases[token][2:] in ([], ["placed"] if not outside else [])
    assert all(journal.read(token)["phase"] == expected and old_buy_proven_clear(journal.read(token))
               for token in tokens)
    assert len(h.adapter.opens) == (0 if outside else 4)
    assert len({request.metadata["fanout_slot_id"] for request in h.adapter.opens}) == len(h.adapter.opens)
    assert all(request.broker_account_name == "live:orb" for request in h.adapter.opens)
    assert ignored_bot_ticks and not h.adapter.cancels


@pytest.mark.asyncio
async def test_recorded_apus0932_distance_refusal_releases_then_later_same_segment_webull_places_once(
        monkeypatch, fake_sdk):
    row = next(row for row in AUTH_ROWS if row["id"].startswith("bd6ac0b9"))
    h, token, md = await stage(monkeypatch, row, row, market="4.78")
    wire_adapters(monkeypatch, h)
    from tests.unit.test_rpg1_runtime import feedback
    await feedback(h)
    journal = HandoffJournal(h.factory)
    refused = deepcopy(journal.read(token))
    assert refused["phase"] == "refused" and "webull_mirror_precheck_deferred" in refused["replacement_reasons"]
    assert md["stop_price"] == "5.2720" and not h.webull_client.calls.get("place", 0)
    await feedback(h)
    assert old_buy_proven_clear(refused) and not h.strategy._rpg_entry_owned(h.state, account="live:orb")
    segment = h.state.fanout_segment_id
    h.clock[0] += timedelta(minutes=1)
    price(h, h.state, "5.25")  # Distinct later CONTROLLED quote, never substituted into refusal.
    h.strategy._queue_resting_place(h.state, h.state.atr_trail, slot="first")
    mirror, = h.strategy.drain_webull_direct_intents()
    h.strategy.drain_pending_intents()  # This proof exercises only the released mirror leg.
    assert int(mirror.metadata["fanout_segment_id"]) == segment == refused["segment_id"]
    assert mirror.metadata["rpg_resting_generation"] != refused["old"]["metadata"]["rpg_resting_generation"]
    event = TradeIntentEvent(source_service="schwab-1m-v2", produced_at=h.clock[0],
        payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name="live:orb",
            symbol="APUS", side="buy", intent_type="open", quantity=mirror.quantity,
            reason=mirror.reason, metadata=mirror.metadata))
    for _ in range(4):
        await h.service._handle_stream_message({"data": event.model_dump_json()})
    assert h.webull_client.calls.get("place", 0) == 1 and not h.wires
    assert journal.read(token)["phase"] == "refused"
    assert journal.read(token)["authorization"] == refused["authorization"]
