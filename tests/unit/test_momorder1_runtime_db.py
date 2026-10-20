"""Original AIXI/YMAT clocks with controlled broker replies and actual OMS DB writes.

These are offline replays, not a claim to reconstruct missing historical bids.
No actor background loop is started; each production port is awaited explicitly.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder, Fill, VirtualPosition
from project_mai_tai.momentum_live.admission import EpisodeLedger, Route, STRATEGY_CODE, Signal
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings


CASES = [
    ("AIXI", "f9e48766-f9ad-581d-b014-7068e23d5c83", 31, "3.11", "3.27",
     "2026-10-20T09:10:41.798306Z", "2026-10-20T09:10:42.128Z", "2026-10-20T09:11:09.571Z"),
    ("YMAT", "6c313663-c277-5b20-b650-c37ed7324feb", 50, "2.00", "2.10",
     "2026-10-19T12:29:42.654102Z", "2026-10-19T12:29:43.376Z", "2026-10-19T12:30:15.901Z"),
]


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Venue:
    """Only broker transport/results are synthetic; accounting and reads are production."""

    def __init__(self, now, provider, settings):
        self.now, self.provider = now, provider
        self.orders, self.calls = {}, []
        self.unknown = False
        self.fill_on_cancel = None
        self.cancel_error = False
        self.webull = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
        self.schwab = SchwabBrokerAdapter(settings, accounts_by_name={
            "live:schwab_1m_v2": SchwabAccountConfig(account_hash="controlled-account")})
        self.schwab._authorized_request_json = self.http

    def _adapter_for_account(self, account):
        return self.schwab if account == "live:schwab_1m_v2" else self.webull

    def put(self, request, status="accepted", filled=D(0), price=None, at=None):
        self.orders[request.client_order_id] = SimpleNamespace(request=request, status=status,
            filled=filled, price=price, at=at or self.now[0])

    def report(self, request):
        row = self.orders[request.client_order_id]
        return ExecutionReport(row.status, request.client_order_id,
            broker_order_id="venue-" + request.client_order_id,
            broker_fill_id=f"venue-{request.client_order_id}:{row.filled}" if row.filled else None,
            symbol=request.symbol, side=request.side, intent_type=request.intent_type,
            quantity=row.request.quantity, filled_quantity=row.filled, fill_price=row.price,
            origin="broker", reported_at=row.at,
            metadata={**request.metadata, "webull_broker_filled_time": row.at.isoformat()})

    async def fetch_order_update(self, request):
        self.calls.append(("read", request.client_order_id))
        return None if self.unknown else self.report(request)

    async def http(self, method, path, *, body=None):
        assert method == "GET"
        coid = path.rsplit("/", 1)[-1].removeprefix("venue-")
        self.calls.append(("read", coid))
        if self.unknown:
            return 503, {}, {}
        row = self.orders[coid]
        status = {"accepted": "WORKING", "cancelled": "CANCELED",
                  "partially_filled": "PARTIALLY_FILLED"}.get(row.status, row.status.upper())
        return 200, {}, {"orderId": "venue-" + coid, "status": status,
            "quantity": str(row.request.quantity), "filledQuantity": str(row.filled),
            "orderLegCollection": [{"legId": 1, "instruction": row.request.side.upper(),
                                     "instrument": {"symbol": row.request.symbol}}],
            "orderActivityCollection": [{"executionLegs": [{"legId": 1,
                "quantity": str(row.filled), "price": str(row.price), "time": row.at.isoformat()}]}]
                if row.filled else []}

    async def submit_order(self, request):
        self.calls.append(("cancel" if request.intent_type == "cancel" else "submit", request.client_order_id))
        if request.intent_type == "cancel":
            row = self.orders[request.client_order_id]
            if self.fill_on_cancel is not None:
                row.status, row.filled, row.price = "filled", row.request.quantity, self.fill_on_cancel
                row.at = self.now[0]
            else:
                row.status = "cancelled"
            if self.cancel_error:
                raise RuntimeError("417 ORDER_CAN_NOT_BE_CANCEL controlled response")
            return []  # ACK never asserts the original order's terminal state.
        self.put(request)
        if request.metadata.get("webull_exit_only_pair"):
            for coid in self.webull.exit_pair_leg_client_order_ids(request.client_order_id):
                self.put(replace(request, client_order_id=coid))
        return [self.report(request)]

    async def list_account_positions(self, account):
        entry = next(row for row in self.orders.values() if row.request.side == "buy")
        return [BrokerPositionSnapshot(account, entry.request.symbol, entry.filled,
            entry.price or D(0), as_of=self.now[0])]


async def seed(tmp_path, case=CASES[0], *, provider="webull", at=None):
    symbol, identity, shares, entry_price, target, sent_at, filled_at, exit_at = case
    now = [at or utc(sent_at)]
    settings = Settings(_env_file=None, momentum_live_enabled=True, oms_adapter="paper",
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_broker_provider="schwab",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'momorder1.db'}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    venue = Venue(now, provider, settings)
    service = OmsRiskService(settings, SimpleNamespace(xadd=AsyncMock()), session_factory=sf, broker_adapter=venue)
    service.seed_runtime_metadata()
    runtime = OmsLiveRuntime(service, clock=lambda: now[0], identity="controlled-momorder1")
    account = "live:orb" if provider == "webull" else "live:schwab_1m_v2"
    raw = {"episode_id": identity, "session_date": now[0].date().isoformat(), "symbol": symbol,
        "start_ms": 100, "end_ms": 200, "source_ids": ["controlled-recorded-identity"],
        "state": "claimed", "route": None, "first_submit_ns": None, "fallback_used": False,
        "cancel_requested": False, "filled": "0", "exited": "0", "first_fill_ns": None, "closed_ns": None}
    runtime.book = EpisodeLedger.restore([raw])
    signal = Signal(now[0].date().isoformat(), symbol, "controlled-recorded-identity", 100, 200,
        D(entry_price), runtime.now_ns(), 50000, country_route=provider)
    assert await runtime._claim(raw, account, provider, signal)
    runtime.book.begin_submit(identity, Route(provider), now_ns=runtime.now_ns())
    await runtime._save(runtime.book.snapshot()[0])
    coid = f"ml1-{symbol}-e-{identity.replace('-', '')[:20]}"
    request = OrderRequest(coid, account, STRATEGY_CODE, symbol, "buy", "open", D(shares),
        "MOMLIVE1_DETECTED", {"episode_id": identity, "momlive_strategy": STRATEGY_CODE,
            "momlive_one_shot": "true", "limit_price": entry_price, "session": "AM"}, "limit", "day")
    await runtime._prepare(identity, request, provider)
    venue.put(request)
    await runtime._record(request, [venue.report(request)])
    service._latest_quotes_by_symbol[symbol] = {"ask": D(entry_price), "bid": D(entry_price), "received_at": now[0]}
    return SimpleNamespace(now=now, venue=venue, service=service, runtime=runtime, sf=sf,
        identity=identity, request=request, case=case, engine=engine)


async def held_case(h):
    symbol, _, shares, price, _, _, filled_at, _ = h.case
    h.now[0] = max(h.now[0], utc(filled_at))
    h.venue.put(h.request, "filled", D(shares), D(price), h.now[0])
    await h.runtime._record(h.request, [h.venue.report(h.request)])
    h.runtime.book.reconcile(h.identity, cumulative_filled=D(shares), cumulative_exited=D(0),
        terminal_entry=True, now_ns=h.runtime.now_ns())
    await h.runtime._save(h.runtime.book.snapshot()[0])
    position = (await h.runtime._observe(h.identity)).position
    await h.runtime._protect(position)
    return position, h.runtime.native.handles[h.identity]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=["AIXI", "YMAT"])
@pytest.mark.parametrize("provider", ["webull", "schwab"])
async def test_recorded_entry_fill_before_two_second_deadline_prevents_cancel(tmp_path, case, provider):
    h = await seed(tmp_path, case, provider=provider)
    try:
        h.now[0] = utc(case[5]) + timedelta(seconds=2)
        h.venue.put(h.request, "filled", D(case[2]), D(case[3]), utc(case[6]))
        assert h.runtime.book.cancel_due(h.identity, now_ns=h.runtime.now_ns())
        await h.runtime._save(h.runtime.book.snapshot()[0])
        await h.runtime._cancel_entry(h.identity)
        assert h.venue.calls == [("read", h.request.client_order_id)]
        snapshot = await h.runtime._inspect(h.identity)
        assert snapshot["quantity"] == case[2] and snapshot["entry_terminal"]
        assert not h.runtime.runtime[h.identity].get("cancel_wire_sent_coid")
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize("unknown", [False, True])
async def test_unfilled_deadline_reads_then_cancels_once_and_rereads(tmp_path, provider, unknown):
    h = await seed(tmp_path, provider=provider)
    try:
        h.venue.unknown = unknown
        h.now[0] += timedelta(seconds=1, microseconds=999999)
        assert not h.runtime.book.cancel_due(h.identity, now_ns=h.runtime.now_ns())
        assert h.venue.calls == []
        h.now[0] += timedelta(microseconds=1)
        assert h.runtime.book.cancel_due(h.identity, now_ns=h.runtime.now_ns())
        await h.runtime._save(h.runtime.book.snapshot()[0])
        await h.runtime._cancel_entry(h.identity)
        assert h.venue.calls == [("read", h.request.client_order_id),
            ("cancel", h.request.client_order_id), ("read", h.request.client_order_id)]
        await h.runtime._cancel_entry(h.identity)
        assert sum(kind == "cancel" for kind, _ in h.venue.calls) == 1
        assert h.runtime.runtime[h.identity]["cancel_wire_sent_coid"] == h.request.client_order_id
        assert (await h.runtime._inspect(h.identity))["quantity"] == 0
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["webull", "schwab"])
async def test_entry_cancel_417_followed_by_fresh_full_fill_is_booked_once(tmp_path, provider):
    h = await seed(tmp_path, provider=provider)
    try:
        h.now[0] += timedelta(seconds=2)
        h.venue.fill_on_cancel, h.venue.cancel_error = D("3.11"), True
        assert h.runtime.book.cancel_due(h.identity, now_ns=h.runtime.now_ns())
        await h.runtime._save(h.runtime.book.snapshot()[0])
        await h.runtime._cancel_entry(h.identity)
        await h.runtime._cancel_entry(h.identity)
        assert [kind for kind, _ in h.venue.calls] == ["read", "cancel", "read"]
        with h.sf() as session:
            row = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == h.request.client_order_id))
            fills = session.scalars(select(Fill).where(Fill.order_id == row.id)).all()
            assert row.status == "filled" and sum(f.quantity for f in fills) == 31 and len(fills) == 1
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=["AIXI", "YMAT"])
async def test_actual_am_single_root_fill_remains_filled_on_repeated_backend_reads(tmp_path, case):
    h = await seed(tmp_path, case)
    try:
        _, handle = await held_case(h)
        h.now[0] = utc(case[7]) + timedelta(microseconds=223000)
        root_request = h.venue.orders[handle.base_coid].request
        h.venue.put(root_request, "filled", D(case[2]), D(case[4]), utc(case[7]))
        for _ in range(3):
            read = await h.runtime.backend.read(handle, h.runtime)
            assert read.terminal and read.cumulative_exited == case[2]
        with h.sf() as session:
            root = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == handle.base_coid))
            fills = session.scalars(select(Fill).where(Fill.order_id == root.id)).all()
            assert root.status == "filled"
            assert len(fills) == 1 and fills[0].quantity == case[2] and fills[0].price == D(case[4])
            vp = session.scalar(select(VirtualPosition).where(VirtualPosition.strategy_id == root.strategy_id,
                VirtualPosition.broker_account_id == root.broker_account_id, VirtualPosition.symbol == case[0]))
            assert vp.quantity == 0 and vp.realized_pnl == D(case[2]) * (D(case[4]) - D(case[3]))
        assert not [coid for kind, coid in h.venue.calls if kind == "cancel"]
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("later", ["cancelled", "rejected", "expired"])
async def test_late_broker_terminal_cannot_overwrite_a_full_execution(tmp_path, later):
    h = await seed(tmp_path)
    try:
        _, handle = await held_case(h)
        h.now[0] = utc(CASES[0][7])
        request = h.venue.orders[handle.base_coid].request
        h.venue.put(request, "filled", D(31), D("3.27"), h.now[0])
        full = h.venue.report(request)
        await h.runtime._record(request, [full])
        await h.runtime._record(request, [replace(full, event_type=later, filled_quantity=D(0), fill_price=None)])
        with h.sf() as session:
            row = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
            fills = session.scalars(select(Fill).where(Fill.order_id == row.id)).all()
            assert row.status == "filled" and len(fills) == 1 and fills[0].quantity == 31
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
async def test_partial_execution_and_cancelled_remainder_still_stays_cancelled(tmp_path):
    h = await seed(tmp_path)
    try:
        _, handle = await held_case(h)
        request = h.venue.orders[handle.base_coid].request
        h.venue.put(request, "cancelled", D(7), D("3.27"))
        read = await h.runtime.backend.read(handle, h.runtime)
        assert read.terminal and read.cumulative_exited == 7
        with h.sf() as session:
            row = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
            fills = session.scalars(select(Fill).where(Fill.order_id == row.id)).all()
            assert row.status == "cancelled" and len(fills) == 1 and fills[0].quantity == 7
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
async def test_normal_synthetic_root_releases_reservation_without_fabricating_root_fill(tmp_path):
    h = await seed(tmp_path, at=datetime(2026, 10, 20, 14, tzinfo=UTC))
    try:
        _, handle = await held_case(h)
        assert handle.session == "NORMAL" and len(handle.child_ids) == 2
        target, stop = handle.child_ids
        target_request, stop_request = h.venue.orders[target].request, h.venue.orders[stop].request
        h.venue.put(target_request, "filled", D(31), D("3.27"))
        h.venue.put(stop_request, "cancelled")
        read = await h.runtime.backend.read(handle, h.runtime)
        assert read.terminal and read.cumulative_exited == 31
        with h.sf() as session:
            root = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == handle.base_coid))
            child = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == target))
            assert root.status == "cancelled" and child.status == "filled"
            assert session.scalars(select(Fill).where(Fill.order_id == root.id)).all() == []
            fills = session.scalars(select(Fill).where(Fill.order_id == child.id)).all()
            assert len(fills) == 1 and fills[0].quantity == 31
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["hard_stop_15pct", "time_300s", "KILLSW1_STOP"])
async def test_persisted_old_target_latch_cannot_mask_protective_exit(tmp_path, monkeypatch, reason):
    h = await seed(tmp_path)
    try:
        position, handle = await held_case(h)
        h.runtime.runtime[h.identity]["soft_exit_reason"] = "target_5pct"
        position.pending_exit_reason = "target_5pct"
        if reason == "time_300s":
            h.now[0] += timedelta(seconds=301)
        h.service._latest_quotes_by_symbol["AIXI"] = {
            "ask": D("2.64"), "bid": D("2.63"), "received_at": h.now[0]}
        if reason == "KILLSW1_STOP":
            # Exercise promotion/release but retain the real STOP receipt gate.
            monkeypatch.setattr(h.runtime, "_control_receipt_ready", AsyncMock(return_value=False))
        await h.runtime._exit(position, reason)
        assert h.runtime.runtime[h.identity]["soft_exit_reason"] == reason
        assert [(kind, coid) for kind, coid in h.venue.calls if kind == "cancel"] == [("cancel", handle.base_coid)]
        exits = [row for row in h.venue.orders.values() if row.request.metadata.get("exit_reason")]
        if reason == "KILLSW1_STOP":
            assert exits == []  # No position-receipt waiver or pretend STOP completion.
        else:
            assert len(exits) == 1 and exits[0].request.quantity == 31
            assert exits[0].request.metadata["exit_reason"] == reason
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
async def test_legacy_pm_handle_at_nine_fifteen_cannot_enable_am_software_target(tmp_path):
    h = await seed(tmp_path, at=datetime(2026, 10, 20, 13, 15, tzinfo=UTC))
    try:
        position, handle = await held_case(h)
        handle.session = "PM"  # Actual legacy wire value from the old hour<9 classifier.
        await h.runtime._save_handle(handle)
        h.runtime.runtime[h.identity]["soft_exit_reason"] = "target_5pct"
        position = (await h.runtime._observe(h.identity)).position
        assert position.market_session == "AM" and handle.session == "PM"
        assert position.exit_reason(D("3.27"), quote_ns=h.runtime.now_ns(), now_ns=h.runtime.now_ns()) is None
        assert not [coid for kind, coid in h.venue.calls if kind == "cancel"]
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("hour,minute,session_name", [(12, 59, "AM"), (13, 0, "AM"),
    (13, 29, "AM"), (13, 30, "NORMAL"), (20, 0, "PM")])
async def test_nine_to_nine_twenty_nine_is_am_not_pm(tmp_path, monkeypatch, hour, minute, session_name):
    h = await seed(tmp_path, at=datetime(2026, 10, 20, hour, minute, tzinfo=UTC))
    try:
        h.venue.put(h.request, "filled", D(31), D("3.11"))
        await h.runtime._record(h.request, [h.venue.report(h.request)])
        position = (await h.runtime._observe(h.identity)).position
        ensure = AsyncMock()
        monkeypatch.setattr(h.runtime.native, "ensure", ensure)
        await h.runtime._protect(position)
        assert ensure.await_args.kwargs["session"] == session_name
    finally:
        h.engine.dispose()
