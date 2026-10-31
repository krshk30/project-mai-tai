"""Controlled venue responses, not a reproduction of the historical XLAB order."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import logging
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.broker_adapters import schwab as schwab_module
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, TradeIntent
from project_mai_tai.broker_adapters.momentum_order_read import MomentumEntryLookup
from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget
from project_mai_tai.momentum_live.admission import EpisodeLedger, Refusal, Route, STRATEGY_CODE, Signal
from project_mai_tai.momentum_live.execution import ExecutionPorts, LiveEntryExecutor
from project_mai_tai.momentum_live.entry_reprice import UnsentEntryConflict
from project_mai_tai.momentum_live.actor import ActorPorts, LiveActor
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.positions_read_receipt import record_receipts
from project_mai_tai.settings import Settings
from tests.unit.test_control_plane import build_test_session_factory
from tests.unit.test_webull_adapter import _adapter as webull_adapter, _FakeClient, _ServerException, fake_sdk as fake_sdk


@pytest.fixture(autouse=True)
def capture_service_log_records(monkeypatch, caplog):
    # Service setup replaces root handlers; retain capture on the real logger.
    logger = logging.getLogger("oms-risk")
    monkeypatch.setattr(logger, "handlers", [*logger.handlers, caplog.handler])


class ControlledSchwabWire:
    """Only the HTTP boundary is replaced; adapter and MOM persistence are real."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.status = "WORKING"
        self.filled = D(0)
        self.fill_on_delete = False
        self.place_status = 201
        self.headers = {"Location": "https://api.schwabapi.com/trader/v1/accounts/hash-k/orders/12345"}
        self.body = {}
        self.quantity = D(28)

    def raw(self):
        stamp = self.clock().isoformat()
        return {"orderId": "12345", "status": self.status, "quantity": str(self.quantity),
                "filledQuantity": str(self.filled), "enteredTime": stamp,
                "orderLegCollection": [{"legId": 1, "instruction": "BUY",
                                        "instrument": {"symbol": "XLAP"}}],
                "orderActivityCollection": ([{"executionLegs": [{"legId": 1,
                    "quantity": str(self.filled), "price": "3.57", "time": stamp}]}]
                    if self.filled else [])}

    async def request(self, method, path, *, body=None, entry_guard=None):
        # Match schwab.py:1478-1492; guard before the simulated transport send.
        if entry_guard is not None:
            if method.upper() != "POST" or not path.rstrip("/").endswith("/orders") or not body:
                raise RuntimeError("mombuffer1_guard_transport_shape_unknown")
            legs = body.get("orderLegCollection") or []
            entry_guard.check(quantity=legs[0].get("quantity") if len(legs) == 1 else None,
                              limit=body.get("price"),
                              symbol=(legs[0].get("instrument") or {}).get("symbol") if len(legs) == 1 else None)
            entry_guard.before_transport()
        self.calls.append((method, path, deepcopy(body)))
        if method == "POST":
            self.quantity = D(str(body["orderLegCollection"][0]["quantity"]))
            return self.place_status, self.headers, self.body
        assert path == "/trader/v1/accounts/hash-k/orders/12345"
        if method == "DELETE":
            if self.fill_on_delete:
                self.filled = self.quantity
                self.status = "FILLED"
            else:
                self.status = "CANCELED"
            return 200, {}, {}
        assert method == "GET"
        return 200, {}, self.raw()


def harness(monkeypatch, hour=14):
    now = [datetime(2026, 10, 16, hour, tzinfo=UTC)]
    settings = Settings(_env_file=None, oms_adapter="paper", momentum_live_enabled=True,
        strategy_schwab_1m_v2_enabled=True,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_broker_provider="schwab",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        schwab_base_url="https://api.schwabapi.com")
    leaf = SchwabBrokerAdapter(settings, accounts_by_name={
        "live:schwab_1m_v2": SchwabAccountConfig(account_hash="hash-k")})
    wire = ControlledSchwabWire(lambda: now[0])
    monkeypatch.setattr(leaf, "_authorized_request_json", wire.request)
    monkeypatch.setattr(leaf, "_wait_for_terminal_order", AsyncMock(
        side_effect=AssertionError("MOM place ACK must not await terminal polling")))
    service = OmsRiskService(settings, SimpleNamespace(xadd=AsyncMock()),
        session_factory=build_test_session_factory(), broker_adapter=leaf)
    service.seed_runtime_metadata()
    runtime = OmsLiveRuntime(service, clock=lambda: now[0])
    runtime.book = EpisodeLedger.restore([])
    service._latest_quotes_by_symbol["XLAP"] = {
        "ask": D("3.57"), "bid": D("3.56"), "received_at": now[0]}
    with service.session_factory() as session:
        registered = {account.name: account.provider for account in session.scalars(
            select(BrokerAccount).where(BrokerAccount.name.in_(runtime.accounts))).all()}
        assert registered == {runtime.accounts[0]: "schwab", runtime.accounts[1]: "webull"}
        record_receipts(session, [(account, now[0], 0) for account in runtime.accounts], recorded_at=now[0])
        session.commit()
    ports = ExecutionPorts(runtime.clock, runtime.quote, runtime.route_for, runtime._foreign_owner,
        runtime._claim, runtime._prepare, runtime._save, runtime._submit, runtime._record,
        runtime._route_report)
    executor = LiveEntryExecutor(runtime.book, ports, schwab_account=runtime.accounts[0],
                                 webull_account=runtime.accounts[1])
    signal = Signal("2026-10-16", "XLAP", "controlled-rest", 100, 200, D("3.57"),
                    runtime.now_ns(), 50000, country_route="schwab",
                    country_category="sec_us", country_evidence_json='{"classification":"US"}')
    return SimpleNamespace(now=now, leaf=leaf, wire=wire, service=service,
                           runtime=runtime, executor=executor, signal=signal)


async def deadline_cancel(h, identity):
    h.now[0] += timedelta(seconds=2, microseconds=1)
    assert h.runtime.book.cancel_due(identity, now_ns=h.runtime.now_ns())
    await h.runtime._save(h.runtime.book.snapshot()[0])
    await h.runtime._cancel_entry(identity)


