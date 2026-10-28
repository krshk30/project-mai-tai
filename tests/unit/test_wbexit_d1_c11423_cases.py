"""Existing WBEXIT1 fixture, not a new runner or a native-preimage replay.

AIOK quantity/entry bind to retained logs. Its physical child geometry is a
CONTROLLED tick-rounded adapter model, not recovered native child payloads.
SPRC quantity/bid/clocks bind to the incident; entry price and DB rows are
explicitly modeled. No permission, owner, STOP or freshness guard is mocked.
All cases in this successor are SOURCE ONLY / UNRUN.
"""

from dataclasses import replace
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from threading import Event
from time import monotonic

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, ExitPairReleaseResult
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter, WebullPositionsUnavailable
from project_mai_tai.db.models import (
    AccountPosition, BrokerOrder, Fill, OmsManagedPosition, SystemIncident, TradeIntent, VirtualPosition,
)
from project_mai_tai.oms import service as oms
from project_mai_tai.oms import webull_attach_riskoff as wb
from project_mai_tai.settings import Settings
from tests.unit import test_confirmation_exit_fanout as fanout_fixture
from tests.unit.test_wbattach1_riskoff import closes, make_case as make_case
from tests.wbexit1_application_harness import (
    CANONICAL_EXTENDED_SESSION, ExitPort, call_exit, drain_recovery,
    installed_case, retained_case,
)


class D1ExitPort(ExitPort):
    """Phase-correct replies at the existing fixture's broker I/O boundary."""

    async def list_account_positions(self, account):
        started = monotonic()
        rows = await super().list_account_positions(account)
        if not oms._is_regular_market_session():
            self.wire_started = started  # Positive PM neighbour has an actual controlled fresh read.
        return rows

    async def cancel_exit_pair(self, **binding):
        self.observations.append((monotonic(), "first_cancel", binding))
        return await super().cancel_exit_pair(**binding)

    async def confirm_exit_pair_terminal(self, **binding):
        base = binding["base_client_order_id"]
        pairs = [r for r in self.requests if r.client_order_id == base
                 and r.metadata.get("webull_exit_only_pair") == "true"]
        assert len(pairs) == 1, "fixture has no unique physical pair request"
        pair = pairs[0]
        if base in self.cancelled:
            return await super().confirm_exit_pair_terminal(**binding)
        reports = []
        for suffix, key, native in (("T", "bracket_target_price", "limit_price"),
                                    ("S", "bracket_stop_price", "stop_price")):
            price = WebullBrokerAdapter._round_to_tick(Decimal(pair.metadata[key]))
            report = self.report(WebullBrokerAdapter._combo_leg_coid(base, suffix), event="accepted")
            reports.append(replace(report, quantity=pair.quantity, metadata={
                **report.metadata, "wbexit1_geometry": json.dumps({
                    native: str(price), "status": "WORKING"})}))
        return ExitPairReleaseResult(outcome="reserved", reports=tuple(reports))

    async def confirm_exit_pair_from_today_list(self, **binding):
        result = await super().confirm_exit_pair_from_today_list(**binding)
        self.observations.append((monotonic(), "child_census", result))
        return result

    async def read_exit_order_status(self, request, *, after):
        started = monotonic()
        assert after < started and request in self.requests
        reports = [r for _, action, batch in self.observations if action == "sell_report"
                   for r in batch if r.client_order_id == request.client_order_id]
        assert reports, "no controlled execution/status witness for OUR exact order"
        self.observations.append((started, "our_sell_status", request))
        return replace(reports[-1], quantity=request.quantity, reported_at=oms.utcnow()), started

    async def submit_order(self, request):
        call = wb._ATR_CALL.get()
        if call is not None and call.recovery_request is request:
            # The changed contract intentionally wires BEFORE the DB record.
            # This is the physical adapter fixture, not an APP owner/lease mock.
            assert call.task is asyncio.current_task() and call.owner_key
            assert call.released_quantity is not None and call.released_proof
            assert call.recovery[4] == frozenset((r.client_order_id, r.broker_order_id)
                                               for r in call.released_proof)
            assert request.quantity <= call.released_quantity
            self.requests.append(request)
            self.observations.append((monotonic(), "reprotect_wire", request))
            event = "rejected" if getattr(self, "refuse_reprotect", False) else "accepted"
            return [replace(self.report(request.client_order_id, event=event), quantity=request.quantity)]
        return await super().submit_order(request)


