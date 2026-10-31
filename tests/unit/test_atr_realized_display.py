"""CONTROLLED receipt identity/display tests; no actual-copy or economic proof.

No allocator, normalizer, model or database executes in these controls.
"""

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from project_mai_tai.atr_simulation.aggregate_entry_read_model import AggregateReadBinding
from project_mai_tai.atr_simulation import realized_display as display
from project_mai_tai.services import atr_aggregate_routes as routes
from project_mai_tai.settings import Settings


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def controlled_receipt(*, unknown=True, all_unknown=False):
    """Only explicit fixture membership, not selected actual173 acceptance."""
    rows, legs, signals = [], [], []
    for index in range(173):
        key = ["CONTROLLED", "2026-10-12", f"{index // 60:02}:{index % 60:02}:00"]
        group = []
        for account in (("live:orb", "live:schwab_1m_v2") if index < 80 else ("live:orb",)):
            order = f"CONTROLLED-{index}-{account}"
            rows.append(dict(identity=dict(account_id=account), order_id=order,
                raw_baseline_result=dict(reference_key=key, recorded_aggregate=dict(quantity=100))))
            missing = all_unknown or (unknown and index == 0 and account == "live:orb")
            leg = dict(seed_id=account+":"+order, signal_key=key, account=account,
                client_order_id=order, quantity=100, status="UNKNOWN" if missing else "MATCHED",
                gross_pnl=None if missing else "1.00",
                issues=["ENTRY_NOT_FULLY_EXITED"] if missing else [],
                fill_ids=[] if missing else ["CONTROLLED-BUY-"+order, "CONTROLLED-EXIT-"+order])
            group.append(leg)
            legs.append(leg)
        missing_signal = all_unknown or (unknown and index == 0)
        signals.append(dict(signal_key=key, legs=group,
            status="UNKNOWN" if missing_signal else "MATCHED",
            gross_pnl=None if missing_signal else ("2.00" if index < 80 else "1.00")))
    publication = encoded(dict(source_id="aggregate-entry:"+"a"*64, input_sha256="a"*64,
                               result_rows=rows))
    binding = AggregateReadBinding("aggregate-entry:"+"a"*64, "a"*64, publication,
                                   hashlib.sha256(publication).hexdigest())
    matched = 0 if all_unknown else (172 if unknown else 173)
    proof = dict(version="ATR_RESTORED_FILL_NORMALIZATION_V1",
        receipt_type="NORMALIZATION_BINDING_NOT_RESTORE_EXECUTION",
        restore_complete=True, native_execution_qualified=False,
        source_counts=dict(fills=20733, broker_orders=85174),
        target_identity=dict(database="CONTROLLED-not-actual-copy", nonce="CONTROLLED"),
        receipt_sha256="b"*64, capture_sha256="c"*64,
        restore_receipt_sha256="d"*64, main_authority_sha256="e"*64,
        projection_sha256="f"*64)
    result = dict(version="ATR_REALIZED_RECONCILIATION_V3_MANUAL_EXCLUSION_COMPLETE_SIGNALS",
        display_contract="COMPLETE_SIGNALS_ONLY_GROSS_FEES_NOT_INCLUDED_V1",
        account_legs=legs, signals=signals,
        matched_signals=matched, unmatched_signals=173-matched,
        complete_signal_count=matched, unreconciled_signal_count=173-matched,
        complete_signal_gross_pnl="0" if all_unknown else ("251.00" if unknown else "253.00"),
        known_partial_account_leg_count=1 if unknown and not all_unknown else 0,
        known_partial_account_leg_gross_pnl="1.00" if unknown and not all_unknown else "0",
        known_partial_account_legs=[legs[1]] if unknown and not all_unknown else [],
        matched_component_gross_pnl="0" if all_unknown else ("252.00" if unknown else "253.00"),
        full_gross_pnl="253.00" if not unknown and not all_unknown else None,
        native_execution_qualified=False, restore_receipt=proof,
        input_sha256=dict(capture="c"*64, restore_receipt="b"*64, selected_signal_receipt="a"*64),
        fees_status="fees not included", fee_key_census={})
    return result, binding