@pytest.mark.asyncio
@pytest.mark.parametrize("hour,session_name", [(12, "AM"), (14, "NORMAL")])
async def test_controlled_xlap_rest_past_2s_commits_ack_then_cancels_exact_native_id(monkeypatch, hour, session_name):
    h = harness(monkeypatch, hour)
    identity, original, result = await h.executor.admit(h.signal)
    assert result["broker_order_id"] == "12345" and result["status"] == "accepted"
    assert result["quantity"] == 0
    assert [call[0] for call in h.wire.calls] == ["POST"]
    payload = h.wire.calls[0][2]
    assert payload["session"] == session_name and payload["orderType"] == "LIMIT"
    assert payload["price"] == 3.57 and payload["duration"] == "DAY"
    with h.service.session_factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == original.client_order_id))
        assert order.broker_order_id == "12345" and order.status == "accepted"
    await deadline_cancel(h, identity)
    # MOMORDER1 reads before DELETE and attributes a final original-order read.
    assert [call[0] for call in h.wire.calls] == ["POST", "GET", "DELETE", "GET", "GET"]
    await h.runtime._cancel_entry(identity)
    assert len([call for call in h.wire.calls if call[0] == "DELETE"]) == 1
    await h.executor.admit(h.signal)
    assert len([call for call in h.wire.calls if call[0] == "POST"]) == 1
    observation = await h.runtime._observe(identity)
    assert observation.entry_terminal and observation.quantity == 0
    with h.service.session_factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == original.client_order_id))
        assert order.status == "cancelled" and order.broker_order_id == "12345"
    h.leaf._wait_for_terminal_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [12, 14])
async def test_early_ack_working_observe_then_fill_sets_trusted_first_fill(monkeypatch, hour):
    h = harness(monkeypatch, hour)
    identity, original, result = await h.executor.admit(h.signal)
    assert result["status"] == "accepted" and result["broker_order_id"] == "12345"
    assert result["quantity"] == 0 and result["first_fill_ns"] is None
    assert [call[0] for call in h.wire.calls] == ["POST"]
    with h.service.session_factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == original.client_order_id))
        assert order.status == "accepted" and order.broker_order_id == "12345"
        assert not list(session.scalars(select(Fill)))

    h.now[0] += timedelta(milliseconds=500)
    resting = await h.runtime._observe(identity)
    assert resting.quantity == 0 and not resting.entry_terminal
    assert resting.first_fill_ns is None and resting.position is None
    assert [call[0] for call in h.wire.calls] == ["POST", "GET"]
    with h.service.session_factory() as session:
        assert not list(session.scalars(select(Fill)))

    h.now[0] += timedelta(milliseconds=500)
    h.wire.status, h.wire.filled = "FILLED", original.quantity
    execution_time = datetime.fromisoformat(h.wire.raw()["orderActivityCollection"][0]["executionLegs"][0]["time"])
    expected_ns = int(execution_time.timestamp() * 1e9)
    assert expected_ns > h.runtime.book.episodes[identity].first_submit_ns
    observation = await h.runtime._observe(identity)
    assert observation.entry_terminal and observation.quantity == original.quantity
    assert observation.exited == 0 and observation.position.quantity == original.quantity
    assert observation.first_fill_ns == expected_ns and expected_ns > 0
    assert [call[0] for call in h.wire.calls] == ["POST", "GET", "GET"]
    assert all(call[1] == "/trader/v1/accounts/hash-k/orders/12345"
               for call in h.wire.calls if call[0] == "GET")

    repeated = await h.runtime._observe(identity)
    assert repeated.first_fill_ns == expected_ns and repeated.quantity == original.quantity
    snapshot = await h.runtime._inspect(identity)
    assert snapshot["first_fill_ns"] == expected_ns
    with h.service.session_factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == original.client_order_id))
        assert order.status == "filled" and order.broker_order_id == "12345"
        assert order.payload["momlive_first_fill_ns"] == str(expected_ns)
        fills = session.scalars(select(Fill)).all()
        assert len(fills) == 1
        fill = fills[0]
        assert fill.order_id == order.id and fill.strategy_id == order.strategy_id
        assert fill.broker_account_id == order.broker_account_id and fill.side == "buy"
        assert fill.broker_fill_id and fill.quantity == original.quantity and fill.price == D("3.57")
        assert fill.filled_at.replace(tzinfo=UTC) == execution_time
        assert fill.payload["momlive_fill_source"] == "broker"
        assert h.runtime.db.fill_value(fill) == original.quantity * D("3.57")
    assert [call[0] for call in h.wire.calls] == ["POST", "GET", "GET"]
    h.leaf._wait_for_terminal_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [12, 14])
async def test_restart_between_ack_and_cancel_recovers_id_and_latch_from_rows(monkeypatch, hour):
    h = harness(monkeypatch, hour)
    identity, original, _ = await h.executor.admit(h.signal)
    restarted = OmsLiveRuntime(h.service, clock=lambda: h.now[0])
    bundle = await h.service._run_db(lambda session: restarted.db.restore_bundle(
        session, datetime(2026, 10, 16, tzinfo=UTC)), commit=False)
    restarted.book = EpisodeLedger.restore(bundle["episodes"])
    restarted.runtime.update(bundle["runtime"])
    restarted.bindings.update(bundle["bindings"])
    h.runtime = restarted
    row = (await restarted._inspect(identity))["orders"][0]
    assert row["request"].client_order_id == original.client_order_id
    assert row["request"].metadata["broker_order_id"] == "12345"
    await deadline_cancel(h, identity)
    bundle = await h.service._run_db(lambda session: restarted.db.restore_bundle(
        session, datetime(2026, 10, 16, tzinfo=UTC)), commit=False)
    restarted.runtime = bundle["runtime"]
    await restarted._cancel_entry(identity)
    assert restarted.runtime[identity]["cancel_wire_sent_coid"] == original.client_order_id
    assert [call[0] for call in h.wire.calls] == ["POST", "GET", "DELETE", "GET", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [12, 14])
@pytest.mark.parametrize("race", ["filled_before_cancel", "fill_on_delete", "partial_before_cancel"])
async def test_fill_wins_and_partial_is_booked_once_from_original_order(monkeypatch, hour, race):
    h = harness(monkeypatch, hour)
    identity, original, _ = await h.executor.admit(h.signal)
    if race == "filled_before_cancel":
        h.wire.status, h.wire.filled = "FILLED", original.quantity
        observation = await h.runtime._observe(identity)
        await h.runtime._cancel_entry(identity)
        assert not any(call[0] == "DELETE" for call in h.wire.calls)
    else:
        if race == "partial_before_cancel":
            h.wire.status, h.wire.filled = "PARTIALLY_FILLED", D(7)
            before = await h.runtime._observe(identity)
            assert before.quantity == 7 and not before.entry_terminal
        else:
            h.wire.fill_on_delete = True
        await deadline_cancel(h, identity)
        if race == "fill_on_delete":
            # The cancel's finally-read now books the fill before another cadence.
            assert (await h.runtime._inspect(identity))["quantity"] == original.quantity
        observation = await h.runtime._observe(identity)
    assert observation.entry_terminal
    expected = D(7) if race == "partial_before_cancel" else original.quantity
    assert observation.quantity == expected and observation.exited == 0
    assert observation.position.quantity == expected
    await h.runtime._observe(identity)
    with h.service.session_factory() as session:
        fills = session.scalars(select(Fill)).all()
        assert len(fills) == 1 and fills[0].quantity == expected
        assert h.runtime.db.fill_value(fills[0]) == expected * D("3.57")
    assert len([call for call in h.wire.calls if call[0] == "POST"]) == 1
    assert len([call for call in h.wire.calls if call[0] == "DELETE"]) <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["missing", "wrong_account", "wrong_host", "query", "fragment",
    "body_conflict", "duplicate_header", "not_created", "nonnumeric", "body_only", "malformed_url"])
