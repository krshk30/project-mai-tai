"""Stop-10 restore controls: real local DB/OMS, controlled broker transports.

No target-replacement policy is exercised or imported. MOMORDER1's original
AM/RTH target-owner assertions and mandatory-exit contracts remain unchanged.
"""

from dataclasses import replace
from decimal import Decimal as D
from hashlib import sha256
import json
import logging

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, TradeIntent
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from tests.unit.test_momlive1_runtime import event
from tests.unit.test_momorder1_runtime_db import CASES, held_case, seed, utc


SESSION_CLOCK = {"AM": "2026-10-19T12:29:43.376Z",
    "NORMAL": "2026-10-19T14:00:00Z", "PM": "2026-10-19T21:00:00Z"}


def exits(h):
    return [row.request for row in h.venue.orders.values() if row.request.metadata.get("exit_reason")]


def quote(h, bid):
    h.service._latest_quotes_by_symbol["YMAT"] = {
        "bid": D(bid), "ask": D(bid) + D(".01"), "received_at": h.now[0]}


async def held_native(tmp_path, provider, session_name="NORMAL"):
    h = await seed(tmp_path, CASES[1], provider=provider, at=utc(SESSION_CLOCK[session_name]))
    h.trees, h.retirement_at_sell = {}, []
    submit, http = h.venue.submit_order, h.venue.http

    async def submit_with_tree(request):
        if request.metadata.get("exit_reason"):
            h.retirement_at_sell.append([(row.status, row.filled) for row in h.venue.orders.values()
                if row.request.side == "sell" and not row.request.metadata.get("exit_reason")
                and (session_name != "NORMAL" or row.request.client_order_id != h.handle.base_coid)])
        reports = await submit(request)
        if provider == "schwab" and request.metadata.get("exit_only_oco"):
            children = (request.client_order_id + "T", request.client_order_id + "S")
            h.trees[request.client_order_id] = children
            for coid in children:
                h.venue.put(replace(request, client_order_id=coid))
        return reports

    async def tree_http(method, path, *, body=None):
        coid = path.rsplit("/", 1)[-1].removeprefix("venue-")
        if coid not in h.trees:
            return await http(method, path, body=body)
        h.venue.calls.append(("read", coid))
        if h.venue.unknown:
            return 503, {}, {}
        children = []
        for child in h.trees[coid]:
            _, _, raw = await http("GET", "/orders/venue-" + child)
            raw["orderType"] = "LIMIT" if child.endswith("T") else "STOP"
            metadata = h.venue.orders[child].request.metadata
            raw["price" if child.endswith("T") else "stopPrice"] = metadata[
                "bracket_target_price" if child.endswith("T") else "bracket_stop_price"]
            children.append(raw)
        return 200, {}, {"orderId": "venue-" + coid, "orderStrategyType": "OCO",
            "childOrderStrategies": children}

    async def cancel_children(account, ids):
        assert account == h.handle.account
        for identity in ids:
            row = h.venue.orders[identity.removeprefix("venue-")]
            await h.venue.submit_order(replace(row.request, intent_type="cancel", quantity=D(0)))

    h.venue.submit_order = submit_with_tree
    h.venue.schwab._authorized_request_json = tree_http
    h.venue.cancel_exit_leg_ids = cancel_children
    _, h.handle = await held_case(h)
    assert h.handle.session == session_name
    h.native_coids = ((h.handle.base_coid,) if session_name != "NORMAL" else
        h.trees[h.handle.base_coid] if provider == "schwab" else
        h.venue.webull.exit_pair_leg_client_order_ids(h.handle.base_coid))
    return h