def prepare(result, binding):
    raw = encoded(result)
    return display.prepare_realized_display(raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
                                            aggregate_binding=binding)


def test_partial_reconciled_component_and_unmatched_count_are_separate_from_full_and_live():
    result, binding = controlled_receipt()
    before = deepcopy(result)
    prepared = prepare(result, binding)
    view = prepared.view()
    assert result == before
    assert result["matched_component_gross_pnl"] == "252.00"  # Known-leg audit, not page numerator.
    assert view["complete_signal_gross_pnl"] == "251.00" and view["full_gross_pnl"] is None
    assert view["matched_signals"] == 172 and view["unmatched_signals"] == 1
    assert view["matched_account_legs"] == 252 and view["account_leg_count"] == 253
    assert view["included_in_live_totals"] is view["native_execution_qualified"] is False
    assert view["actual_restore_receipt_sha256"] == "d"*64
    view["complete_signal_gross_pnl"] = "FORGED"
    assert prepared.view()["complete_signal_gross_pnl"] == "251.00"
    with pytest.raises(FrozenInstanceError):
        prepared.raw = b"{}"


def test_complete_receipt_copies_full_gross_but_never_claims_net_or_global_live():
    result, binding = controlled_receipt(unknown=False)
    view = prepare(result, binding).view()
    assert view["full_gross_pnl"] == "253.00" and view["unmatched_signals"] == 0
    html = display.render_realized_display(view)
    assert "Realized (fills): +$253.00 gross over 173 complete signals" in html
    assert "fees not included" in html and "never added to LIVE daily totals" in html


def test_all_unknown_and_no_receipt_never_display_zero():
    result, binding = controlled_receipt(all_unknown=True)
    view = prepare(result, binding).view()
    assert view["complete_signal_gross_pnl"] is None and view["unmatched_signals"] == 173
    for html in (display.render_realized_display(view), display.render_realized_display(None)):
        assert "realized: under reconciliation" in html
        assert "gross component:" not in html and "selected-ledger gross:" not in html
        assert "unmatched signals:" not in html and "component: 0" not in html


def test_external_expected_digest_is_required_not_taken_from_result():
    result, binding = controlled_receipt()
    raw = encoded(result)
    with pytest.raises(ValueError, match="external_receipt_sha"):
        display.prepare_realized_display(raw, expected_sha256="0"*64, aggregate_binding=binding)
    with pytest.raises(ValueError, match="external_sha256"):
        display.prepare_realized_display(raw, expected_sha256=None, aggregate_binding=binding)


@pytest.mark.parametrize("fault", (
    "seed", "capture", "normalization", "copy", "identity", "sources", "authority",
    "native", "refused", "missing-leg", "duplicate-leg", "foreign-order",
    "duplicate-signal", "signal-status", "count", "incomplete-full", "amount", "fee",
))
def test_unbound_partial_foreign_or_malformed_receipts_refuse(fault):
    result, binding = controlled_receipt()
    proof = result["restore_receipt"]
    if fault == "seed":
        result["input_sha256"]["selected_signal_receipt"] = "0"*64
    elif fault == "capture":
        proof["capture_sha256"] = "0"*64
    elif fault == "normalization":
        proof["receipt_type"] = "ACTUAL_RESTORE_EXECUTION"
    elif fault == "copy":
        proof["restore_complete"] = False
    elif fault == "identity":
        proof["target_identity"] = {}
    elif fault == "sources":
        proof["source_counts"]["fills"] = 10
    elif fault == "authority":
        proof["main_authority_sha256"] = None
    elif fault == "native":
        result["native_execution_qualified"] = True
    elif fault == "refused":
        result["status"] = "REFUSED_DUPLICATE_IDENTITY"
    elif fault == "missing-leg":
        result["account_legs"].pop()
    elif fault == "duplicate-leg":
        result["account_legs"][-1] = deepcopy(result["account_legs"][0])
    elif fault == "foreign-order":
        result["account_legs"][0]["client_order_id"] = "OTHER"
    elif fault == "duplicate-signal":
        result["signals"][-1] = deepcopy(result["signals"][0])
    elif fault == "signal-status":
        result["signals"][0]["status"] = "MATCHED"
    elif fault == "count":
        result["unmatched_signals"] = True
    elif fault == "incomplete-full":
        result["full_gross_pnl"] = "252.00"
    elif fault == "amount":
        result["matched_component_gross_pnl"] = "NaN"
    else:
        result["fees_status"] = "NET_ZERO_FEES_ASSUMED"
    with pytest.raises(ValueError):
        prepare(result, binding)