async def test_ambiguous_post_ack_keeps_reservation_refuses_cancel_and_pages_once(monkeypatch, defect, caplog):
    h = harness(monkeypatch)
    location = h.wire.headers["Location"]
    if defect == "missing":
        h.wire.headers = {}
    elif defect == "wrong_account":
        h.wire.headers["Location"] = location.replace("hash-k", "foreign")
    elif defect == "wrong_host":
        h.wire.headers["Location"] = location.replace("api.schwabapi.com", "other.invalid")
    elif defect in {"query", "fragment"}:
        h.wire.headers["Location"] += "?bad=1" if defect == "query" else "#bad"
    elif defect == "body_conflict":
        h.wire.body = {"orderId": "99999"}
    elif defect == "duplicate_header":
        h.wire.headers["location"] = location.replace("12345", "99999")
    elif defect == "not_created":
        h.wire.place_status = 202
    elif defect == "nonnumeric":
        h.wire.headers["Location"] = location.replace("12345", "not-an-id")
    elif defect == "malformed_url":
        h.wire.headers["Location"] = "https://[invalid/orders/12345"
    else:
        h.wire.headers, h.wire.body = {}, {"orderId": "12345"}
    with caplog.at_level("ERROR"):
        with pytest.raises(Refusal, match="cancel_entry_broker_identity_UNKNOWN"):
            await h.executor.admit(h.signal)
        identity = next(iter(h.runtime.book.episodes))
        await deadline_cancel(h, identity)
        await h.runtime._cancel_entry(identity)
    assert len([row for row in caplog.records if row.levelname == "CRITICAL" and "MOMCANCEL1" in row.message]) == 1
    snapshot = await h.runtime._inspect(identity)
    assert not snapshot["entry_terminal"] and snapshot["quantity"] == 0
    assert snapshot["orders"][0]["broker_order_id"] is None
    assert snapshot["orders"][0]["request"].metadata["momcancel1_ack_identity"] == "UNKNOWN"
    assert await h.runtime.backend.fetch_entry_update(snapshot["orders"][0]["request"], h.runtime) is None
    await h.executor.admit(h.signal)
    assert [call[0] for call in h.wire.calls] == ["POST"]
    restored = await h.service._run_db(lambda session: h.runtime.db.restore_bundle(
        session, datetime(2026, 10, 16, tzinfo=UTC)), commit=False)
    h.runtime.runtime = restored["runtime"]
    await h.runtime._cancel_entry(identity)
    assert len([row for row in caplog.records if row.levelname == "CRITICAL" and "MOMCANCEL1" in row.message]) == 1


@pytest.mark.asyncio
async def test_conflicting_late_ack_never_replaces_durable_id_or_authorizes_cancel(monkeypatch, caplog):
    h = harness(monkeypatch)
    identity, original, _ = await h.executor.admit(h.signal)
    report = ExecutionReport("accepted", original.client_order_id, broker_order_id="99999",
        symbol=original.symbol, side="buy", intent_type="open", origin="broker",
        quantity=original.quantity,
        metadata={"momcancel1_place_ack": "true", "momcancel1_ack_identity": "VALID"})
    with pytest.raises(Refusal):
        await h.runtime._record(original, [report])
    with pytest.raises(Refusal):
        await deadline_cancel(h, identity)
    snapshot = await h.runtime._inspect(identity)
    assert snapshot["orders"][0]["broker_order_id"] == "12345"
    assert not snapshot["entry_terminal"]
    assert [call[0] for call in h.wire.calls] == ["POST"]


@pytest.mark.asyncio
async def test_preexisting_missing_id_is_critical_once_and_never_reads_or_wires(monkeypatch, caplog):
    h = harness(monkeypatch)
    identity, _, _ = await h.executor.admit(h.signal)
    with h.service.session_factory() as session:
        order = session.scalar(select(BrokerOrder))
        order.broker_order_id = None
        order.payload = {key: value for key, value in order.payload.items() if not key.startswith("momcancel1_")}
        session.commit()
    with pytest.raises(Refusal):
        await deadline_cancel(h, identity)
    with pytest.raises(Refusal):
        await h.runtime._cancel_entry(identity)
    assert [call[0] for call in h.wire.calls] == ["POST"]
    assert len([row for row in caplog.records if row.levelname == "CRITICAL" and "MOMCANCEL1" in row.message]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["filled", "cancelled", "rejected", "expired", "aborted"])
async def test_late_valid_ack_cannot_reopen_terminal_entry(monkeypatch, terminal):
    h = harness(monkeypatch)
    identity, original, _ = await h.executor.admit(h.signal)
    with h.service.session_factory() as session:
        order = session.scalar(select(BrokerOrder))
        order.status = terminal
        session.commit()
    await h.runtime._record(original, [ExecutionReport("accepted", original.client_order_id,
        symbol=original.symbol, side="buy", intent_type="open", quantity=original.quantity, origin="broker",
        broker_order_id="12345", metadata={"momcancel1_place_ack": "true", "momcancel1_ack_identity": "VALID"})])
    assert (await h.runtime._inspect(identity))["orders"][0]["status"] == terminal
    await h.runtime._cancel_entry(identity)
    assert [call[0] for call in h.wire.calls] == ["POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["strategy", "side", "intent", "reason", "order_type", "owner_marker", "one_shot", "episode", "coid"])
async def test_mom_fast_ack_does_not_capture_other_request_shapes(monkeypatch, defect):
    h = harness(monkeypatch)
    request = h.executor._request(h.signal, "controlled-episode", Route.SCHWAB)
    if defect == "strategy":
        request = replace(request, strategy_code="other")
    elif defect == "side":
        request = replace(request, side="sell")
    elif defect == "intent":
        request = replace(request, intent_type="scale")
    elif defect == "reason":
        request = replace(request, reason="OTHER")
    elif defect == "order_type":
        request = replace(request, order_type="market")
    elif defect == "coid":
        request = replace(request, client_order_id="another-entry")
    else:
        key = {"owner_marker": "momlive_strategy", "one_shot": "momlive_one_shot", "episode": "episode_id"}[defect]
        request = replace(request, metadata={k: value for k, value in request.metadata.items() if k != key})
    terminal = AsyncMock(return_value=None)
    monkeypatch.setattr(h.leaf, "_wait_for_terminal_order", terminal)
    reports = await h.leaf.submit_order(request)
    terminal.assert_awaited_once()
    assert "momcancel1_place_ack" not in reports[0].metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["Location", "location", "LOCATION"])