async def legacy_geometry(h):
    """Controlled pre-stop-10 beforeimage, never a live or source mutation."""
    def beforeimage(session):
        root = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == h.handle.base_coid))
        root.payload = {**root.payload, "bracket_stop_price": "1.70"}
        intent = session.get(TradeIntent, root.intent_id)
        intent.payload = {**intent.payload, "metadata": {
            **intent.payload["metadata"], "bracket_stop_price": "1.70"}}
    await h.service._run_db(beforeimage)
    for coid in (h.handle.base_coid, *h.native_coids):
        row = h.venue.orders[coid]
        row.request = replace(row.request, metadata={**row.request.metadata, "bracket_stop_price": "1.70"})


def guard_logs(caplog):
    # Retained boot-guard diagnostic name; it does not enable target replacement.
    return [r for r in caplog.records if r.levelno == logging.CRITICAL
        and "[MOMEXIT-B-RESTORE-GUARD]" in r.getMessage()]


def owner_proofs(h):
    return [json.loads(call.args[1]["data"]) for call in h.service.redis.xadd.await_args_list
        if str(call.args[0]).endswith(":momentum-live-owner-proof")]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize("unknown", [False, True], ids=["working", "possibly-working"])
async def test_restored_legacy_stop_logs_once_blocks_new_entries_but_owned_cleanup_and_flat_recovery_work(
    tmp_path, caplog, provider, unknown,
):
    h = await held_native(tmp_path, provider)
    restored = None
    try:
        await legacy_geometry(h)
        h.venue.unknown = unknown
        caplog.clear()
        caplog.set_level(logging.CRITICAL)
        restored = OmsLiveRuntime(h.service, clock=lambda: h.now[0], identity="701:201")
        assert await restored.start()
        assert restored.actor.restored and len(restored.actor.tasks) == 4
        assert restored.native.handles[h.identity].base_coid == h.handle.base_coid
        for _ in range(3):
            assert not restored._entry_enabled()
            assert not restored.actor.offer_intent(event(h.now[0], route=provider))
        assert len(guard_logs(caplog)) == 1
        assert owner_proofs(h)[-1]["entry_admission_enabled"] is False
        assert owner_proofs(h)[-1]["episodes"]
        assert exits(h) == [] and not [c for c in h.venue.calls if c[0] == "cancel"]
        await restored.close()
        h.runtime = restored
        h.handle = restored.native.handles[h.identity]
        h.venue.unknown = False
        quote(h, "1.99")
        position = (await restored._observe(h.identity)).position
        await restored._exit(position, "time_300s")
        assert len(exits(h)) == 1 and exits(h)[0].quantity == 50
        assert exits(h)[0].metadata["exit_reason"] == "time_300s"
        assert not restored._entry_enabled()
        assert h.venue.orders[h.handle.base_coid].request.metadata["bracket_stop_price"] == "1.70"
        software = exits(h)[0]
        h.venue.put(software, "filled", D(50), D("1.99"), h.now[0])
        await restored._record(software, [h.venue.report(software)])
        observed = await restored._observe(h.identity)
        assert observed.quantity == observed.exited == 50
        assert not observed.outstanding_exit and restored.native.handles[h.identity].status == "terminal"
        assert restored._entry_enabled() and len(guard_logs(caplog)) == 1
    finally:
        if restored is not None:
            await restored.close()
        h.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["webull", "schwab"])
async def test_restored_current_ten_percent_stop_starts_normally_without_guard_or_replacement(tmp_path, caplog, provider):
    h = await held_native(tmp_path, provider)
    restored = None
    try:
        root, _ = await h.runtime.backend.root(h.handle, h.runtime)
        assert D(root["request"].metadata["bracket_stop_price"]) == D("1.80")
        caplog.clear()
        caplog.set_level(logging.CRITICAL)
        restored = OmsLiveRuntime(h.service, clock=lambda: h.now[0], identity="701:202")
        assert await restored.start()
        assert restored.actor.restored and len(restored.actor.tasks) == 4
        assert restored._entry_enabled() and restored.actor.ports.enabled()
        assert owner_proofs(h)[-1]["entry_admission_enabled"] is True
        assert guard_logs(caplog) == []
        assert restored.native.handles[h.identity].base_coid == h.handle.base_coid
        assert exits(h) == [] and not [c for c in h.venue.calls if c[0] == "cancel"]
    finally:
        if restored is not None:
            await restored.close()
        h.engine.dispose()