def test_direct_constructor_cannot_wrap_unvalidated_summary():
    result, binding = controlled_receipt()
    raw = b'{"component_gross_pnl":"999"}'
    with pytest.raises(ValueError, match="completed_allocator"):
        display.BoundRealizedDisplay(raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
                                    aggregate_binding=binding)


@pytest.mark.parametrize("raw", (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e9999}'))
def test_duplicate_or_nonfinite_json_is_refused_before_display(raw):
    _, binding = controlled_receipt()
    with pytest.raises(ValueError):
        display.prepare_realized_display(raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
                                         aggregate_binding=binding)


def test_recorded_fee_fields_keep_semantic_review_not_net_pnl():
    result, binding = controlled_receipt()
    result.update(fee_key_census={"$.commission": 2}, fees_status="RECORDED_FIELDS_REQUIRE_SEMANTIC_REVIEW")
    html = display.render_realized_display(prepare(result, binding).view())
    assert "RECORDED_FIELDS_REQUIRE_SEMANTIC_REVIEW" in html
    assert "Realized (fills): +$251.00 gross over 172 complete signals" in html
    assert "fees not included" in html


def test_unknown_issue_text_is_escaped_and_scientific_zero_is_supported():
    result, binding = controlled_receipt()
    result["account_legs"][0]["issues"] = ["<script>CONTROLLED</script>"]
    for leg in result["account_legs"]:
        if leg["status"] == "MATCHED":
            leg["gross_pnl"] = "0E-8"
    for signal in result["signals"]:
        if signal["status"] == "MATCHED":
            signal["gross_pnl"] = "0E-8"
    result.update(complete_signal_gross_pnl="0E-8", known_partial_account_leg_gross_pnl="0E-8",
                  matched_component_gross_pnl="0E-8")
    html = display.render_realized_display(prepare(result, binding).view())
    assert "&lt;script&gt;CONTROLLED&lt;/script&gt;" in html and "<script>" not in html
    assert prepare(result, binding).view()["complete_signal_gross_pnl"] == "0E-8"
    assert "Realized (fills): +$0.00 gross over 172 complete signals" in html


def test_configured_artifact_is_read_once_and_original_bytes_are_unchanged(tmp_path):
    result, binding = controlled_receipt()
    raw = encoded(result)
    path = tmp_path / "CONTROLLED-reconciliation.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    prepared = display.configured_realized_display(str(path), hashlib.sha256(raw).hexdigest(),
                                                  aggregate_binding=binding)
    assert path.read_bytes() == raw
    path.unlink()
    assert prepared.view()["unmatched_signals"] == 1


@pytest.mark.parametrize("fault", ("relative", "symlink", "writable", "digest", "half-config"))
def test_artifact_filesystem_and_external_binding_guards(tmp_path, fault):
    result, binding = controlled_receipt()
    raw = encoded(result)
    path = tmp_path / "receipt.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    sha = hashlib.sha256(raw).hexdigest()
    selected = str(path)
    if fault == "relative":
        selected = "receipt.json"
    elif fault == "symlink":
        link = tmp_path / "alias.json"
        link.symlink_to(path)
        selected = str(link)
    elif fault == "writable":
        path.chmod(0o622)
    elif fault == "digest":
        sha = "0"*64
    else:
        sha = None
    with pytest.raises((ValueError, OSError)):
        display.configured_realized_display(selected, sha, aggregate_binding=binding)