async def test_matching_header_body_identity_is_validated_without_extra_read(monkeypatch, header):
    h = harness(monkeypatch)
    h.wire.headers = {header: h.wire.headers["Location"]}
    h.wire.body = {"orderId": 12345}
    _, _, result = await h.executor.admit(h.signal)
    assert result["broker_order_id"] == "12345"
    assert [call[0] for call in h.wire.calls] == ["POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [12, 14])
async def test_webull_fufu_shape_cancels_original_coid_without_native_id(monkeypatch, fake_sdk, hour):
    h = harness(monkeypatch, hour)
    client = _FakeClient({"cancel": {}})
    leaf = webull_adapter(client, _query_budget=QueryBudget())
    h.service.broker_adapter = leaf
    signal = replace(h.signal, symbol="FUFU", country_route="webull")
    episode, fresh = h.runtime.book.claim(signal, now_ns=h.runtime.now_ns())
    assert fresh
    data = h.runtime.book.snapshot()[0]
    assert await h.runtime._claim(data, "live:orb", "webull", signal)
    original = OrderRequest(f"ml1-FUFU-e-{episode.episode_id.replace('-', '')[:20]}",
        "live:orb", STRATEGY_CODE, "FUFU", "buy", "open", D(28), "MOMLIVE1_DETECTED",
        {"episode_id": episode.episode_id, "momlive_strategy": STRATEGY_CODE,
         "momlive_one_shot": "true"}, "limit", "day")
    await h.runtime._prepare(episode.episode_id, original, "webull")
    h.runtime.book.begin_submit(episode.episode_id, Route.WEBULL, now_ns=h.runtime.now_ns())
    await h.runtime._save(h.runtime.book.snapshot()[0])
    assert (await h.runtime._inspect(episode.episode_id))["orders"][0]["broker_order_id"] is None
    client._bodies["detail"] = {"account_id": "ACC1", "client_order_id": original.client_order_id,
        "order_id": "EJJSLL5VJ047EH7KV7K224SEV9", "items": [{"symbol": "FUFU", "side": "BUY",
            "qty": "28", "order_status": "WORKING", "filled_qty": "0"}]}
    h.service._reserve1_sync_pass_id = 1
    await deadline_cancel(h, episode.episode_id)
    await asyncio.gather(*h.runtime._missing_entry_reads.values())
    await h.runtime._cancel_entry(episode.episode_id)
    await h.runtime._cancel_entry(episode.episode_id)
    assert client.calls == {"detail": 2, "cancel": 1}
    assert client.last["cancel"].values == {"account_id": "ACC1", "client_order_id": original.client_order_id}
    assert h.runtime.runtime[episode.episode_id]["cancel_wire_sent_coid"] == original.client_order_id
    assert not (await h.runtime._inspect(episode.episode_id))["entry_terminal"]
    assert h.wire.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["atr_primary", "atr_stop", "atr_regular", "orb", "other", "mom_sell", "unmarked"])
async def test_other_schwab_callers_keep_existing_fast_return_or_terminal_poll(monkeypatch, caller):
    h = harness(monkeypatch)
    request = h.executor._request(h.signal, "controlled-episode", Route.SCHWAB)
    # Keep the raw anchor actually sealed by LiveEntryExecutor._request.
    metadata = {"limit_price": "3.57", "signal_price": request.metadata["signal_price"]}
    fast = caller in {"atr_primary", "atr_stop"}
    if caller == "atr_primary":
        h.leaf.settings.strategy_schwab_1m_v2_webull_resting_mirror_enabled = True
        request = replace(request, strategy_code="schwab_1m_v2", reason="ATR",
            metadata={**metadata, "entry_price": "3.56", "atr_variant": "CW-v2-resting",
                      "resting_entry": "true", "fanout_slot_id": "slot"})
    elif caller == "atr_stop":
        request = replace(request, strategy_code="schwab_1m_v2", side="sell", intent_type="close",
                          reason="HARD_STOP", metadata={**metadata, "stop_guard": "true"})
    elif caller == "orb":
        h.leaf.settings.orb_live_schwab_orders_enabled = True
        request = replace(request, strategy_code="orb_schwab", reason="ORB", order_type="stop_limit",
            metadata={**metadata, "bracket": "true", "bracket_entry_type": "STOP_LIMIT",
                "orb_intended_break_level": "3.56", "stop_price": "3.56",
                "bracket_target_price": "3.75", "bracket_stop_price": "3.03"})
    elif caller == "mom_sell":
        request = replace(request, side="sell", intent_type="close", reason="MOMLIVE1_EXIT", metadata=metadata)
    elif caller == "unmarked":
        request = replace(request, metadata=metadata)
    else:
        request = replace(request, strategy_code="schwab_1m_v2" if caller == "atr_regular" else "other",
                          reason="ENTRY", metadata={**metadata, "entry_price": "3.56"})
    terminal = AsyncMock(return_value=None)
    monkeypatch.setattr(h.leaf, "_wait_for_terminal_order", terminal)
    reports = await h.leaf.submit_order(request)
    assert [report.event_type for report in reports] == ["accepted"]
    assert reports[0].broker_order_id == "12345"
    assert "momcancel1_ack_identity" not in reports[0].metadata
    if fast:
        terminal.assert_not_awaited()
    else:
        terminal.assert_awaited_once_with(h.leaf.accounts_by_name[request.broker_account_name], "12345")
    assert [call[0] for call in h.wire.calls] == ["POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("episode_id", [7, [], object()])
async def test_malformed_episode_marker_after_post_keeps_original_terminal_poll(monkeypatch, episode_id):
    h = harness(monkeypatch)
    request = h.executor._request(h.signal, "controlled-episode", Route.SCHWAB)
    request = replace(request, metadata={**request.metadata, "episode_id": episode_id})
    terminal = AsyncMock(return_value=None)
    monkeypatch.setattr(h.leaf, "_wait_for_terminal_order", terminal)
    reports = await h.leaf.submit_order(request)
    assert [call[0] for call in h.wire.calls] == ["POST"]
    terminal.assert_awaited_once_with(h.leaf.accounts_by_name[request.broker_account_name], "12345")
    assert "momcancel1_place_ack" not in reports[0].metadata


@pytest.mark.parametrize("metadata", [None, 7, [], object(), {"episode_id": object()}])
def test_mom_scope_predicate_is_total_for_malformed_metadata(metadata):
    request = OrderRequest("irrelevant", "live:schwab_1m_v2", STRATEGY_CODE,
        "XLAP", "buy", "open", D(28), "MOMLIVE1_DETECTED", metadata, "limit", "day")
    assert SchwabBrokerAdapter._is_momlive_entry(request) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [7, None, ("Location",)])
