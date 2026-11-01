"""Additional claim-contention proof; the original HOTFIX1 gate is untouched.

Broker execution is deliberately paused. This measures an actual held managed
owner's nonblocking refusal, NOT a broker lifecycle or a historical exit rate.
"""
import asyncio
import json
import threading
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from sqlalchemy import event as sql_event

from project_mai_tai.oms.service import _ManagedSellEvents
from tests.unit.managed_entry_fixtures import bind_managed_entry
from tests.unit import test_hotfix1_symbol_tick_cache as original

lane = original.lane


@asynccontextmanager
async def held_owner(lane, monkeypatch):
    service, _, factory, clock = lane
    service.settings = service.settings.model_copy(update={"bot_control_enabled": True})
    account, symbol = "live:orb", "CLAIM"
    with factory() as unit:
        service.store.ensure_strategy(unit, "schwab_1m_v2", name="ATR")
        service.store.ensure_broker_account(unit, account, provider="webull", environment="live")
        row = service.store.create_managed_position(unit, strategy_code="schwab_1m_v2",
            broker_account_name=account, symbol=symbol, entry_price=Decimal(6), quantity=10,
            entry_path="ATR Flip", entry_time=clock[0])
        bind_managed_entry(unit, row)
        position = service._hydrate_v2_position(row)
        identity = str(row.id)
        unit.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    counts = {"sender_entries": 0, "attempts": 0, "held_refusals": 0, "claim_sql": 0}
    loop_thread = threading.get_ident()
    probing = False

    async def paused_sender(*args, **kwargs):
        counts["sender_entries"] += 1
        assert counts["sender_entries"] == 1, "another seller entered beneath the held claim"
        entered.set()
        await release.wait()
        return _ManagedSellEvents(ownership_refused=True)

    monkeypatch.setattr(service, "_emit_v2_managed_sell", paused_sender)
    # Save the real decorated method: neither its decorator, lock nor body is mocked.
    claimed_emit = service._emit_v2_exit_on_loop_claimed

    async def invoke():
        return await claimed_emit(account, symbol, position, 6.0,
            kind="HARD", reference_price=6.0, reason="oms_v2_managed_exit:HARD_STOP",
            bid=6.0, close_on_fill=True, expected_managed_row_id=identity)

    holder = asyncio.create_task(invoke())
    key = (account, symbol, identity)
    def statement(*args):
        if probing and threading.get_ident() == loop_thread:
            counts["claim_sql"] += 1

    engine = factory.kw["bind"]
    attached = False
    try:
        # Setup must actually reach the existing sender AFTER its real row claim.
        # A fixture/guard mismatch fails here; no synthetic claim is inserted.
        await asyncio.wait_for(entered.wait(), 3)
        assert not holder.done()
        assert key in service._control_managed_exit_claims
        assert key[:2] in service._control_managed_exit_symbol_owners
        sql_event.listen(engine, "before_cursor_execute", statement)
        attached = True

        async def contend():
            nonlocal probing
            assert not holder.done()
            before = counts["claim_sql"]
            counts["attempts"] += 1
            probing = True
            try:
                outcome = await invoke()
            finally:
                probing = False
            assert outcome == "refused"
            assert counts["claim_sql"] == before, "held claim reached SQL before refusal"
            assert counts["sender_entries"] == 1
            counts["held_refusals"] += 1

        yield service, contend, counts, key
    finally:
        if attached:
            sql_event.remove(engine, "before_cursor_execute", statement)
        release.set()
        await holder
        assert key not in service._control_managed_exit_claims
        assert key[:2] not in service._control_managed_exit_symbol_owners


@pytest.mark.asyncio
async def test_actual_held_claim_refuses_without_sql_and_releases_its_owner(lane, monkeypatch):
    async with held_owner(lane, monkeypatch) as (_, contend, counts, _):
        for _ in range(8):
            await contend()
        assert counts == {"sender_entries": 1, "attempts": 8, "held_refusals": 8, "claim_sql": 0}


@pytest.mark.asyncio
async def test_probe_detects_missing_early_index_instead_of_accepting_a_fake_refusal(lane, monkeypatch):
    async with held_owner(lane, monkeypatch) as (service, contend, counts, key):
        # The real episode claim remains. Only its O(1) symbol index is removed:
        # the body may still refuse, but only AFTER a forbidden SQL row read.
        owner = service._control_managed_exit_symbol_owners.pop(key[:2])
        try:
            with pytest.raises(AssertionError, match="held claim reached SQL"):
                await contend()
            assert counts["claim_sql"] > 0 and counts["held_refusals"] == 0
        finally:
            service._control_managed_exit_symbol_owners[key[:2]] = owner


@pytest.mark.timing
@pytest.mark.asyncio
async def test_original_240_events_60_seconds_with_actual_c6_held_claim(lane, monkeypatch):
    async with held_owner(lane, monkeypatch) as (service, contend, counts, _):
        quote_handler, trade_handler = service._handle_quote_tick_event, service._handle_trade_tick_event
        callbacks = {"quote": 0, "trade": 0}

        async def quote(event):
            await quote_handler(event)
            callbacks["quote"] += 1
            await contend()

        async def trade(event):
            await trade_handler(event)
            callbacks["trade"] += 1
            await contend()

        monkeypatch.setattr(service, "_handle_quote_tick_event", quote)
        monkeypatch.setattr(service, "_handle_trade_tick_event", trade)
        # Run the original implementation and ALL its original assertions:
        # 240/s for >=60s; >=200/s, max loop stall<50ms, one blocked-retained DB
        # transaction, none after1s, hard-stop callback count, zero queued BUYs.
        await original.test_recorded_240_events_per_second_60_seconds_slow_db_flat_transactions(
            lane, monkeypatch, enabled=True)
        total = callbacks["quote"] + callbacks["trade"]
        assert callbacks["quote"] > 0 and callbacks["trade"] > 0
        assert counts["attempts"] == counts["held_refusals"] == total
        assert total >= 12_000
        assert counts["sender_entries"] == 1 and counts["claim_sql"] == 0
        print("KILLSW1_CLAIM_ACTIVE=" + json.dumps({
            "scope": "controlled held real managed claim per recorded quote/trade callback; no wire credit",
            **callbacks, **counts}, sort_keys=True))
