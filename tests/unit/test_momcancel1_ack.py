"""Controlled venue responses, not a reproduction of the historical XLAB order."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.broker_adapters import schwab as schwab_module
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill
from project_mai_tai.momentum_live.admission import EpisodeLedger, Refusal, Route, STRATEGY_CODE, Signal
from project_mai_tai.momentum_live.execution import ExecutionPorts, LiveEntryExecutor
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.positions_read_receipt import record_receipts
from project_mai_tai.settings import Settings
from tests.unit.test_control_plane import build_test_session_factory
from tests.unit.test_webull_adapter import _adapter as webull_adapter, _FakeClient, fake_sdk


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

    async def request(self, method, path, *, body=None):
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
        with pytest.raises(Refusal):
            await deadline_cancel(h, identity)
        with pytest.raises(Refusal):
            await h.runtime._cancel_entry(identity)
    assert len([row for row in caplog.records if row.levelname == "CRITICAL" and "MOMCANCEL1" in row.message]) == 1
    assert any(row.levelname == "ERROR" and identity in row.message for row in caplog.records)
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
    with pytest.raises(Refusal):
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
    leaf = webull_adapter(client)
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
    await deadline_cancel(h, episode.episode_id)
    await h.runtime._cancel_entry(episode.episode_id)
    assert client.calls == {"cancel": 1}
    assert client.last["cancel"].values == {"account_id": "ACC1", "client_order_id": original.client_order_id}
    assert h.runtime.runtime[episode.episode_id]["cancel_wire_sent_coid"] == original.client_order_id
    assert not (await h.runtime._inspect(episode.episode_id))["entry_terminal"]
    assert h.wire.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["atr_primary", "atr_stop", "atr_regular", "orb", "other", "mom_sell", "unmarked"])
async def test_other_schwab_callers_keep_existing_fast_return_or_terminal_poll(monkeypatch, caller):
    h = harness(monkeypatch)
    request = h.executor._request(h.signal, "controlled-episode", Route.SCHWAB)
    metadata = {"limit_price": "3.57"}
    fast = caller in {"atr_primary", "atr_stop"}
    if caller == "atr_primary":
        h.leaf.settings.strategy_schwab_1m_v2_webull_resting_mirror_enabled = True
        request = replace(request, strategy_code="schwab_1m_v2", reason="ATR",
            metadata={**metadata, "atr_variant": "CW-v2-resting", "resting_entry": "true", "fanout_slot_id": "slot"})
    elif caller == "atr_stop":
        request = replace(request, strategy_code="schwab_1m_v2", side="sell", intent_type="close",
                          reason="HARD_STOP", metadata={**metadata, "stop_guard": "true"})
    elif caller == "orb":
        h.leaf.settings.orb_live_schwab_orders_enabled = True
        request = replace(request, strategy_code="orb_schwab", reason="ORB", order_type="stop_limit",
            metadata={**metadata, "bracket": "true", "bracket_entry_type": "STOP_LIMIT",
                "stop_price": "3.56", "bracket_target_price": "3.75", "bracket_stop_price": "3.03"})
    elif caller == "mom_sell":
        request = replace(request, side="sell", intent_type="close", reason="MOMLIVE1_EXIT", metadata=metadata)
    elif caller == "unmarked":
        request = replace(request, metadata=metadata)
    else:
        request = replace(request, strategy_code="schwab_1m_v2" if caller == "atr_regular" else "other",
                          reason="ENTRY", metadata=metadata)
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
    with pytest.raises(Refusal, match="^cancel_entry_broker_identity_UNKNOWN$"):
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
        with pytest.raises(Refusal, match="^cancel_entry_broker_identity_UNKNOWN$"):
            await h.runtime._cancel_entry(identity)
    expected_critical = 2 if recovery == "restart" and save_failure == "before_commit" else 1
    critical = [row for row in caplog.records if row.levelname == "CRITICAL" and "MOMCANCEL1" in row.message]
    assert len(critical) == expected_critical
    assert all(identity in row.message for row in critical)
    assert any("diagnostic_receipt=UNKNOWN" in row.message for row in caplog.records)
    bundle = await h.service._run_db(lambda session: h.runtime.db.restore_bundle(
        session, datetime(2026, 10, 16, tzinfo=UTC)), commit=False)
    assert bundle["runtime"][identity]["momcancel1_identity_critical_reported"] is True
    snapshot = await h.runtime._inspect(identity)
    assert not snapshot["entry_terminal"] and snapshot["quantity"] == 0
    assert snapshot["orders"][0]["request"].metadata["momcancel1_ack_identity"] == "UNKNOWN"
    assert [call[0] for call in h.wire.calls] == ["POST"]