def assert_filled_owned_only(case, expected):
    port = case[2]
    requests = closes(port)
    assert len(requests) == 1 and requests[0].quantity == Decimal(expected)
    reports = [r for _, action, batch in port.observations if action == "sell_report" for r in batch]
    assert len(reports) == 1 and reports[0].origin == "broker"
    assert reports[0].event_type == "filled" and reports[0].filled_quantity == Decimal(expected)
    assert reports[0].broker_order_id and reports[0].broker_fill_id
    assert port.total == port.manual_quantity
    with case[1]() as session:
        row = session.scalar(select(OmsManagedPosition))
        fills = session.scalars(select(Fill).where(Fill.side == "sell")).all()
        assert row.status == "closed" and row.current_quantity == 0
        assert sum((f.quantity for f in fills), Decimal(0)) == Decimal(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True], ids=["D1-OFF-installed-refusal", "D1-ON-owned-fill"])
async def test_aiok_shared_first_release_sells_owned_27_within_original_budget(make_case, enabled):
    # Stale RTH cache is a CONTROLLED witness for the actual logged predicate,
    # not the lost native request/response timestamps. OFF retains that refusal.
    case = await retained_case(make_case, "AIOK", port_type=D1ExitPort, d1_enabled=enabled)
    outcome, _ = await call_exit(case, "CONFIRMATION_EXIT")
    await drain_recovery(case[0])
    if not enabled:
        assert outcome == "sell_refused_recovering"
        assert not closes(case[2]) and case[2].total == Decimal(1027)
        assert not any(action == "positions_wire" for _, action, _ in case[2].observations)
        with case[1]() as session:
            row = session.scalar(select(OmsManagedPosition))
            assert row.status == "open" and row.current_quantity == 27
        return
    assert outcome == "closed"
    assert_filled_owned_only(case, 27)
    observations = case[2].observations
    cancel_at = min(t for t, action, _ in observations if action == "first_cancel")
    censuses = [(t, proof) for t, action, proof in observations if action == "child_census"]
    assert any(t < cancel_at and proof.outcome == "reserved" for t, proof in censuses)
    terminal_at = max(t for t, proof in censuses if proof.outcome == "released")
    wire_at = [t for t, action, _ in observations if action == "positions_wire"]
    assert len(wire_at) == 1 and wire_at[0] > terminal_at
    filled_at = max(t for t, action, _ in observations if action == "sell_report")
    assert 0 <= filled_at - cancel_at <= 10
    assert len(case[2].cancelled) == 1
    assert len([r for r in case[2].requests if r.metadata.get("webull_exit_only_pair") == "true"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True], ids=["D1-OFF-PM", "D1-ON-PM"])
async def test_sprc_pm_software_held_neighbour_still_sells_196_without_pair(make_case, monkeypatch, enabled):
    case = await installed_case(make_case, attach_pair=False, port_type=D1ExitPort, d1_enabled=enabled)
    service, factory, port, entry_id, clock = case
    now = datetime(2026, 10, 26, 11, 53, 11, 69000, tzinfo=UTC)
    entered = datetime(2026, 10, 26, 11, 22, 10, tzinfo=UTC)
    owned, modeled_entry = Decimal(196), Decimal("1.50")
    port.symbol, port.owned, port.total = "SPRC", owned, port.manual_quantity + owned
    port.reference_bid = port.quote_bid = 1.49
    with factory.begin() as session:
        entry = session.get(BrokerOrder, entry_id)
        entry.symbol, entry.quantity = "SPRC", owned
        entry.created_at = entry.submitted_at = entered
        entry.payload = {**entry.payload, "wbexit1_fixture_provenance": {
            "kind": "CONTROLLED_PM_NEIGHBOUR_NOT_NATIVE_REPLAY",
            "native_preimage": "UNKNOWN", "entry_price": "MODELED_1.50_NOT_RETAINED"}}
        for intent in session.scalars(select(TradeIntent)):
            intent.symbol, intent.quantity, intent.created_at = "SPRC", owned, entered
        for fill in session.scalars(select(Fill)):
            fill.symbol, fill.quantity, fill.price, fill.filled_at = "SPRC", owned, modeled_entry, entered
        for position in session.scalars(select(VirtualPosition)):
            position.symbol, position.quantity, position.average_price = "SPRC", owned, modeled_entry
        for position in session.scalars(select(AccountPosition)):
            position.symbol, position.quantity = "SPRC", port.total
            position.average_price, position.source_updated_at = modeled_entry, now - timedelta(seconds=1)
        row = session.scalar(select(OmsManagedPosition))
        row.symbol, row.entry_price, row.entry_time = "SPRC", modeled_entry, entered
        row.original_quantity = row.current_quantity = int(owned)
    clock[0] = now
    monkeypatch.setattr(oms, "_extended_hours_session", CANONICAL_EXTENDED_SESSION)
    outcome, _ = await call_exit(case, "CW_HARD_STOP")
    await drain_recovery(service)
    assert outcome == "closed"
    assert_filled_owned_only(case, 196)
    assert not port.cancelled and not any(action == "first_cancel" for _, action, _ in port.observations)
    assert len(port.requests) == 1 and port.requests[0].order_type == "limit"
    assert port.requests[0].metadata["extended_hours"] == "true"
    assert not any(r.metadata.get("webull_exit_only_pair") == "true" for r in port.requests)