def test_unconfigured_projection_never_opens_a_file(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unconfigured must not open")
    monkeypatch.setattr(display.os, "open", forbidden)
    assert display.configured_realized_display(None, None, aggregate_binding=None) is None
    assert Settings(_env_file=None).atr_simulation_realized_receipt_path is None
    assert Settings(_env_file=None).atr_simulation_realized_receipt_sha256 is None


def test_route_renders_separate_bound_line_with_no_model_read_or_live_mutation():
    result, binding = controlled_receipt()
    realized = prepare(result, binding)
    def no_read(*args, **kwargs):
        raise AssertionError("unselected model source must not read")
    port = routes.AggregateSimulationReadPort(SimpleNamespace(read=no_read), binding, realized)
    context = asyncio.run(routes.load_aggregate_display(port))
    before = deepcopy(context)
    live = {"daily_pnl": "CONTROLLED-live-unchanged", "rows": ["ACTUAL-not-SIM"]}
    original = encoded(live)
    html = routes.render_aggregate_page(context, render_row=lambda row: "UNUSED")
    assert "Realized (fills): +$251.00 gross over 172 complete signals" in html
    assert "252.00" not in html
    assert "1 signals unreconciled" in html and "partial timing UNKNOWN" in html
    assert "not a requested-page or account-wide total" in html
    assert context == before and encoded(live) == original


def test_get_api_and_html_share_bound_receipt_and_keep_modeled_unknown_separate():
    result, binding = controlled_receipt()
    port = routes.AggregateSimulationReadPort(SimpleNamespace(read=lambda *a, **kw: None),
                                            binding, prepare(result, binding))
    app = FastAPI()
    app.include_router(routes.build_aggregate_router(port, render_row=lambda row: "UNUSED"))
    with TestClient(app) as client:
        api = client.get("/atr-aggregate/api")
        html = client.get("/atr-aggregate")
        assert client.post("/atr-aggregate").status_code == 405
    assert api.status_code == html.status_code == 200
    assert api.headers["cache-control"] == html.headers["cache-control"] == "no-store"
    assert api.json()["view"] is None and api.json()["read_status"] == "UNAVAILABLE"
    assert api.json()["realized"]["complete_signal_gross_pnl"] == "251.00"
    assert "1 signals unreconciled" in html.text and "Modeled replay" in html.text


def test_config_without_aggregate_source_cannot_be_silently_ignored():
    from project_mai_tai.atr_simulation.accepted_aggregate_bootstrap import configured_aggregate_port
    with pytest.raises(ValueError, match="requires_accepted_aggregate"):
        configured_aggregate_port("CONTROLLED-no-db", None,
                                  realized_receipt_path="/CONTROLLED/no-read",
                                  realized_receipt_sha256="a"*64)


def test_shared_page_actual_live_data_and_modeled_rows_are_not_mutated(monkeypatch):
    import httpx
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker
    from project_mai_tai.services import control_plane as cp
    from tests.unit.test_atrpnl1 import fill, page_data
    from tests.unit.test_firstbar_readonly_mount import Recorder
    from threading import get_ident

    result, binding = controlled_receipt()
    realized = prepare(result, binding)
    data = page_data([fill("buy", 10, 4, "CONTROLLED-entry"),
                      fill("sell", 10, 5, "CONTROLLED-entry-ocoexit-native")])
    original = deepcopy(data)
    def unused(*args, **kwargs):
        raise AssertionError("unselected model source must not read")
    port = routes.AggregateSimulationReadPort(SimpleNamespace(read=unused), binding, realized)
    loop_thread, render_threads = get_ident(), []
    original_render = routes.render_aggregate_page
    def observed_render(*args, **kwargs):
        render_threads.append(get_ident())
        return original_render(*args, **kwargs)
    monkeypatch.setattr(routes, "render_aggregate_page", observed_render)
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        app = cp.build_app(Settings(_env_file=None, trade_coach_enabled=False,
                                    bot_control_enabled=False),
            session_factory=sessionmaker(engine), firstbar_read_port=Recorder(),
            aggregate_simulation_read_port=port)
        async def dashboard():
            return deepcopy(data)
        app.state.repository = SimpleNamespace(load_bot_dashboard_data=dashboard)
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                          base_url="http://CONTROLLED") as client:
                return (await client.get("/bot/1m-schwab-v2"),
                        await client.get("/atr-aggregate"))
        response, aggregate = asyncio.run(request())
        assert response.status_code == 200 and data == original
        assert "$+10.00" in response.text
        assert "Simulation unavailable right now" in response.text
        assert "Realized (fills): +$251.00 gross over 172 complete signals" in aggregate.text
        assert "1 signals unreconciled" in aggregate.text
        assert "never added to LIVE daily totals" in aggregate.text
        assert render_threads and all(thread != loop_thread for thread in render_threads)
    finally:
        engine.dispose()