async def test_nonstring_ack_header_key_is_unknown_after_post_no_extra_wire(monkeypatch, key):
    h = harness(monkeypatch)
    h.wire.headers[key] = "malformed header key"
    with pytest.raises(Refusal, match="^cancel_entry_broker_identity_UNKNOWN$"):
        await h.executor.admit(h.signal)
    identity = next(iter(h.runtime.book.episodes))
    snapshot = await h.runtime._inspect(identity)
    assert snapshot["orders"][0]["broker_order_id"] is None
    assert not snapshot["entry_terminal"]
    assert [call[0] for call in h.wire.calls] == ["POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError, TypeError, RuntimeError])
async def test_ack_parser_error_after_post_is_unknown_not_unsent(monkeypatch, error):
    h = harness(monkeypatch)

    def broken_parser(value):
        raise error("controlled pure parser failure")

    monkeypatch.setattr(schwab_module, "urlsplit", broken_parser)
    with pytest.raises(Refusal, match="^cancel_entry_broker_identity_UNKNOWN$"):
        await h.executor.admit(h.signal)
    identity = next(iter(h.runtime.book.episodes))
    assert not (await h.runtime._inspect(identity))["entry_terminal"]
    assert [call[0] for call in h.wire.calls] == ["POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("save_failure", ["before_commit", "after_commit"])
@pytest.mark.parametrize("recovery", ["same_process", "restart"])
async def test_actual_runtime_diagnostic_save_failure_preserves_refusal_and_critical(monkeypatch, caplog, save_failure, recovery):
    h = harness(monkeypatch)
    h.wire.headers = {}
    save = h.runtime._save
    failures = [2]

    async def fail_diagnostic_save(episode):
        identity = episode["episode_id"]
        if h.runtime.runtime.get(identity, {}).get("momcancel1_identity_critical_reported") and failures[0]:
            # CRITICAL must already be observable even if this DB write fails.
            assert any(row.levelname == "CRITICAL" and identity in row.message for row in caplog.records)
            failures[0] -= 1
            if save_failure == "after_commit":
                await save(episode)
            raise RuntimeError("controlled diagnostic save failure")
        await save(episode)

    monkeypatch.setattr(h.runtime, "_save", fail_diagnostic_save)
    with pytest.raises(Refusal, match="^cancel_entry_broker_identity_UNKNOWN$") as refused:
        await h.executor.admit(h.signal)
    assert refused.value.__cause__ is None
    identity = next(iter(h.runtime.book.episodes))
    assert h.runtime.runtime[identity].get("momcancel1_identity_critical_reported") is not True
    assert identity in h.runtime._momcancel1_identity_critical_emitted
    await h.runtime._cancel_entry(identity)
    assert h.runtime.runtime[identity].get("momcancel1_identity_critical_reported") is not True
    assert len([row for row in caplog.records if row.levelname == "CRITICAL" and "MOMCANCEL1" in row.message]) == 1
    bundle = await h.service._run_db(lambda session: h.runtime.db.restore_bundle(
        session, datetime(2026, 10, 16, tzinfo=UTC)), commit=False)
    assert bool(bundle["runtime"][identity].get("momcancel1_identity_critical_reported")) == (save_failure == "after_commit")
    if recovery == "restart":
        restarted = OmsLiveRuntime(h.service, clock=lambda: h.now[0])
        restarted.book = EpisodeLedger.restore(bundle["episodes"])
        restarted.runtime.update(bundle["runtime"])
        restarted.bindings.update(bundle["bindings"])
        h.runtime = restarted
    for _ in range(2):
        await h.runtime._cancel_entry(identity)
    expected_critical = 1
    critical = [row for row in caplog.records if row.levelname == "CRITICAL" and "MOMCANCEL1" in row.message]
    assert len(critical) == expected_critical
    assert all(identity in row.message for row in critical)
    assert any("diagnostic_receipt=UNKNOWN" in row.message for row in caplog.records)
    bundle = await h.service._run_db(lambda session: h.runtime.db.restore_bundle(
        session, datetime(2026, 10, 16, tzinfo=UTC)), commit=False)
    assert bool(bundle["runtime"][identity].get("momcancel1_identity_critical_reported")) == (save_failure == "after_commit")
    snapshot = await h.runtime._inspect(identity)
    assert not snapshot["entry_terminal"] and snapshot["quantity"] == 0
    assert snapshot["orders"][0]["request"].metadata["momcancel1_ack_identity"] == "UNKNOWN"
    assert [call[0] for call in h.wire.calls] == ["POST"]


async def _c11145_pending_entry(h, provider, *, symbol="IPST"):
    """Existing harness, controlled pending shape; NOT a historical replay."""
    signal = replace(h.signal, symbol=symbol, signal_price=D("3.04"), country_route=provider,
                     observed_ns=h.runtime.now_ns())
    h.service._latest_quotes_by_symbol[symbol] = {
        "ask": D("3.04"), "bid": D("3.03"), "received_at": h.now[0]}
    episode, fresh = h.runtime.book.claim(signal, now_ns=h.runtime.now_ns())
    assert fresh
    account = h.runtime.accounts[0 if provider == "schwab" else 1]
    assert await h.runtime._claim(h.executor._snapshot(episode.episode_id), account, provider, signal)
    route = Route.SCHWAB if provider == "schwab" else Route.WEBULL
    request = h.executor._request(signal, episode.episode_id, route)
    assert request.quantity == 32
    await h.runtime._prepare(episode.episode_id, request, provider)
    h.runtime.book.begin_submit(episode.episode_id, route, now_ns=h.runtime.now_ns())
    h.now[0] += timedelta(seconds=3)
    assert h.runtime.book.cancel_due(episode.episode_id, now_ns=h.runtime.now_ns())
    await h.runtime._save(h.executor._snapshot(episode.episode_id))
    h.service._reserve1_sync_pass_id = 1
    return episode.episode_id, (await h.runtime._inspect(episode.episode_id))["orders"][0]["request"]


async def _c11220_conflict_entry(h, provider, *, before_begin=False):
    """Controlled preimage conflict through the REAL handler, not historical cause proof."""
    signal = replace(h.signal, symbol="IPST", signal_price=D("3.04"), country_route=provider)
    h.service._latest_quotes_by_symbol["IPST"] = {
        "ask": D("3.04"), "bid": D("3.03"), "received_at": h.now[0]}
    async def conflict(previous, current, episode=None):
        # Retain a changed preimage, the safety situation this branch handles.
        with h.service.session_factory() as session:
            order = session.scalar(select(BrokerOrder).where(
                BrokerOrder.client_order_id == previous.client_order_id))
            order.payload = {**order.payload, "reference_price": "3.03"}
            session.commit()
        await h.runtime._reprice_unsent_entry(previous, current, episode)
    h.executor.ports.reprice_unsent_entry = conflict
    h.executor.ports.mark_refused_pre_submit = h.runtime._mark_refused_pre_submit
    if before_begin:
        async def prepare(identity, request, route):
            await h.runtime._prepare(identity, request, route)
            h.service._latest_quotes_by_symbol["IPST"]["ask"] = D("3.05")
        h.executor.ports.durable_order = prepare
    with pytest.raises(UnsentEntryConflict, match="mombuffer1_unsent_preimage_changed"):
        await h.executor.admit(signal)
    identity = h.runtime.book.snapshot()[0]["episode_id"]
    request = (await h.runtime._inspect(identity))["orders"][0]["request"]
    assert request.metadata["refused_pre_submit"] is True
    assert request.metadata["momlive_submit_attempt_count"] == "1"
    h.now[0] += timedelta(seconds=3)
    if not h.runtime.book.episodes[identity].cancel_requested:
        assert h.runtime.book.cancel_due(identity, now_ns=h.runtime.now_ns())
    await h.runtime._save(h.runtime.book.snapshot()[0])
    h.service._reserve1_sync_pass_id = 1
    return identity, request


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [12, 14], ids=["PM", "RTH"])
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("before_begin", [False, True], ids=["second-refresh", "first-refresh"])
async def test_c11220_real_conflict_marker_settles_through_actor_and_stays_closed(monkeypatch, caplog, fake_sdk, hour, provider, before_begin):
    h = harness(monkeypatch, hour)
    identity, request = await _c11220_conflict_entry(h, provider, before_begin=before_begin)
    other = replace(h.signal, symbol="HOLD", source_id="C11145-other-slot")
    h.runtime.book.claim(other, now_ns=h.runtime.now_ns())
    assert h.runtime.book._active_count == 2
    # Local first-attempt refusal is proof; no broker list/detail is needed.
    actor = LiveActor(h.runtime.book, h.executor, ActorPorts(lambda: True, h.runtime.now_ns,
        h.runtime._observe, h.runtime._save, h.runtime._cancel_entry,
        h.runtime._protect, h.runtime._exit, h.runtime._exit_quote), state_lock=h.runtime.state_lock)
    h.runtime.actor = actor
    if provider == "webull":
        client = _FakeClient({})
        client.raises["detail"] = _ServerException("ORDER_NOT_FOUND", "controlled exact client response", 417)
        h.service.broker_adapter = webull_adapter(client, _query_budget=QueryBudget())
    lookup = AsyncMock(wraps=h.runtime.backend.lookup_missing_entry)
    monkeypatch.setattr(h.runtime.backend, "lookup_missing_entry", lookup)
    updates = []
    engine = h.service.session_factory.kw["bind"]
    def capture_update(connection, cursor, sql, parameters, context, many):
        if context.compiled is not None and getattr(context.compiled.statement, "is_update", False):
            updates.append(context.compiled.statement.table.name)
    event.listen(engine, "before_cursor_execute", capture_update)
    try:
        with caplog.at_level("INFO", logger="oms-risk"):
            await h.runtime._cancel_entry(identity)
            await asyncio.gather(*h.runtime._missing_entry_reads.values())
            await actor._service_owned(identity)
            await actor._service_owned(identity)
    finally:
        event.remove(engine, "before_cursor_execute", capture_update)
    assert lookup.await_count == 1 and h.runtime.book._active_count == 1
    assert set(updates) == {"broker_orders", "trade_intents"}
    assert updates.count("broker_orders") == 1
    with h.service.session_factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        parent = session.get(TradeIntent, order.intent_id)
        assert order.status == "aborted" and parent.status == "cancelled"
        assert order.payload["momlive_entry_never_dispatched"] is True
        assert parent.payload["momlive_episode"]["state"] == "terminal_no_fill"
        assert parent.payload["momlive_episode"]["closed_ns"] == h.runtime.book.episodes[identity].closed_ns
        assert not list(session.scalars(select(Fill)))
    assert len([r for r in caplog.records if "broker_client_lookup=ABSENT" in r.message]) == 1
    assert h.wire.calls == []
    if provider == "webull":
        assert client.calls == {"detail": 1}
    assert h.runtime.book.episodes[identity].state == "terminal_no_fill"
    assert not h.runtime.book._active_by_symbol.get("IPST")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour", [12, 14], ids=["PM", "RTH"])
async def test_c11220_two_retained_clients_max_two_reads_per_pass(monkeypatch, fake_sdk, provider, hour):
    h = harness(monkeypatch, hour)
    first, _ = await _c11145_pending_entry(h, provider)
    second, _ = await _c11145_pending_entry(h, provider, symbol="BBCI")
    assert h.runtime.book._active_count == 2
    if provider == "webull":
        client = _FakeClient({})
        client.raises["detail"] = TimeoutError("controlled unknown")
        h.service.broker_adapter = webull_adapter(client, _query_budget=QueryBudget())
    lookup = AsyncMock(wraps=h.runtime.backend.lookup_missing_entry)
    monkeypatch.setattr(h.runtime.backend, "lookup_missing_entry", lookup)
    for _ in range(10):
        for identity in (first, second):
            await h.runtime._cancel_entry(identity)
        await asyncio.gather(*h.runtime._missing_entry_reads.values())
    assert lookup.await_count == 2
    assert len({call.args[0].client_order_id for call in lookup.await_args_list}) == 2
    assert {call.args[2] for call in lookup.await_args_list} == {1}
    if provider == "webull":
        assert client.calls == {"detail": 2}
    assert h.runtime.book._active_count == 2 and h.wire.calls == []
    with pytest.raises(Refusal):
        h.runtime.book.claim(replace(h.signal, symbol="THIRD"), now_ns=h.runtime.now_ns())


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["permit", "submitted", "native", "attempt", "count", "marker", "episode"])
async def test_c11220_conflict_marker_cannot_override_dispatch_or_attempt_uncertainty(monkeypatch, corruption):
    h = harness(monkeypatch)
    identity, request = await _c11220_conflict_entry(h, "schwab")
    with h.service.session_factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        parent = session.get(TradeIntent, order.intent_id)
        if corruption == "submitted":
            order.submitted_at = h.now[0]
        elif corruption == "native":
            order.broker_order_id = "uncertain-native"
        elif corruption == "episode":
            parent.payload = {**parent.payload, "momlive_entry_attempt": {}}
        else:
            key, value = {"permit": ("bot_control_permit", "MAY_HAVE_SENT"),
                "attempt": ("momlive_submit_attempt_id", "different-attempt"),
                "count": ("momlive_submit_attempt_count", "2"),
                "marker": ("refused_pre_submit", False)}[corruption]
            order.payload = {**order.payload, key: value}
        session.commit()
    snapshot = await h.runtime._inspect(identity)
    current = snapshot["orders"][0]["request"]
    proof = MomentumEntryLookup("absent", current.broker_account_name, current.client_order_id,
        current.symbol, "schwab", 1, h.runtime.now_ns(), "refused_pre_submit_proven")
    with pytest.raises(Refusal):
        await h.service._run_db(lambda session: h.runtime.db.retire_broker_absent_entry(
            session, current, proof, now_ns=h.runtime.now_ns(), sync_pass=1))
    assert not (await h.runtime._inspect(identity))["entry_terminal"]
    assert h.runtime.book._active_count == 1 and h.wire.calls == []