@pytest.mark.asyncio
async def test_default_off_retains_existing_schwab_confirmation_case_with_live_features_on(monkeypatch):
    monkeypatch.setattr(fanout_fixture, "datetime", oms.datetime)
    defaults = Settings(_env_file=None)
    assert defaults.strategy_schwab_1m_v2_wbexit1_d1_enabled is False
    original_factory = fanout_fixture._service
    services = []

    def factory(*args, **kwargs):
        service, sessions = original_factory(*args, **kwargs)
        object.__setattr__(service.settings, "strategy_schwab_1m_v2_keepwatch1_enabled", True)
        object.__setattr__(service.settings, "strategy_schwab_1m_v2_brokerdecides1_enabled", True)
        services.append(service)
        return service, sessions

    # Fixture setup only: the existing application gates/emitter are unmodified.
    monkeypatch.setattr(fanout_fixture, "_service", factory)
    await fanout_fixture.test_controlled_fanout_flag_pair_closes_only_managed_accounts(
        monkeypatch, False, [fanout_fixture.SCHWAB])
    service, = services
    assert service.settings.strategy_schwab_1m_v2_wbexit1_d1_enabled is False
    assert service.settings.strategy_schwab_1m_v2_keepwatch1_enabled is True
    assert service.settings.strategy_schwab_1m_v2_brokerdecides1_enabled is True
    request, = service.broker_adapter.submitted
    assert request.broker_account_name == fanout_fixture.SCHWAB
    assert not any("wbexit1" in key or "shared_rth" in key for key in request.metadata)
    assert not service.broker_adapter.cancel_pair_calls


class RecoveryFaultPort(D1ExitPort):
    fault = "unreadable"
    partial = Decimal(0)
    unreadable_order_status = False

    async def read_exit_positions(self, account, *, after):
        if self.partial and not closes(self):
            return await super().read_exit_positions(account, after=after)
        self.observations.append((monotonic(), "positions_failure", self.fault))
        if self.fault == "timeout":
            await asyncio.sleep(3)  # APP's original two-second read timeout wins.
        raise WebullPositionsUnavailable("CONTROLLED exact positions read unavailable")

    async def submit_order(self, request):
        if request.metadata.get("webull_exit_only_pair") == "true" or not self.partial:
            return await super().submit_order(request)
        self.requests.append(request)
        self.observations.append((monotonic(), "sell_invoked", request))
        self.total -= self.partial
        report = replace(self.report(request.client_order_id, event="cancelled", filled=self.partial),
                         quantity=request.quantity, reported_at=oms.utcnow())
        self.observations.append((monotonic(), "sell_report", [report]))
        # Controlled post-dispatch I/O failure, NOT a retained native chronology.
        # The real APP finally must read OUR terminal order, not account holdings.
        await self.read_exit_positions(self.account, after=monotonic())
        raise AssertionError("fault unexpectedly returned")

    async def read_exit_order_status(self, request, *, after):
        if self.unreadable_order_status:
            raise WebullPositionsUnavailable("CONTROLLED OUR order status unavailable")
        return await super().read_exit_order_status(request, after=after)


def recovery_requests(port):
    return [request for _, action, request in port.observations if action == "reprotect_wire"]


@pytest.fixture
def capture_service_log(caplog):
    bindings = []

    def bind(service):
        # configure_logging(force=True) removes pytest's root capture handler.
        # Reuse WBATTACH's direct-handler capture, without altering APP logging.
        logger = service.logger
        before = (tuple(logger.handlers), logger.propagate, logger.level)
        added = caplog.handler not in logger.handlers
        if added:
            logger.addHandler(caplog.handler)
        bindings.append((logger, before, added))

    yield bind
    for logger, before, added in reversed(bindings):
        if added:
            logger.removeHandler(caplog.handler)
        assert (tuple(logger.handlers), logger.propagate, logger.level) == before


def assert_original_reprotect(case, expected):
    port = case[2]
    request, = recovery_requests(port)
    original = port.requests[0]
    assert request.quantity == Decimal(expected)
    assert request.client_order_id != original.client_order_id
    for key in ("bracket_target_price", "bracket_stop_price"):
        # Original metadata has four decimals; native child prices are tick-rounded.
        assert Decimal(request.metadata[key]) == WebullBrokerAdapter._round_to_tick(Decimal(original.metadata[key]))
    cancelled_at = min(t for t, action, _ in port.observations if action == "first_cancel")
    wire_at, = [t for t, action, _ in port.observations if action == "reprotect_wire"]
    assert 0 <= wire_at - cancelled_at <= 10
    proofs = [(t, result) for t, action, result in port.observations
              if action == "child_census" and result.outcome == "reserved"
              and any(r.client_order_id == WebullBrokerAdapter._combo_leg_coid(request.client_order_id, "T")
                      for r in result.reports)]
    assert len(proofs) == 1 and 0 <= proofs[0][0] - cancelled_at <= 10
    assert len(port.cancelled) == 1, "no stack/cancel retry permitted"


