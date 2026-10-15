"""E6 controlled local timing; never a recorded counterfactual broker fill."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from project_mai_tai.broker_adapters.protocols import ExitPairReleaseResult, OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from tests.unit.test_edgeprot1_lane_b import drain, quote, setup
from tests.unit.test_v2_cw_managed_exit import ACCT, WEBULL_ACCT
from tests.unit.test_webull_adapter import _adapter, _FakeClient, _ServerException, fake_sdk as _webull_sdk

fake_sdk = _webull_sdk

TAG = "[OMS-V2-CW-TARGET-EXIT] "


def receipts(caplog):
    return [json.loads(r.getMessage().split(TAG, 1)[1]) for r in caplog.records if TAG in r.getMessage()]


def capture(monkeypatch, caplog, svc):
    caplog.set_level(logging.INFO, logger="oms-risk")
    monkeypatch.setattr(svc.logger, "handlers", [*svc.logger.handlers, caplog.handler])


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
@pytest.mark.parametrize("session", ["AM", "NORMAL", "PM"])
async def test_one_real_timing_line_separates_protection_ack_and_transport(monkeypatch, caplog, account, session):
    svc, _, broker = setup(monkeypatch, account, session=session)
    capture(monkeypatch, caplog, svc)
    svc.settings.oms_v2_exit_release_reservation_enabled = True
    if account == WEBULL_ACCT:
        original_guard = svc._prepare_confirmation_webull_leg
        async def guard(*args, **kwargs):
            await asyncio.sleep(.015)
            return await original_guard(*args, **kwargs)
        monkeypatch.setattr(svc, "_prepare_confirmation_webull_leg", guard)
        expected_guard = "_prepare_confirmation_webull_leg"
    else:
        async def guard(**kwargs):
            await asyncio.sleep(.015)
            return ExitPairReleaseResult(outcome="released")
        monkeypatch.setattr(svc, "_release_exit_reservation_before_close", guard)
        expected_guard = "_release_exit_reservation_before_close"
    original_submit = broker.submit_order
    async def submit(request):
        await asyncio.sleep(.015)
        reports = await original_submit(request)
        await asyncio.sleep(.015)  # Report construction is not local OMS report receipt.
        return reports
    monkeypatch.setattr(broker, "submit_order", submit)
    await svc._handle_quote_tick_event(quote("2.64"))
    await drain(svc)
    receipt, = receipts(caplog)
    request, = broker.requests
    assert receipt["outcome"] == "broker_accepted"
    assert receipt["session"] == session
    assert receipt["order_type"] == "limit" and receipt["limit_price"] == "2.62"
    assert request.order_type == "limit" and request.quantity == 120
    assert datetime.fromisoformat(receipt["decision_at"]) < datetime.fromisoformat(receipt["submit_at"])
    assert datetime.fromisoformat(receipt["submit_at"]) < datetime.fromisoformat(receipt["broker_report_at"])
    assert datetime.fromisoformat(receipt["broker_report_at"]) < datetime.fromisoformat(receipt["broker_ack_at"])
    assert receipt["broker_ack_at"] == receipt["transport_return_at"]
    assert receipt["broker_ack_definition"] == "local_oms_receipt_of_bound_broker_ack_evidence"
    assert next(g for g in receipt["guards"] if g["name"] == expected_guard)["elapsed_ms"] >= 10
    assert receipt["decision_to_submit_ms"] >= 10 and receipt["submit_to_ack_ms"] >= 20


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
@pytest.mark.parametrize("mode,expected", [("rejected", "broker_rejected"), ("client", "client_refusal"),
    ("unknown", "unclassified_report"), ("empty", "transport_return"), ("error", "error"),
    ("old_report", "broker_accepted"), ("wrong_order", "unmatched_report")])
async def test_ack_requires_broker_source_and_exact_attempt_not_old_report_time(monkeypatch, caplog, account, mode, expected):
    svc, _, broker = setup(monkeypatch, account)
    capture(monkeypatch, caplog, svc)
    original = broker.submit_order
    async def submit(request):
        if mode == "error":
            raise RuntimeError("controlled transport failure")
        reports = await original(request)
        if mode == "empty":
            return []
        if mode == "old_report":
            return [replace(reports[0], reported_at=datetime.now(UTC)-timedelta(days=1))]
        if mode == "wrong_order":
            return [replace(reports[0], client_order_id="another-attempt")]
        return [replace(reports[0], event_type="rejected" if mode in {"client", "rejected"} else "accepted",
                        origin={"client": "client", "unknown": "unknown"}.get(mode, "broker"))]
    monkeypatch.setattr(broker, "submit_order", submit)
    request = OrderRequest(client_order_id="controlled-receipt", broker_account_name=account,
        strategy_code="schwab_1m_v2", symbol="VSME", side="sell", intent_type="close",
        quantity=Decimal(120), reason="oms_v2_managed_exit:CW_TARGET", order_type="limit",
        metadata={"cw_decided_at": datetime.now(UTC).isoformat(), "limit_price": "2.62", "session": "NORMAL"})
    if mode == "error":
        with pytest.raises(RuntimeError, match="controlled transport failure"):
            await svc._submit_cw_target_order(request, [])
    else:
        await svc._submit_cw_target_order(request, [])
    receipt, = receipts(caplog)
    assert receipt["outcome"] == expected
    if mode in {"rejected", "old_report"}:
        assert receipt["broker_ack_at"] == receipt["transport_return_at"]
        if mode == "old_report":
            assert receipt["broker_ack_at"] != receipt["broker_report_at"]
    else:
        assert receipt["broker_ack_at"] is None
    if mode == "error":
        assert receipt["transport_return_at"] is None and receipt["error_type"] == "RuntimeError"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
async def test_known_protection_block_has_no_fictitious_wire_or_ack(monkeypatch, caplog, account):
    svc, _, broker = setup(monkeypatch, account)
    capture(monkeypatch, caplog, svc)
    svc.settings.oms_v2_exit_release_reservation_enabled = True
    if account == WEBULL_ACCT:
        async def blocked(*args, **kwargs):
            return "unanswerable"
        monkeypatch.setattr(svc, "_prepare_confirmation_webull_leg", blocked)
        expected = "_prepare_confirmation_webull_leg"
    else:
        async def blocked(**kwargs):
            return ExitPairReleaseResult(outcome="reserved")
        monkeypatch.setattr(svc, "_release_exit_reservation_before_close", blocked)
        expected = "_release_exit_reservation_before_close"
    await svc._handle_quote_tick_event(quote("2.64"))
    await drain(svc)
    receipt, = receipts(caplog)
    assert receipt["outcome"] == "not_submitted" and receipt["blocked_guard"] == expected
    assert receipt["submit_at"] is None and receipt["broker_ack_at"] is None
    assert receipt["client_order_id"] is None and broker.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
@pytest.mark.parametrize("gate", ["eod", "binding"])
async def test_preintent_gates_have_no_fictitious_submission(monkeypatch, caplog, account, gate):
    svc, _, broker = setup(monkeypatch, account)
    capture(monkeypatch, caplog, svc)
    svc._capture_cw_target_decision(account, "VSME", {"bid": 2.64, "received_at": datetime.now(UTC)})
    context = svc._cw_target_decision_context(svc._cw_target_decisions[(account, "VSME")])
    async def ready(*args):
        return gate != "eod"
    monkeypatch.setattr(svc, "_v2_eod_handover_ready", ready)
    if gate == "binding":
        context["cw_target_entry_price"] = "999"
    if account == WEBULL_ACCT:
        outcome = await svc._webull_cw_exit_on_shared_path(account, "VSME", tag="CW_TARGET",
            ref=2.6145, bid=2.64, expected_row_id=context["cw_target_bound_row_id"], decision_context=context)
    else:
        outcome = await svc._emit_v2_exit_on_loop_claimed(account, "VSME", None, 2.49,
            kind="CLOSE", reference_price=2.6145, reason="oms_v2_managed_exit:CW_TARGET",
            bid=2.64, close_on_fill=True, confirmation_context=context)
    receipt, = receipts(caplog)
    assert outcome == ("eod_handover_unconfirmed" if gate == "eod" else
        "refused_before_release" if account == WEBULL_ACCT else "refused")
    assert receipt["blocked_guard"] == ("_v2_eod_handover_ready" if gate == "eod"
        else "_cw_target_binding_matches")
    assert broker.requests == [] and receipt["outcome"] == "not_submitted"
    assert receipt["submit_at"] is None and receipt["broker_ack_at"] is None
    assert receipt["client_order_id"] is None and receipt["transport_return_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
async def test_target_quote_intake_remains_sql_free_with_receipt(monkeypatch, caplog, account):
    svc, sf, _ = setup(monkeypatch, account)
    entered, release = asyncio.Event(), asyncio.Event()
    original, sql = svc._run_db, []
    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    def counted(*args):
        sql.append(1)
    monkeypatch.setattr(svc, "_run_db", paused)
    engine = sf.kw["bind"]
    event.listen(engine, "before_cursor_execute", counted)
    try:
        await svc._handle_quote_tick_event(quote("2.64"))
        await entered.wait()
        assert sql == [] and receipts(caplog) == []
    finally:
        event.remove(engine, "before_cursor_execute", counted)
        release.set()
        await drain(svc)


@pytest.mark.asyncio
@pytest.mark.parametrize("account,mode", [(ACCT, mode) for mode in
    ["positive", "empty", "http200", "no_location", "wrong_account", "rejected"]] +
    [(WEBULL_ACCT, mode) for mode in ["positive", "empty", "wrong_identity", "rejected"]])
@pytest.mark.parametrize("target", [True, False])
async def test_real_adapter_origin_unchanged_positive_ack_metadata_only(monkeypatch, caplog, fake_sdk, account, mode, target):
    request = OrderRequest(client_order_id="controlled-adapter", broker_account_name=account,
        strategy_code="schwab_1m_v2", symbol="VSME", side="sell", intent_type="close",
        quantity=Decimal(120), reason="oms_v2_managed_exit:CW_TARGET" if target else "oms_v2_managed_exit:CW_HARD_STOP",
        order_type="limit", metadata={"limit_price": "2.62", "session": "NORMAL",
            "cw_target_api_received_at": "yesterday",
            "cw_target_api_ack_client_order_id": "controlled-adapter",
            "cw_target_api_ack_evidence": "webull_matching_client_order_id"})
    if account == ACCT:
        adapter = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
        adapter.settings = SimpleNamespace(schwab_native_bracket_enabled=False)
        adapter.accounts_by_name = {account: SimpleNamespace(account_hash="controlled")}
        async def response(*args, **kwargs):
            await asyncio.sleep(.005)
            status = 400 if mode == "rejected" else 200 if mode in {"http200", "empty"} else 201
            location = "https://api.schwabapi.com/trader/v1/accounts/controlled/orders/123"
            if mode == "wrong_account":
                location = location.replace("controlled", "another-account")
            headers = {} if mode in {"no_location", "empty"} else {"Location": location}
            return status, headers, {}
        monkeypatch.setattr(adapter, "_authorized_request_json", response)
        monkeypatch.setattr(adapter, "_should_return_accepted_immediately", lambda request: True)
    else:
        body = {} if mode == "empty" else {"client_order_id":
            "another-attempt" if mode == "wrong_identity" else request.client_order_id}
        client = _FakeClient({"place": body})
        if mode == "rejected":
            client.raises["place"] = _ServerException("CONTROLLED_REJECT", "CONTROLLED_REJECT", 417)
        adapter = _adapter(client)
        adapter.accounts_by_name[account] = adapter.accounts_by_name["live:orb"]
        monkeypatch.setattr(adapter, "_resolve_instrument_id", lambda *args: "controlled-instrument")
    before = datetime.now(UTC)
    if target:
        svc, _, _ = setup(monkeypatch, account)
        capture(monkeypatch, caplog, svc)
        original_submit = adapter.submit_order
        async def delayed_return(request):
            reports = await original_submit(request)
            await asyncio.sleep(.01)
            return reports
        monkeypatch.setattr(adapter, "submit_order", delayed_return)
        svc.broker_adapter = adapter
        report, = await svc._submit_cw_target_order(request, [])
    else:
        report, = await adapter.submit_order(request)
    after = datetime.now(UTC)
    assert report.event_type == ("rejected" if mode == "rejected" else "accepted")
    assert before <= report.reported_at <= after
    assert report.origin == ("broker" if mode == "rejected" else "unknown")
    if not target:
        assert report.metadata.get("cw_target_api_received_at") == "yesterday"
        return  # Non-target metadata and original origin are untouched.
    receipt, = receipts(caplog)
    if mode == "positive":
        api_at = datetime.fromisoformat(receipt["adapter_api_ack_at"])
        assert before <= api_at <= report.reported_at <= after
        assert receipt["broker_ack_at"] == receipt["transport_return_at"]
        assert datetime.fromisoformat(receipt["broker_ack_at"]) - api_at >= timedelta(milliseconds=8)
        assert receipt["outcome"] == "broker_accepted"
        assert receipt["broker_ack_evidence"] == ("schwab_http_201_location" if account == ACCT
            else "webull_matching_client_order_id")
    elif mode == "rejected":
        assert receipt["outcome"] == "broker_rejected" and receipt["broker_ack_at"] is not None
        assert receipt["adapter_api_ack_at"] is None
    else:
        assert receipt["broker_ack_at"] is None and receipt["adapter_api_ack_at"] is None
        assert before <= datetime.fromisoformat(receipt["transport_return_at"]) <= after
        assert receipt["outcome"] == "unclassified_report"
        assert "cw_target_api_ack_evidence" not in report.metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
@pytest.mark.parametrize("failure", ["builder", "sink"])
@pytest.mark.parametrize("raises", [False, True])
async def test_receipt_failure_is_inert_for_reports_and_original_exception(monkeypatch, account, failure, raises):
    from project_mai_tai.oms import service as module
    svc, _, broker = setup(monkeypatch, account)
    original = broker.submit_order
    expected, error = [], RuntimeError("original adapter error")
    async def submit(request):
        if raises:
            raise error
        expected.extend(await original(request))
        return expected
    def broken(*args, **kwargs):
        raise ValueError("controlled telemetry failure")
    monkeypatch.setattr(broker, "submit_order", submit)
    if failure == "builder":
        monkeypatch.setattr(module, "target_exit_receipt", broken)
    else:
        monkeypatch.setattr(svc.logger, "info", broken)
    request = OrderRequest(client_order_id="controlled-inert", broker_account_name=account,
        strategy_code="schwab_1m_v2", symbol="VSME", side="sell", intent_type="close",
        quantity=Decimal(120), reason="oms_v2_managed_exit:CW_TARGET", order_type="limit",
        metadata={"limit_price": "2.62", "cw_decided_at": datetime.now(UTC).isoformat()})
    if raises:
        with pytest.raises(RuntimeError) as caught:
            await svc._submit_cw_target_order(request, [])
        assert caught.value is error
    else:
        assert await svc._submit_cw_target_order(request, []) is expected
        assert broker.requests == [request] and request.metadata["limit_price"] == "2.62"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
@pytest.mark.parametrize("owned", [0, 119])
async def test_owned_cap_and_refusal_remain_authority_with_receipt(monkeypatch, caplog, account, owned):
    from project_mai_tai.db.models import VirtualPosition
    svc, sf, broker = setup(monkeypatch, account)
    capture(monkeypatch, caplog, svc)
    with sf.begin() as session:
        strategy = svc.store.ensure_strategy(session, "schwab_1m_v2")
        acct = svc.store.ensure_broker_account(session, account, provider="simulated", environment="paper")
        session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=acct.id,
            symbol="VSME", quantity=Decimal(owned), average_price=Decimal("2.49")))
    await emit_bound_target(svc, sf, account)
    receipt, = receipts(caplog)
    if owned:
        request, = broker.requests
        assert request.quantity == owned and receipt["quantity"] == str(owned)
        assert request.metadata["limit_price"] == "2.62"
    else:
        assert broker.requests == [] and receipt["submit_at"] is None
        assert receipt["blocked_guard"] == "_v2_managed_sell_owned_quantity"


async def emit_bound_target(svc, sf, account):
    from tests.unit.test_edgeprot1_target_cache import row
    svc._capture_cw_target_decision(account, "VSME", {"bid": 2.64, "received_at": datetime.now(UTC)})
    context = svc._cw_target_decision_context(svc._cw_target_decisions[(account, "VSME")])
    with sf() as session:
        result = await svc._emit_v2_managed_sell(session, row(session, account), intent_type="close",
            quantity=120, reference_price=2.6145, reason="oms_v2_managed_exit:CW_TARGET",
            bid=2.64, confirmation_context=context)
        session.commit()
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
@pytest.mark.parametrize("failure", ["builder", "sink"])
@pytest.mark.parametrize("owned", [0, 119])
async def test_receipt_failure_preserves_full_owned_order_and_refusal_path(monkeypatch, account, failure, owned):
    from project_mai_tai.db.models import VirtualPosition
    from project_mai_tai.oms import service as module
    svc, sf, broker = setup(monkeypatch, account)
    with sf.begin() as session:
        strategy = svc.store.ensure_strategy(session, "schwab_1m_v2")
        acct = svc.store.ensure_broker_account(session, account, provider="simulated", environment="paper")
        session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=acct.id,
            symbol="VSME", quantity=Decimal(owned), average_price=Decimal("2.49")))
    def broken(*args, **kwargs):
        raise ValueError("controlled telemetry failure")
    if failure == "builder":
        monkeypatch.setattr(module, "target_exit_receipt", broken)
    else:
        original = svc.logger.info
        def sink(message, *args, **kwargs):
            if TAG.strip() in message:
                broken()
            return original(message, *args, **kwargs)
        monkeypatch.setattr(svc.logger, "info", sink)
    result = await emit_bound_target(svc, sf, account)
    if owned:
        request, = broker.requests
        assert request.order_type == "limit" and request.metadata["limit_price"] == "2.62"
        assert request.quantity == 119 and result.submitted_quantity == 119 and not result.ownership_refused
    else:
        assert broker.requests == [] and not result and result.ownership_refused
