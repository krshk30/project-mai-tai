"""UNRUN LAB diagnostics controls, not retained CHOW/native causal replay.

Compare actual candidate methods with exact 8159 source methods using the same
explicit CONTROLLED boundaries. No DB, provider, Redis, service run or sale.
"""
import ast
import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
import inspect
import os
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from project_mai_tai.broker_adapters.protocols import ExecutionReport, ExitPairReleaseResult
from project_mai_tai.oms import eod_oco_step_log as diagnostic
from project_mai_tai.oms import service as module
from project_mai_tai.oms.service import OmsRiskService
from tests.unit import eod_oco_8159_fixture as retained

ACCOUNT, SYMBOL, ROW, ENTRY = "live:orb", "CHOW", "CONTROLLED-managed", "CONTROLLED-entry"
BASE = "schwab_1m_v2-CHOW-protect-3f84a37f10f2"
NOW = datetime(2026, 10, 30, 20, 1, tzinfo=UTC)
RELEASE_MODES = ["non_webull", "legacy_precheck", "no_row", "wrong_row", "no_base",
    "release_fill", "legacy_after_release", "confirm_missing", "confirm_unknown", "confirm_released",
    "legacy_after_confirm", "base_error", "release_error", "confirm_error", "release_cancelled", "confirm_reserved"]
TRANSITION_MODES = ["fill_recording_false", "fill_recording_true", "current_row_error",
                    "released_stable", "current_row_changed", "unanswerable"]


class Log:
    def __init__(self, fail=False):
        self.fail, self.steps, self.business = fail, [], []

    def info(self, template, *args):
        if template.startswith("[OMS-V2-EOD-OCO-STEP]"):
            if self.fail:
                raise RuntimeError("CONTROLLED diagnostic sink failure")
            self.steps.append(template % args)
        else:
            self.business.append(("info", template % args))

    def error(self, template, *args): self.business.append(("error", template % args))
    def warning(self, template, *args): self.business.append(("warning", template % args))
    def exception(self, template, *args): self.business.append(("exception", template % args))


def reports():
    return tuple(ExecutionReport(event_type="cancelled", client_order_id=BASE + suffix,
        broker_order_id="CONTROLLED-native-" + suffix, origin="broker", symbol=SYMBOL,
        side="sell", intent_type="cancel", quantity=Decimal(64), filled_quantity=Decimal(0),
        metadata={"cancel_outcome": "confirmed"}, reported_at=NOW) for suffix in ("T", "S"))