@pytest.mark.asyncio
async def test_c11220_slot_turnover_does_not_allow_third_read_in_same_pass(monkeypatch):
    h = harness(monkeypatch)
    first, _ = await _c11220_conflict_entry(h, "schwab")
    second, _ = await _c11145_pending_entry(h, "schwab", symbol="BBCI")
    lookup = AsyncMock(wraps=h.runtime.backend.lookup_missing_entry)
    monkeypatch.setattr(h.runtime.backend, "lookup_missing_entry", lookup)
    for identity in (first, second):
        await h.runtime._cancel_entry(identity)
    await asyncio.gather(*h.runtime._missing_entry_reads.values())
    for identity in (first, second):
        await h.runtime._cancel_entry(identity)
    assert h.runtime.book._active_count == 1 and lookup.await_count == 2
    third, _ = await _c11145_pending_entry(h, "schwab", symbol="THIRD")
    for _ in range(5):
        await h.runtime._cancel_entry(third)
    assert lookup.await_count == 2 and h.runtime.book._active_count == 2
    h.service._reserve1_sync_pass_id = 2
    await h.runtime._cancel_entry(third)
    await asyncio.gather(*h.runtime._missing_entry_reads.values())
    await h.runtime._cancel_entry(third)
    assert lookup.await_count == 3 and lookup.await_args_list[-1].args[2] == 2


