"""Controlled terminal-fill readback; no CRMT native payload is claimed here."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.db.models import BrokerOrder, Fill, TradeIntent
from project_mai_tai.momentum_live.admission import STRATEGY_CODE
from project_mai_tai.momentum_live.ledger import load_live_ledger
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from tests.unit.test_momorder1_runtime_db import seed


IDENTITY = "7c9460f7-25cf-59a4-a4b8-8199c8c34001"  # Controlled full UUID; only reported prefix is retained.
CRMT = ("CRMT", IDENTITY, 111, "1.00", "1.05",
        "2026-10-20T19:05:27Z", "2026-10-20T19:05:28Z", "2026-10-20T19:10:28Z")


async def terminal_case(tmp_path, provider, side, hour=14):
    # Oct19 session controls cannot accidentally inject future broker times.
    now = datetime(2026, 10, 19, hour, 10, 30, tzinfo=UTC)
    h = await seed(tmp_path, CRMT, provider=provider, at=now)
    execution = now-timedelta(seconds=2)
    h.venue.put(h.request, "filled", D(111), D("1.00"), execution-timedelta(minutes=5))
    entry_report = h.venue.report(h.request)
    await h.runtime._record(h.request, [entry_report])
    request = h.request
    if side == "sell":
        session = "AM" if hour < 13 else "PM" if hour >= 20 else "NORMAL"
        request = OrderRequest(f"ml1-CRMT-x-{IDENTITY.replace('-', '')[:20]}", h.request.broker_account_name,
            STRATEGY_CODE, "CRMT", "sell", "close", D(111), "time_300s",
            {"episode_id": IDENTITY, "momlive_strategy": STRATEGY_CODE, "momlive_one_shot": "true",
             "exit_reason": "time_300s", "clock_source": "first_execution", "session": session,
             "limit_price": "0.9151"}, "limit", "day")
        await h.runtime._prepare(IDENTITY, request, provider)
        h.venue.put(request, "filled", D(111), D("0.9151"), execution)
        # Instant-filled submit shape: actual broker-origin report, but no
        # momentum execution-time marker. reported_at alone is not trusted.
        await h.runtime._record(request, [h.venue.report(request)])
    snapshot = await h.runtime._inspect(IDENTITY)
    row = next(item for item in snapshot["orders"]
               if item["request"].client_order_id == request.client_order_id)
    return h, row


def stamp_key(side):
    return "momlive_first_fill_ns" if side == "buy" else "momlive_last_exit_fill_ns"


def durable(h, coid):
    with h.sf() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == coid))
        fills = session.scalars(select(Fill).where(Fill.order_id == order.id)).all()
        parent = session.get(TradeIntent, UUID(IDENTITY))
        return dict(order.payload), [(str(f.id), f.quantity, f.price) for f in fills], dict(parent.payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour", [8, 14, 21], ids=["AM", "NORMAL", "PM"])
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_terminal_missing_stamp_reads_once_via_actual_provider_validator(tmp_path, monkeypatch, provider, hour, side):
    h, row = await terminal_case(tmp_path, provider, side, hour)
    try:
        coid, key = row["request"].client_order_id, stamp_key(side)
        before, fills, _ = durable(h, coid)
        assert before["momlive_terminal_broker_event"] == "filled" and key not in before
        fetch = h.runtime.backend.fetch_entry_update
        reads = []

        async def checked(request, runtime, *, fresh=False):
            assert fresh is True and request.client_order_id == coid and request.intent_type != "cancel"
            # A separate session sees the once marker BEFORE any broker await.
            _, _, parent = durable(h, coid)
            assert parent["momlive_runtime"]["execution_time_readback_coids"].count(coid) == 1
            reads.append(request)
            return await fetch(request, runtime, fresh=fresh)

        monkeypatch.setattr(h.runtime.backend, "fetch_entry_update", checked)
        wire_before = [call for call in h.venue.calls if call[0] in {"submit", "cancel"}]
        await h.runtime._read_missing_execution_time(IDENTITY, row)
        await h.runtime._read_missing_execution_time(IDENTITY, row)  # Stale snapshot must not re-read.
        after, after_fills, _ = durable(h, coid)
        assert len(reads) == 1 and after_fills == fills
        assert [call for call in h.venue.calls if call[0] in {"submit", "cancel"}] == wire_before
        if side == "buy" and provider == "webull":
            # Existing Webull LAST-only evidence cannot prove FIRST. Do not
            # invent a first execution time merely to clear the page.
            assert key not in after
        else:
            assert int(after[key]) == int(h.venue.orders[coid].at.timestamp()*1e9)
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_existing_trusted_stamp_adds_zero_reads_and_preserves_fill(tmp_path, monkeypatch, provider, side):
    h, row = await terminal_case(tmp_path, provider, side)
    try:
        coid, key = row["request"].client_order_id, stamp_key(side)
        stamp = str(int(h.venue.orders[coid].at.timestamp()*1e9))
        with h.sf.begin() as session:
            order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == coid))
            order.payload = {**order.payload, key: stamp}
        row = {**row, "request": replace(row["request"], metadata={**row["request"].metadata, key: stamp})}

        async def forbidden(*args, **kwargs):
            raise AssertionError("already-stamped fill must not fetch again")

        monkeypatch.setattr(h.runtime.backend, "fetch_entry_update", forbidden)
        before = durable(h, coid)
        await h.runtime._read_missing_execution_time(IDENTITY, row)
        await h.runtime._read_missing_execution_time(IDENTITY, row)
        assert durable(h, coid) == before
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_actual_observe_reaches_terminal_instant_fill_readback(tmp_path, monkeypatch, side):
    h, row = await terminal_case(tmp_path, "schwab", side)
    try:
        if side == "sell":
            # Keep this assertion about the exit hook, not an unrelated entry read.
            entry = h.venue.orders[h.request.client_order_id]
            await h.runtime._record(h.request, [replace(h.venue.report(h.request), metadata={
                **entry.request.metadata, "momlive_execution_time_source": "broker_execution_time"})])
        reads, fetch = [], h.runtime.backend.fetch_entry_update

        async def checked(request, runtime, *, fresh=False):
            reads.append((request.client_order_id, fresh))
            return await fetch(request, runtime, fresh=fresh)

        monkeypatch.setattr(h.runtime.backend, "fetch_entry_update", checked)
        await h.runtime._observe(IDENTITY)
        await h.runtime._observe(IDENTITY)
        coid = row["request"].client_order_id
        assert reads == [(coid, True)]
        assert int(durable(h, coid)[0][stamp_key(side)]) == int(h.venue.orders[coid].at.timestamp()*1e9)
        assert not [call for call in h.venue.calls if call[0] in {"submit", "cancel"}]
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_time", "foreign_identity", "missing_body"])
async def test_missing_or_unbound_native_time_stays_unknown_once_and_survives_restore(tmp_path, monkeypatch, fault):
    h, row = await terminal_case(tmp_path, "schwab", "sell")
    try:
        coid = row["request"].client_order_id
        http = h.venue.schwab._authorized_request_json

        async def damaged(method, path, *, body=None):
            code, headers, raw = await http(method, path, body=body)
            if fault == "missing_body":
                return 200, {}, None
            if fault == "foreign_identity":
                raw["orderId"] = "foreign-order"
            else:
                for activity in raw["orderActivityCollection"]:
                    for execution in activity["executionLegs"]:
                        execution.pop("time")
                # Valid closeTime must not be silently substituted for execution.
                raw["closeTime"] = h.now[0].isoformat()
            return code, headers, raw

        monkeypatch.setattr(h.venue.schwab, "_authorized_request_json", damaged)
        before = durable(h, coid)[1]
        await h.runtime._read_missing_execution_time(IDENTITY, row)
        after, fills, parent = durable(h, coid)
        assert "momlive_last_exit_fill_ns" not in after and fills == before
        assert parent["momlive_runtime"]["execution_time_readback_coids"] == [coid]
        result = load_live_ledger(h.sf, h.now[0].date())
        assert result["status"] == "PARTIAL" and result["daily_pnl_partial"] is True
        assert any(item["episode_id"] == IDENTITY and item["exit_time"] == item["pnl"] == "UNKNOWN"
                   for item in result["unknown_episodes"])
        # Fresh runtime objects restored from durable data, no start/background jobs.
        restored = OmsLiveRuntime(h.service, clock=lambda: h.now[0], identity="controlled-readback-restore")
        with h.sf() as session:
            bundle = restored.db.restore_bundle(session, h.now[0].replace(hour=0, minute=0, second=0))
        from project_mai_tai.momentum_live.admission import EpisodeLedger
        restored.book = EpisodeLedger.restore(bundle["episodes"])
        restored.runtime.update(bundle["runtime"])
        restored.bindings.update(bundle["bindings"])

        async def forbidden(*args, **kwargs):
            raise AssertionError("restart must retain prior unavailable read attempt")

        monkeypatch.setattr(restored.backend, "fetch_entry_update", forbidden)
        await restored._read_missing_execution_time(IDENTITY, row)
        assert durable(h, coid)[1] == before
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
async def test_crmt_shaped_submit_filled_real_exit_hook_repairs_stamp_without_second_sell(tmp_path, monkeypatch):
    h, _ = await terminal_case(tmp_path, "schwab", "buy")
    try:
        # Isolate the exit readback: the entry already has trusted execution evidence.
        entry = h.request
        await h.runtime._record(entry, [replace(h.venue.report(entry), metadata={
            **entry.metadata, "momlive_execution_time_source": "broker_execution_time"})])
        position = (await h.runtime._observe(IDENTITY)).position
        submit = h.venue.submit_order
        close_coids = []

        async def instant(request):
            reports = await submit(request)
            if request.side == "sell" and request.intent_type == "close":
                close_coids.append(request.client_order_id)
                h.venue.put(request, "filled", request.quantity, D("0.9151"), h.now[0]-timedelta(seconds=1))
                return [*reports, h.venue.report(request)]
            return reports

        monkeypatch.setattr(h.venue, "submit_order", instant)
        h.service._latest_quotes_by_symbol["CRMT"] = {
            "bid": D("0.9151"), "ask": D("0.9152"), "received_at": h.now[0]}
        await h.runtime._exit(position, "time_300s")
        assert len(close_coids) == 1
        metadata, fills, _ = durable(h, close_coids[0])
        assert metadata["momlive_terminal_broker_event"] == "filled"
        assert int(metadata["momlive_last_exit_fill_ns"]) == int((h.now[0]-timedelta(seconds=1)).timestamp()*1e9)
        assert sum(amount for _, amount, _ in fills) == 111
        assert not [call for call in h.venue.calls if call[0] == "cancel"]
    finally:
        h.engine.dispose()