def release_driver(mode, *, fail=False):
    trace, log = [], Log(fail)
    row = None if mode == "no_row" else SimpleNamespace(id="OTHER" if mode == "wrong_row" else ROW)
    entry = SimpleNamespace(id=ENTRY, broker_order_id="CONTROLLED-entry-native")
    legacy_calls = 0

    async def legacy(*args, **kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        trace.append(("legacy", legacy_calls, deepcopy(kwargs)))
        return (mode, legacy_calls) in {("legacy_precheck", 1), ("legacy_after_release", 2),
                                       ("legacy_after_confirm", 3)}

    async def db(work, *, commit):
        trace.append(("db", work.__name__, commit))
        if mode == "base_error": raise RuntimeError("CONTROLLED base DB boundary")
        return work(None)

    def get_row(*args, **kwargs): trace.append(("row",)); return row
    def get_entry(*args): trace.append(("entry",)); return entry
    def base_for(*args, **kwargs): trace.append(("base",)); return "" if mode == "no_base" else BASE

    async def release(**kwargs):
        trace.append(("cancel_release", kwargs))
        if mode == "release_error": raise RuntimeError("CONTROLLED cancel boundary")
        if mode == "release_cancelled": raise asyncio.CancelledError()
        return ExitPairReleaseResult(outcome="resolved_by_fill" if mode == "release_fill" else "released",
                                     reports=reports())

    async def confirm(**kwargs):
        trace.append(("confirm", kwargs))
        if mode == "confirm_error": raise RuntimeError("CONTROLLED confirm boundary")
        outcome = "unanswerable" if mode == "confirm_unknown" else "reserved" if mode == "confirm_reserved" else "released"
        return ExitPairReleaseResult(outcome=outcome, reports=reports())

    async def native(*args, **kwargs): trace.append(("non_webull", args, kwargs)); return "released"
    adapter = SimpleNamespace(release_exit_pair_for_close=release)
    if mode != "confirm_missing": adapter.confirm_exit_pair_terminal = confirm
    svc = SimpleNamespace(logger=log, store=SimpleNamespace(get_open_managed_position=get_row),
        broker_adapter=adapter, _is_v2_webull_account=lambda account: mode != "non_webull",
        _legacy_webull_partial_handover_refused=legacy, _run_db=db, _find_oco_entry_order=get_entry,
        _oco_exit_base_for_entry=base_for, _release_native_oco_for_cw_flip=native)
    return svc, trace, log


async def run_release(methods, mode, *, fail=False):
    svc, trace, log = release_driver(mode, fail=fail)
    try:
        result = await methods._release_v2_eod_oco(svc, ACCOUNT, SYMBOL, expected_row_id=ROW)
    except asyncio.CancelledError:
        result = "CANCELLED_PROPAGATED"
    return result, trace, log


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", RELEASE_MODES)
async def test_release_branch_calls_returns_and_step_observations_match_8159(mode):
    old, old_trace, old_log = await run_release(retained.RetainedEod8159, mode)
    new, new_trace, new_log = await run_release(OmsRiskService, mode)
    assert new == old and new_trace == old_trace and new_log.business == old_log.business
    expected = {"non_webull": "non_webull_return", "legacy_precheck": "legacy_precheck_refused",
        "no_row": "base_row_missing_or_changed", "wrong_row": "base_row_missing_or_changed",
        "no_base": "base_missing", "release_fill": "release_resolved_by_fill",
        "legacy_after_release": "legacy_after_release_refused", "confirm_missing": "confirm_method_missing",
        "confirm_unknown": "confirmed_return", "confirm_released": "confirmed_return",
        "legacy_after_confirm": "legacy_after_confirm_refused", "base_error": "broker_read_or_cancel_error",
        "release_error": "broker_read_or_cancel_error", "confirm_error": "broker_read_or_cancel_error",
        "release_cancelled": "release_cancelled", "confirm_reserved": "confirmed_return"}[mode]
    assert any("step=" + expected + " " in line for line in new_log.steps)
    assert not old_log.steps
    if mode == "confirm_released":
        assert any("base=" + BASE in line and "release=released confirm=released" in line
                   and "cancelled/broker/0/confirmed" in line for line in new_log.steps)
    if mode in {"no_row", "wrong_row", "no_base", "legacy_precheck"}:
        assert not any(item[0] == "cancel_release" for item in new_trace)


def transition_driver(methods, mode, *, fail=False):
    trace, log = [], Log(fail)
    row = SimpleNamespace(id=ROW, strategy_code="schwab_1m_v2", broker_account_name=ACCOUNT, symbol=SYMBOL)
    snapshot = SimpleNamespace(managed_row_id=ROW)
    incident = SimpleNamespace(status="open", closed_at=None, payload={
        "source": OmsRiskService._EXIT_RELEASE_INCIDENT_SOURCE, "broker_account_name": ACCOUNT,
        "symbol": SYMBOL, "managed_row_id": ROW})

    class Session:
        def scalars(self, query):
            trace.append(("incident_query",))
            return SimpleNamespace(all=lambda: [incident])

    def get_row(*args, **kwargs): trace.append(("row",)); return row
    async def db(work, *, commit):
        trace.append(("db", work.__name__, commit))
        if work.__name__ == "_current_row_id":
            if mode == "current_row_error": raise RuntimeError("CONTROLLED post-confirm DB read")
            if mode == "current_row_changed": row.id = "CONTROLLED-new-episode"
        return work(Session())

    def read_snapshot(*args): trace.append(("snapshot",)); return snapshot
    async def release(*args, **kwargs):
        trace.append(("release", args, kwargs))
        outcome = "resolved_by_fill" if mode.startswith("fill_recording") else "unanswerable" if mode == "unanswerable" else "released"
        return ExitPairReleaseResult(outcome=outcome, reports=reports())
    async def close(*args, **kwargs): trace.append(("record_fill", args, kwargs)); return mode == "fill_recording_true"
    async def carry(*args): trace.append(("carry", args))
    async def evaluate(*args): trace.append(("evaluate", args))
    def sync_incident(*args, **kwargs): trace.append(("sync_incident", kwargs))
    svc = SimpleNamespace(logger=log, settings=SimpleNamespace(oms_v2_eod_oco_transition_enabled=True,
        oms_native_oco_resolve_grace_seconds=90), _v2_eod_oco_transition_due=lambda: True,
        _session_day_et=lambda: "2026-10-30", _managed_v2_symbols={(ACCOUNT, SYMBOL)},
        _native_oco_armed_confirmed_at={}, _native_oco_resolving={}, _v2_eod_oco_transition_rows={},
        _v2_eod_oco_transitioned=set(), _latest_quotes_by_symbol={}, _EXIT_RESERVATION_RETRY_SECONDS=15,
        _EXIT_RELEASE_INCIDENT_SOURCE=OmsRiskService._EXIT_RELEASE_INCIDENT_SOURCE,
        store=SimpleNamespace(get_open_managed_position=get_row), _run_db=db,
        _read_v2_managed_snapshot=read_snapshot, _release_v2_eod_oco=release,
        _close_resolved_oco_managed_row=close, _exit_pair_fill_detail=lambda release: {"CONTROLLED": True},
        _sync_exit_release_incident=sync_incident, _carry_v2_native_oco_floor_at_handover=carry,
        _evaluate_v2_managed_exit=evaluate)
    svc._sync_v2_eod_release_incident = MethodType(methods._sync_v2_eod_release_incident, svc)
    return svc, trace, log, incident


async def run_transition(methods, mode, *, fail=False):
    svc, trace, log, incident = transition_driver(methods, mode, fail=fail)
    await methods._v2_eod_oco_transition(svc)
    result = dict(latch=deepcopy(svc._v2_eod_oco_transition_rows),
        transitioned=deepcopy(svc._v2_eod_oco_transitioned), managed=deepcopy(svc._managed_v2_symbols),
        incident=(incident.status, incident.closed_at), inflight=deepcopy(svc._v2_eod_oco_transition_inflight))
    return result, trace, log


@pytest.fixture(autouse=True)
def clocks(monkeypatch):
    monkeypatch.setattr(module, "utcnow", lambda: NOW)
    monkeypatch.setattr(retained, "utcnow", lambda: NOW)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(retained, "time", SimpleNamespace(monotonic=lambda: 100.0))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", TRANSITION_MODES)
async def test_caller_reclassification_or_latch_matches_8159_and_names_the_step(mode):
    old, old_trace, old_log = await run_transition(retained.RetainedEod8159, mode)
    new, new_trace, new_log = await run_transition(OmsRiskService, mode)
    assert new == old and new_trace == old_trace and new_log.business == old_log.business
    assert not new["inflight"]
    assert bool(new["latch"]) is (mode == "released_stable")
    if mode == "fill_recording_false":
        assert any("step=transition_fill_recording_refused " in line for line in new_log.steps)
    if mode == "current_row_error":
        assert any("step=transition_current_row_read_error " in line and "release=released" in line
                   for line in new_log.steps)
    assert not old_log.steps


@pytest.mark.parametrize("outcome", ["released", "resolved_by_fill", "unanswerable"])
def test_incident_sync_observes_status_without_reclassifying_release(outcome):
    results = []
    for methods in (retained.RetainedEod8159, OmsRiskService):
        svc, trace, log, incident = transition_driver(methods, "released_stable")
        release = ExitPairReleaseResult(outcome=outcome)
        # Same controlled incident/query boundaries; no DB is opened.
        session = SimpleNamespace(scalars=lambda query: SimpleNamespace(all=lambda: [incident]))
        methods._sync_v2_eod_release_incident(svc, session, ACCOUNT, SYMBOL, ROW, release)
        results.append((incident.status, incident.closed_at, deepcopy(trace), release))
        if methods is OmsRiskService:
            assert len(log.steps) == 2 and all("release=" + outcome in line for line in log.steps)
    assert results[0] == results[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["confirm_released", "legacy_precheck", "release_error", "current_row_error"])
async def test_throwing_diagnostic_sink_cannot_change_calls_returns_or_refusals(mode):
    runner = run_transition if mode == "current_row_error" else run_release
    old, old_trace, old_log = await runner(retained.RetainedEod8159, mode)
    new, new_trace, new_log = await runner(OmsRiskService, mode, fail=True)
    assert new == old and new_trace == old_trace and new_log.business == old_log.business
    assert not new_log.steps


def test_passive_formatter_never_triggers_result_properties_or_ORM_getters():
    accesses = []
    class Poison:
        def __getattribute__(self, name):
            accesses.append(name)
            raise AssertionError("diagnostics invoked a property")
    service = SimpleNamespace(logger=Log())
    diagnostic.eod_oco_step(service, "CONTROLLED-property-guard", ACCOUNT, SYMBOL, ROW,
        {"release": Poison(), "row": Poison(), "entry": Poison()})
    # An unreadable diagnostic may omit a record; it must never propagate.
    assert service.logger.business == [] and accesses == []


def test_passive_status_record_has_expected_base_and_zero_cancelled_report():
    service = SimpleNamespace(logger=Log())
    release = ExitPairReleaseResult(outcome="released", reports=reports())
    scope = dict(base=BASE, release=release, confirmed=release)
    before = deepcopy(scope)
    diagnostic.eod_oco_step(service, "CONTROLLED-status", ACCOUNT, SYMBOL, ROW, scope)
    assert scope == before
    line, = service.logger.steps
    assert "release=released confirm=released" in line and "base=" + BASE in line
    assert "release_count=2 confirm_count=2" in line and "cancelled/broker/0/confirmed" in line


def test_whole_service_AST_is_exact_8159_after_stripping_only_diagnostic_import_and_calls():
    original = ast.parse(Path(os.environ["WBEXIT_EOD_8159_SERVICE_SOURCE"]).read_text())
    candidate = ast.parse(Path(inspect.getsourcefile(OmsRiskService)).read_text())
    class Strip(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return None if node.module == "project_mai_tai.oms.eod_oco_step_log" else node
        def visit_Expr(self, node):
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "_eod_oco_step":
                return None
            return self.generic_visit(node)
    assert ast.dump(original, include_attributes=False) == ast.dump(Strip().visit(candidate), include_attributes=False)