@pytest.mark.asyncio
async def test_c11220_prior_permit_conflict_never_becomes_current_attempt_absence(monkeypatch):
    h = harness(monkeypatch)
    async def prepare(identity, request, provider):
        await h.runtime._prepare(identity, request, provider)
        with h.service.session_factory() as session:
            order = session.scalar(select(BrokerOrder).where(
                BrokerOrder.client_order_id == request.client_order_id))
            order.payload = {**order.payload, "bot_control_permit": "MAY_HAVE_SENT"}
            session.commit()
    h.executor.ports.durable_order = prepare
    h.executor.ports.reprice_unsent_entry = h.runtime._reprice_unsent_entry
    h.executor.ports.mark_refused_pre_submit = h.runtime._mark_refused_pre_submit
    with pytest.raises(UnsentEntryConflict, match="prewire_settlement_dispatch_or_owner_UNKNOWN"):
        await h.executor.admit(h.signal)
    identity = h.runtime.book.snapshot()[0]["episode_id"]
    snapshot = await h.runtime._inspect(identity)
    metadata = snapshot["orders"][0]["request"].metadata
    assert metadata["bot_control_permit"] == "MAY_HAVE_SENT"
    assert "refused_pre_submit" not in metadata and not snapshot["entry_terminal"]
    assert h.runtime.book._active_count == 1 and h.wire.calls == []


@pytest.mark.asyncio
async def test_c11220_webull_order_not_found_without_marker_retains_unknown(monkeypatch, fake_sdk):
    h = harness(monkeypatch)
    identity, request = await _c11145_pending_entry(h, "webull")
    client = _FakeClient({})
    client.raises["detail"] = _ServerException("ORDER_NOT_FOUND", "not enough without marker", 417)
    h.service.broker_adapter = webull_adapter(client, _query_budget=QueryBudget())
    for _ in range(5):
        await h.runtime._cancel_entry(identity)
        await asyncio.gather(*h.runtime._missing_entry_reads.values())
    snapshot = await h.runtime._inspect(identity)
    assert not snapshot["entry_terminal"] and snapshot["orders"][0]["status"] == "pending"
    assert "refused_pre_submit" not in snapshot["orders"][0]["request"].metadata
    assert client.calls == {"detail": 1}
    assert h.runtime.book._active_count == 1 and h.wire.calls == []


@pytest.mark.asyncio
async def test_c11220_settled_owner_rejects_stale_save_and_preserves_closed_ns(monkeypatch):
    h = harness(monkeypatch)
    identity, _ = await _c11220_conflict_entry(h, "schwab")
    stale = h.executor._snapshot(identity)
    await h.runtime._cancel_entry(identity)
    await asyncio.gather(*h.runtime._missing_entry_reads.values())
    await h.runtime._cancel_entry(identity)
    closed_ns = (await h.runtime._inspect(identity))["closed_ns"]
    assert type(closed_ns) is int and closed_ns > 0
    with pytest.raises(Refusal, match="momcancel1_settled_owner_regression_refused"):
        await h.runtime._save(stale)
    await h.runtime._save({**h.executor._snapshot(identity), "closed_ns": None})
    current = await h.runtime._inspect(identity)
    assert current["entry_terminal"] and current["closed_ns"] == closed_ns
    assert h.runtime.book._active_count == 0


