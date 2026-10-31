"""Author-only controls; original fourteen RED results are not reclassified PASS."""

import asyncio
from types import SimpleNamespace

import pytest

from project_mai_tai.broker_adapters import schwab as schwab_module
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.settings import Settings
from tests.unit import test_orbroute1_oms_chain as chain
from tests.unit import test_l8_orb_control_envelope as envelope
from tests.unit import test_orbroute1_manual_net_092 as manual
from tests.unit import test_orbroute1_session_admission as sessions
from tests.unit import test_orbpurple1_atr_entry as purple
from tests.unit import test_orb_schwab_order_route as route
from tests.unit import test_momcancel1_ack as cancel_ack


chain_setup = chain.setup


def test_existing_native_control_assertions_without_fixture_amendment(chain_setup, monkeypatch):
    envelope.test_real_oms_with_native_run_control_submits_one_selected_parent(chain_setup, monkeypatch, True)


def test_existing_manual_net_assertions_without_fixture_amendment(chain_setup, caplog):
    manual.test_manual_thousand_shares_allows_routed_ten_and_logs_without_receipt(
        chain_setup, chain.SCHWAB, True, caplog)


def test_existing_country_chain_assertions_without_fixture_amendment(chain_setup):
    chain.test_real_oms_one_selected_parent_ten_shares_no_mirror(chain_setup, True, chain.SCHWAB)


@pytest.mark.parametrize("kind", ["order", "unknown"])
@pytest.mark.parametrize("second", [0, 14])
def test_existing_prior_session_assertions_without_fixture_amendment(chain_setup, monkeypatch, caplog, kind, second):
    sessions.test_actual_oms_entry_uses_session_aware_wrapper(
        chain_setup, monkeypatch, chain.SCHWAB, True, 13, 28, second, "previous", kind, caplog)


def test_existing_post_preview_atr_assertion_without_fixture_amendment(monkeypatch):
    purple.test_oms_rechecks_atr_after_preview_and_records_local_refusal(monkeypatch)


@pytest.mark.parametrize("symbol,day", [("MI", "2026-10-05"), ("JAGX", "2026-10-06")])
def test_existing_atr_history_assertions_without_fixture_amendment(monkeypatch, tmp_path, symbol, day):
    purple.test_oms_real_schwab_history_passes_both_fills_and_blocks_purple(
        monkeypatch, tmp_path, symbol, day, True)


def test_existing_webull_geometry_mutant_keeps_original_assertion(chain_setup, monkeypatch):
    chain.test_atr_geometry_mutation_is_caught_by_actual_orb_native_pair(chain_setup, monkeypatch)


def test_existing_accepted_put_unknown_confirmation_keeps_new_identity(monkeypatch):
    route.test_put_accepted_confirmation_unreadable_persists_new_id_and_reconciles_under_hold(monkeypatch)


def test_above_cap_refusal_capture_attached_after_service_init(monkeypatch, caplog):
    route.test_pastlevel_actual_oms_placement_shapes(monkeypatch, caplog, "1.22", None, 0)


@pytest.mark.asyncio
async def test_existing_legacy_orb_caller_contract(monkeypatch):
    await cancel_ack.test_other_schwab_callers_keep_existing_fast_return_or_terminal_poll(monkeypatch, "orb")


@pytest.mark.parametrize("kind,policy", [("LIMIT", None), ("STOP_LIMIT", "ORBPASTLEVEL1-v1"),
                                       ("STOP_LIMIT", "unknown-policy")])
@pytest.mark.asyncio
async def test_new_orb_selection_cannot_drop_fence_to_use_legacy_contract(monkeypatch, kind, policy):
    adapter = SchwabBrokerAdapter(Settings())
    calls = []
    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("unfenced marked/new LIMIT request reached transport")
    monkeypatch.setattr(adapter, "_authorized_request_json", forbidden)
    metadata = dict(orb_intended_break_level="1.20",
                    bracket="true", bracket_entry_type=kind, stop_price="1.20", limit_price="1.21",
                    bracket_target_price="1.26", bracket_stop_price="1.10")
    if policy is not None:
        metadata["orb_pastlevel_policy"] = policy
    request = OrderRequest("synthetic", "synthetic", "orb_schwab", "FIXTURE", "buy", "open", 2,
                           "SYNTHETIC", metadata)
    reports = await adapter.submit_order(request)
    assert reports[0].event_type == "rejected" and calls == []
    assert "fenced LIMIT bracket" in reports[0].reason


@pytest.mark.asyncio
async def test_existing_transport_signature_and_context_are_task_local(monkeypatch):
    adapter = SchwabBrokerAdapter(Settings())
    seen = []
    first, second = lambda: None, lambda: None
    expected = {"/first": first, "/second": second}
    async def old_signature(method, path, body):
        await asyncio.sleep(0)
        assert schwab_module._order_prewire_check.get() is expected[path]
        seen.append(path)
        return 201, {}, {}
    monkeypatch.setattr(adapter, "_authorized_request_json", old_signature)
    await asyncio.gather(*[adapter._authorized_order_request_json(
        SimpleNamespace(prewire_check=fence), "PUT", path, body={}) for path, fence in expected.items()])
    assert set(seen) == set(expected)
    assert schwab_module._order_prewire_check.get() is None


@pytest.mark.asyncio
async def test_context_fence_still_runs_after_token_await_and_thread_scheduling(monkeypatch):
    adapter = SchwabBrokerAdapter(Settings())
    stale, calls = [], []
    def fence():
        if stale:
            raise ValueError("orb_pastlevel_quote_stale")
    async def token(**kwargs):
        stale.append(True)
        return "synthetic-token"
    monkeypatch.setattr(adapter, "_get_access_token", token)
    monkeypatch.setattr(adapter, "_blocking_request_json", lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match="quote_stale"):
        await adapter._authorized_order_request_json(SimpleNamespace(prewire_check=fence), "POST", "/unused", body={})
    assert calls == [] and schwab_module._order_prewire_check.get() is None


@pytest.mark.asyncio
async def test_context_fenced_401_never_retries(monkeypatch):
    adapter = SchwabBrokerAdapter(Settings())
    calls = []
    async def token(**kwargs):
        calls.append("token")
        return "synthetic-token"
    async def wire(*args, prewire_check=None, **kwargs):
        assert prewire_check is not None
        prewire_check()
        calls.append("wire")
        return 401, {}, {}
    monkeypatch.setattr(adapter, "_get_access_token", token)
    monkeypatch.setattr(adapter, "_request_json", wire)
    adapter._refresh_token, adapter.client_id, adapter.client_secret = "x", "x", "x"
    result = await adapter._authorized_order_request_json(SimpleNamespace(prewire_check=lambda: None), "POST", "/unused", body={})
    assert result[0] == 401 and calls == ["token", "wire"]
    assert schwab_module._order_prewire_check.get() is None


def test_missing_quote_uses_legacy_without_changing_existing_tests(monkeypatch):
    service, _factory, broker = route._service(monkeypatch)
    service._latest_quotes_by_symbol = {}
    event = route.build_orb_schwab_open_intent(service.settings, "MI", route.Decimal("6.67"))
    result = asyncio.run(service.process_trade_intent(event))
    assert result[0].payload.status == "accepted"
    assert len(broker.previewed) == len(broker.submitted) == 1
    request = broker.submitted[0]
    assert request.order_type == request.metadata["bracket_entry_type"] == "STOP_LIMIT"
    assert request.metadata["stop_price"] == "6.67" and request.metadata["limit_price"] == "6.71"
    assert request.metadata["orb_quote_selection"] == "legacy_no_fresh_ask"
    request.prewire_check()