def test_selected_modeled_view_rows_and_api_values_remain_unchanged(monkeypatch):
    result, binding = controlled_receipt()
    saved_view = dict(read_status="AVAILABLE", rows=[dict(modeled_pnl="CONTROLLED-only")],
                      actual_realized_pnl_included=False, global_pnl_qualified=False)
    original = deepcopy(saved_view)
    def projection(stored, **kwargs):
        return deepcopy(saved_view)
    monkeypatch.setattr(routes, "aggregate_entry_display_view", projection)
    port = routes.AggregateSimulationReadPort(SimpleNamespace(read=lambda *a, **kw: {}),
                                            binding, prepare(result, binding))
    context = asyncio.run(routes.load_aggregate_display(port, source_id=binding.source_id))
    assert context["view"] == original and saved_view == original
    assert context["realized"]["complete_signal_gross_pnl"] == "251.00"
    assert context["view"]["actual_realized_pnl_included"] is False


def test_held_realized_renderer_allows_unrelated_http_progress(monkeypatch):
    import httpx
    from threading import Event

    result, binding = controlled_receipt()
    port = routes.AggregateSimulationReadPort(SimpleNamespace(read=lambda *a, **kw: None),
                                            binding, prepare(result, binding))
    started, release = Event(), Event()
    original = routes.render_aggregate_page
    def held(*args, **kwargs):
        started.set()
        assert release.wait(4)
        return original(*args, **kwargs)
    monkeypatch.setattr(routes, "render_aggregate_page", held)
    app = FastAPI()
    app.include_router(routes.build_aggregate_router(port, render_row=lambda row: "UNUSED"))
    @app.get("/unrelated")
    async def unrelated():
        return {"ok": True}
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                      base_url="http://CONTROLLED") as client:
            task = asyncio.create_task(client.get("/atr-aggregate"))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                assert (await asyncio.wait_for(client.get("/unrelated"), 2)).status_code == 200
                assert not task.done()
            finally:
                release.set()
                response = await task
                assert response.status_code == 200 and "1 signals unreconciled" in response.text
    asyncio.run(scenario())


@pytest.mark.parametrize("injected", (False, True))
def test_normal_build_forwards_explicit_startup_binding_without_connecting(monkeypatch, injected):
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker
    from project_mai_tai.atr_simulation import accepted_aggregate_bootstrap as boot
    from project_mai_tai.services import control_plane as cp
    from tests.unit.test_firstbar_readonly_mount import Recorder

    result, binding = controlled_receipt()
    port = routes.AggregateSimulationReadPort(SimpleNamespace(read=lambda *a, **kw: None),
                                            binding, prepare(result, binding))
    calls, connections = [], []
    def configured(database_url, path, **kwargs):
        calls.append((path, kwargs))
        return port
    monkeypatch.setattr(boot, "configured_aggregate_port", configured)
    engine = sa.create_engine("sqlite:///:memory:")
    sa.event.listen(engine, "connect", lambda *args: connections.append(True))
    try:
        app = cp.build_app(Settings(_env_file=None,
            atr_simulation_aggregate_receipt_path="/CONTROLLED/accepted-seed",
            atr_simulation_realized_receipt_path="/CONTROLLED/actual-copy-result",
            atr_simulation_realized_receipt_sha256="1"*64),
            session_factory=sessionmaker(engine), firstbar_read_port=Recorder(),
            aggregate_simulation_read_port=port if injected else None)
        assert app.state.aggregate_simulation_read_port is port and connections == []
        assert calls == ([] if injected else [("/CONTROLLED/accepted-seed", dict(
            realized_receipt_path="/CONTROLLED/actual-copy-result",
            realized_receipt_sha256="1"*64))])
    finally:
        engine.dispose()