@pytest.mark.asyncio
async def test_c11220_marker_persistence_failure_keeps_original_conflict_and_owner(monkeypatch):
    h = harness(monkeypatch)
    h.executor.ports.reprice_unsent_entry = AsyncMock(side_effect=UnsentEntryConflict("original_conflict"))
    h.executor.ports.mark_refused_pre_submit = AsyncMock(side_effect=RuntimeError("controlled DB uncertainty"))
    with pytest.raises(UnsentEntryConflict, match="original_conflict"):
        await h.executor.admit(h.signal)
    identity = h.runtime.book.snapshot()[0]["episode_id"]
    snapshot = await h.runtime._inspect(identity)
    assert not snapshot["entry_terminal"]
    assert "refused_pre_submit" not in snapshot["orders"][0]["request"].metadata
    assert h.runtime.book._active_count == 1 and h.wire.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [12, 14], ids=["PM", "RTH"])
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_c11145_unknown_retains_and_reads_at_most_once_per_actual_sync_pass(monkeypatch, caplog, fake_sdk, hour, provider):
    h = harness(monkeypatch, hour)
    identity, request = await _c11145_pending_entry(h, provider)
    if provider == "webull":
        client = _FakeClient({})
        client.raises["detail"] = TimeoutError("controlled native read failure")
        h.service.broker_adapter = webull_adapter(client, _query_budget=QueryBudget())
    lookup = AsyncMock(wraps=h.runtime.backend.lookup_missing_entry)
    monkeypatch.setattr(h.runtime.backend, "lookup_missing_entry", lookup)
    for _ in range(10):
        await h.runtime._observe(identity)
        await h.runtime._cancel_entry(identity)
        await asyncio.gather(*h.runtime._missing_entry_reads.values())
    assert lookup.await_count == 1
    h.service._reserve1_sync_pass_id = 2
    for _ in range(10):
        await h.runtime._cancel_entry(identity)
        await asyncio.gather(*h.runtime._missing_entry_reads.values())
    assert lookup.await_count == 2
    assert h.runtime.book._active_count == 1
    snapshot = await h.runtime._inspect(identity)
    assert snapshot["orders"][0]["status"] == "pending" and not snapshot["entry_terminal"]
    assert len([r for r in caplog.records if "broker_client_lookup=UNKNOWN" in r.message]) == 1
    assert h.wire.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [12, 14], ids=["PM", "RTH"])
async def test_c11145_wired_cancel_keeps_original_native_path(monkeypatch, hour):
    h = harness(monkeypatch, hour)
    identity, _, _ = await h.executor.admit(h.signal)
    lookup = AsyncMock(side_effect=AssertionError("wired path must not use no-ID lookup"))
    monkeypatch.setattr(h.runtime.backend, "lookup_missing_entry", lookup)
    await deadline_cancel(h, identity)
    lookup.assert_not_awaited()
    assert [call[0] for call in h.wire.calls] == ["POST", "GET", "DELETE", "GET", "GET"]


@pytest.mark.asyncio
async def test_c11145_read_deadline_does_not_start_overlapping_read_and_close_joins(monkeypatch):
    import asyncio
    from threading import Event

    h = harness(monkeypatch)
    identity, request = await _c11145_pending_entry(h, "webull")
    entered, release = Event(), Event()
    async def slow(original, runtime, pass_id):
        def native_read():
            entered.set()
            release.wait()
        await asyncio.to_thread(native_read)
        return MomentumEntryLookup("unknown", original.broker_account_name, original.client_order_id,
            original.symbol, "webull", pass_id, runtime.now_ns(), "controlled_late_read")
    lookup = AsyncMock(side_effect=slow)
    monkeypatch.setattr(h.runtime.backend, "lookup_missing_entry", lookup)
    closing = None
    try:
        await h.runtime._cancel_entry(identity)
        assert len(h.runtime._missing_entry_reads) == 1
        while not entered.is_set():
            await asyncio.sleep(0)
        h.service._reserve1_sync_pass_id = 2
        await h.runtime._cancel_entry(identity)
        assert lookup.await_count == 1
        closing = asyncio.create_task(h.runtime.close())
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        release.set()
        if closing is not None:
            await closing
    assert not h.runtime._missing_entry_reads and h.runtime.book._active_count == 1


@pytest.mark.asyncio
async def test_c11145_actual_ipst_preimages_no_schwab_lookup_retains_unknown(monkeypatch, caplog):
    """Actual captured payloads; seed FK remap only, NOT a historical causal replay."""
    from hashlib import sha256
    import json
    from pathlib import Path
    from uuid import UUID

    path = Path("/private/tmp/OCT27-C11145-IPST-OPERANDS-ONE/preimages.json")
    if not path.exists():
        pytest.skip(f"requires local rig artifact {path}")
    raw = path.read_bytes()
    assert sha256(raw).hexdigest() == "6369dc5f25c4b51ded65c6c1f958222df654e52ca0c9733028f8b6aff5df4cc9"
    rows = json.loads(raw)
    parent_raw, order_raw = rows["parent"], rows["order"]
    h = harness(monkeypatch)
    h.now[0] = datetime(2026, 10, 27, 15, 12, tzinfo=UTC)
    episode = deepcopy(parent_raw["payload"]["momlive_episode"])
    identity = episode["episode_id"]
    h.runtime.book = EpisodeLedger.restore([episode])
    signal = replace(h.signal, symbol="IPST", signal_price=D("3.04"), country_route="schwab")
    assert await h.runtime._claim(episode, h.runtime.accounts[0], "schwab", signal)
    with h.service.session_factory() as session:
        parent = session.get(TradeIntent, UUID(parent_raw["id"]))
        parent.payload = deepcopy(parent_raw["payload"])
        parent.quantity, parent.status = D(str(parent_raw["quantity"])), parent_raw["status"]
        parent.reason = parent_raw["reason"]
        session.add(BrokerOrder(id=UUID(order_raw["id"]), intent_id=parent.id,
            strategy_id=parent.strategy_id, broker_account_id=parent.broker_account_id,
            client_order_id=order_raw["client_order_id"], broker_order_id=order_raw["broker_order_id"],
            symbol=order_raw["symbol"], side=order_raw["side"], order_type=order_raw["order_type"],
            time_in_force=order_raw["time_in_force"], quantity=D(str(order_raw["quantity"])),
            status=order_raw["status"], payload=deepcopy(order_raw["payload"]), submitted_at=None))
        session.commit()
    h.service._reserve1_sync_pass_id = 1
    for _ in range(10):
        await h.runtime._cancel_entry(identity)
        await asyncio.gather(*h.runtime._missing_entry_reads.values())
    with h.service.session_factory() as session:
        order = session.get(BrokerOrder, UUID(order_raw["id"]))
        parent = session.get(TradeIntent, UUID(parent_raw["id"]))
        assert order.status == "pending" and parent.status == "pending"
        assert order.payload == order_raw["payload"] and parent.payload == parent_raw["payload"]
    assert h.runtime.book._active_count == 1 and h.wire.calls == []
    assert len([row for row in caplog.records if "broker_client_lookup=UNKNOWN" in row.message]) == 1