@pytest.mark.asyncio
async def test_d1_terminal_positions_timeout_reprotects_original_levels_within_ten_seconds(make_case):
    class TimeoutPort(RecoveryFaultPort):
        fault = "timeout"
    case = await retained_case(make_case, "AIOK", port_type=TimeoutPort, d1_enabled=True)
    await call_exit(case, "CONFIRMATION_EXIT")
    assert not closes(case[2])
    assert_original_reprotect(case, 27)
    assert case[2].total == case[2].manual_quantity + Decimal(27)


@pytest.mark.asyncio
async def test_d1_read_and_protect_refusal_pages_unknown_without_second_order(make_case, caplog, capture_service_log):
    case = await retained_case(make_case, "AIOK", port_type=RecoveryFaultPort, d1_enabled=True)
    capture_service_log(case[0])
    case[2].refuse_reprotect = True
    await call_exit(case, "CONFIRMATION_EXIT")
    assert len(recovery_requests(case[2])) == 1 and not closes(case[2])
    assert "protection refusal/ACK UNKNOWN; no second order" in caplog.text
    assert "scan_exception=true" in caplog.text
    assert "[WBEXIT1-REPROTECTED]" not in caplog.text
    with case[1]() as session:
        incident = session.scalar(select(SystemIncident))
        assert incident is not None and "UNKNOWN" in incident.payload.get("d1_exception", incident.payload["reason"])


@pytest.mark.asyncio
async def test_d1_held_claim_commit_wires_before_join_then_pages_late_record(make_case, monkeypatch, caplog, capture_service_log):
    case = await retained_case(make_case, "AIOK", port_type=D1ExitPort, d1_enabled=True)
    service, _, port, _, _ = case
    capture_service_log(service)
    original = service._run_db
    entered, release, finished = Event(), Event(), Event()
    handles = []

    async def held_run_db(fn, *, commit=True):
        values = dict(zip(fn.__code__.co_freevars, (cell.cell_contents for cell in fn.__closure__ or ())))
        mutate = values.get("mutate")
        if getattr(mutate, "__name__", "") != "claim" or entered.is_set():
            return await original(fn, commit=commit)
        first_cancel = min(t for t, action, _ in port.observations if action == "first_cancel")
        handles.append(asyncio.get_running_loop().call_later(max(0, first_cancel + 9.25 - monotonic()), release.set))
        def blocked(session):
            result = fn(session)
            entered.set()
            assert release.wait(12), "controlled physical commit was never released"
            return result  # The real service commits AFTER this returns.
        result = await original(blocked, commit=commit)
        finished.set()
        return result

    monkeypatch.setattr(service, "_run_db", held_run_db)
    try:
        await call_exit(case, "CONFIRMATION_EXIT")
        assert entered.is_set() and finished.is_set(), "physical commit must be joined, not abandoned"
        assert not closes(port)
        assert_original_reprotect(case, 27)
        assert "UNPROVEN record; physical commit still open" in caplog.text
        assert "scan_exception=true" in caplog.text
    finally:
        release.set()
        for handle in handles:
            handle.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_missing", [False, True], ids=["terminal-partial-exact-remainder", "OUR-status-UNKNOWN-no-order"])
async def test_d1_partial_our_sell_positions_failure_uses_only_order_status(make_case, caplog, status_missing, capture_service_log):
    class PartialPort(RecoveryFaultPort):
        partial = Decimal(7)
        unreadable_order_status = status_missing
    case = await retained_case(make_case, "AIOK", port_type=PartialPort, d1_enabled=True)
    if status_missing:
        capture_service_log(case[0])
    await call_exit(case, "CONFIRMATION_EXIT")
    assert len(closes(case[2])) == 1
    assert case[2].total == case[2].manual_quantity + Decimal(20)
    assert any(action == "positions_failure" for _, action, _ in case[2].observations)
    if status_missing:
        assert not recovery_requests(case[2])
        assert "Webull exit unprotected,size unknown" in caplog.text
        assert "scan_exception=true" in caplog.text
    else:
        assert_original_reprotect(case, 20)
        status, = [r for _, action, r in case[2].observations if action == "our_sell_status"]
        assert status.client_order_id == closes(case[2])[0].client_order_id