async def closed_legacy_beforeimage(h, *, orphan):
    await legacy_geometry(h)
    root = h.venue.orders[h.handle.base_coid].request
    durable_children = {}
    for coid in h.native_coids:
        if coid == h.native_coids[1]:
            h.venue.put(h.venue.orders[coid].request, "cancelled")
        broker_id = "venue-" + coid
        local_coid = (f"ml1-YMAT-n-{sha256(broker_id.encode()).hexdigest()[:16]}"
            if h.venue.provider == "schwab" else coid)
        request = replace(root, client_order_id=local_coid, metadata={
            "episode_id": h.identity, "momlive_strategy": root.strategy_code,
            "momlive_native_child": "true", "broker_order_id": broker_id})
        report = replace(h.venue.report(h.venue.orders[coid].request), client_order_id=local_coid)
        await h.runtime.backend._record_child(h.handle, h.runtime, report, request)
        durable_children[coid] = local_coid
    target, stop = h.native_coids
    h.venue.put(h.venue.orders[target].request, "filled", D(50), D("2.10"), h.now[0])
    h.venue.put(h.venue.orders[stop].request, "cancelled")
    read = await h.runtime.backend.read(h.handle, h.runtime, fresh=True)
    assert read.terminal and read.cumulative_exited == 50
    snapshot = await h.runtime._inspect(h.identity)
    assert snapshot["quantity"] == snapshot["exited"] == 50 and not snapshot["outstanding_exit"]
    h.runtime.book.reconcile(h.identity, cumulative_filled=D(50), cumulative_exited=D(50),
        terminal_entry=True, now_ns=h.runtime.now_ns())
    await h.runtime._save(h.runtime.book.snapshot()[0])
    h.handle.status = "working"  # Historical stale cache, not terminal authority.
    await h.runtime._save_handle(h.handle)
    if orphan:
        # Closed/flat labels cannot replace a missing child's terminal proof.
        def missing_terminal(session):
            row = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == durable_children[stop]))
            row.status = "accepted"
            row.payload = {k: v for k, v in row.payload.items() if k != "momlive_terminal_broker_event"}
        await h.service._run_db(missing_terminal)
        h.venue.put(h.venue.orders[stop].request)
    assert h.runtime.book.episodes[h.identity].state == "closed"
    quote(h, "2")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize("orphan", [False, True], ids=["all-native-terminal", "possibly-working-orphan"])
async def test_today_closed_flat_stale_working_handle_uses_local_terminal_ledger_not_active_actor(
    tmp_path, caplog, provider, orphan,
):
    h = await held_native(tmp_path, provider)
    restored = None
    try:
        await closed_legacy_beforeimage(h, orphan=orphan)
        before = list(h.venue.calls)
        caplog.clear()
        caplog.set_level(logging.CRITICAL)
        restored = OmsLiveRuntime(h.service, clock=lambda: h.now[0], identity="701:203")
        assert await restored.start()
        assert restored.actor.restored and restored.book.episodes[h.identity].state == "closed"
        assert restored.book._active_count == 0 and not restored.actor.work.owners
        assert restored._entry_enabled() is (not orphan)
        assert owner_proofs(h)[-1]["entry_admission_enabled"] is (not orphan)
        assert h.venue.calls == before
        assert exits(h) == []
        if orphan:
            assert len(guard_logs(caplog)) == 1
            assert not restored.actor.offer_intent(event(h.now[0], route=provider))
        else:
            assert len(guard_logs(caplog)) <= 1
    finally:
        if restored is not None:
            await restored.close()
        h.engine.dispose()
