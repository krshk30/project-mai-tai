"""External exact-setting controls on a frozen candidate, never venue evidence.

Run with parent's combination_plugin and frozen candidate src/root first on
PYTHONPATH. Legacy tests stay intact. Completed-line outputs, SDK ACKs, clocks,
fills and SQLite journals here are controlled, not historical reconstructions.
"""
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from project_mai_tai import settings as settings_module
from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.events import TradeIntentEvent
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from tests.unit import test_all_on_pm as old
from tests.unit import test_clearwait1_removed_wait as clear
from tests.unit import test_keeprest1 as keep
from tests.unit import test_mirrorhold1_retained_hold as mirror
from tests.unit import test_rpg1_runtime as rpg
from tests.unit import test_webull_list_primary as primary

CANDIDATE = "aed1a358c0da4043ce248550c2b1d02cecdc76ff"
TREE = "b28df7b3d492d0be4e72e1233ce0fff55e43fca4"
ROOT = Path("/Users/velkris/.codex/worktrees/install2-reviewed-batch-20261006/project-mai-tai")
EXACT = {**old.ALL_ON,
         "strategy_schwab_1m_v2_keep_rest_after_buy_enabled": True,
         "strategy_schwab_1m_v2_removed_wait_clear_enabled": True,
         "oms_v2_webull_mirror_retained_hold_enabled": True,
         "webull_list_primary_reads_enabled": True,
         "strategy_schwab_1m_v2_retry_one_enabled": True,
         "strategy_schwab_1m_v2_retry_one_max_retries": 0}


@pytest.fixture(scope="session", autouse=True)
def frozen_source():
    assert Path(settings_module.__file__).resolve() == ROOT / "src/project_mai_tai/settings.py"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip() == CANDIDATE
    assert subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT).decode().strip() == TREE
    assert subprocess.check_output(["git", "diff", CANDIDATE, "--", "src", "ops", "tests"], cwd=ROOT) == b""


@pytest.fixture(autouse=True)
def exact_settings(exact_install2_settings, monkeypatch):
    # Parent plugin forces its six Install2 values before strategy caching.
    # Retain the existing ALL_ON eight at that same construction boundary.
    original = Settings.__init__

    def configured(self, **values):
        original(self, **{**values, **old.ALL_ON})
        assert all(getattr(self, name) == value for name, value in EXACT.items())

    monkeypatch.setattr(Settings, "__init__", configured)
    old.completed_seeded_line.__wrapped__(monkeypatch)
    original_service = mirror._integrated_service

    def real_collision_guard(*args, **kwargs):
        service, adapter = original_service(*args, **kwargs)
        # Retained unit helper disabled this guard. Exact composition must not.
        service._fanout_webull_collision_reason = OmsRiskService._fanout_webull_collision_reason.__get__(service)
        return service, adapter

    monkeypatch.setattr(mirror, "_integrated_service", real_collision_guard)
    monkeypatch.setattr(rpg, "_integrated_service", real_collision_guard)


@pytest.fixture
def lane(monkeypatch):
    value = mirror.lane.__wrapped__(monkeypatch)
    assert all(getattr(value[0].settings, name) == setting for name, setting in EXACT.items())
    adapter = value[0].broker_adapter
    # The SDK __new__ fixture explicitly selected historical detail-read OFF.
    # Restore the actual Install2 cached flag plus its controlled reader runtime.
    adapter._list_primary_enabled = value[0].settings.webull_list_primary_reads_enabled
    clock = primary.Clock()
    adapter._query_budget = primary.QueryBudget(clock)
    adapter._today_reader = primary.TodayOrderReader(adapter._query_budget, 15, clock)
    adapter._terminal_read_lock = threading.Lock()
    adapter._terminal_inflight = set()
    adapter._terminal_proof_store = None
    assert adapter._list_primary_enabled is True
    return value


@pytest.fixture
def fake_sdk(monkeypatch):
    primary._sdk_fixture.__wrapped__(monkeypatch)


def test_exact_constructed_settings_and_cached_strategy():
    strategy, _, _ = keep.seeded(all_on=True)
    assert all(getattr(strategy.settings, name) == value for name, value in EXACT.items())
    assert strategy._retry_one_enabled and strategy._retry_one_max_retries == 0
    assert strategy._line_restoration_enabled and strategy._gap_hold_enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["stream", "five_second_rest"])
@pytest.mark.parametrize("case", [old.STRAYS[0], old.VEEA, old.CLRO], ids=["SAIQ", "VEEA", "CLRO"])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_retained_pm_service_cross_both_brokers(monkeypatch, route, case, slot):
    await old.test_all_on_recorded_service_cross_same_ask_both_legs(monkeypatch, route, case, slot)


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
def test_incomplete_completed_line_refuses_without_consuming(stream, slot):
    old.test_all_eight_on_incomplete_line_blocks_both_cross_paths_without_consuming_state(stream, slot)


def test_service_rechecks_completed_line_version(monkeypatch):
    old.test_all_eight_on_service_refuses_draft_after_completed_line_is_revoked(monkeypatch)


@pytest.mark.parametrize("slot", ["first", "reclaim"])
def test_pm_software_move_not_broker_handoff(slot):
    old.test_all_on_pmrest_software_move_owns_no_handoff_preserves_accounting(slot)


@pytest.mark.parametrize("gate", ["boot", "gap"])
@pytest.mark.parametrize("stream", [True, False])
def test_pm_held_rest_cannot_move_or_cross(gate, stream):
    old.test_all_on_held_rest_cannot_move_or_cross(gate, stream)


def test_new_pm_reclaim_remains_disallowed():
    old.test_all_on_new_pm_reclaim_remains_disallowed()


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_schwab_rth_real_handoff_remains_owned(monkeypatch, slot):
    await old.test_all_on_rth_broker_reprice_owned_by_handoff(monkeypatch, "schwab", slot)


@pytest.mark.parametrize("when", ["09:30:02", "09:31:02"])
def test_rth_conversion_still_honors_thin_bar_cancel(monkeypatch, caplog, when):
    old.test_all_on_rth_conversion_preserves_original_thin_cancel(monkeypatch, caplog, when)


def test_actual_source_catalog_on_owners():
    old.test_all_on_catalog_audit_committed_live_set_requires_both_consumers()


def test_sell_flip_still_ends_pm_wait(monkeypatch, caplog):
    old.test_all_on_flip_wait_takedown_and_rth_latch_regressions(monkeypatch, caplog, "sell_flip")


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_both_broker_clear_handoffs_wait_on_incomplete_line(monkeypatch, broker):
    await old.test_all_on_gap_hold_withholds_proven_clear_handoff_without_replacement(monkeypatch, broker)


def test_obsolete_dark_process_catalog_is_real_failure():
    old.test_all_on_process_catalog_checker_refuses_three_dark_values_without_live_io()


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_webull_actual_rpg_nonces_free_waits_then_serial_wire(monkeypatch, slot):
    original = rpg.runtime

    async def with_slot(mp, broker, **kwargs):
        value = await original(mp, broker, **{**kwargs, "slot": slot})
        assert all(getattr(value.strategy.settings, name) == setting
                   and getattr(value.service.settings, name) == setting for name, setting in EXACT.items())
        return value

    monkeypatch.setattr(rpg, "runtime", with_slot)
    await mirror.test_rpg_nonces_after_many_free_waits_do_not_spend_actual_wire_budget(monkeypatch, True)


@pytest.mark.asyncio
async def test_current_retained_no_wire_reprice_never_invents_cancel(monkeypatch):
    h = await rpg.runtime(monkeypatch, "webull", strategy_overrides=old.ALL_ON)
    FanoutSegmentIdentityStore(h.factory).record(h.state.symbol, h.state.fanout_segment_id, True,
        "CONTROLLED current retained no-wire segment", now=h.clock[0])
    # Controlled initial ledger: no venue order ever submitted for this generation.
    with h.factory() as session:
        session.execute(delete(BrokerOrder))
        session.commit()
    await h.service._handle_stream_message({"data": h.opening.model_dump_json()})
    assert not h.adapter.opens
    local_lane = h.service, None, h.factory, h.clock
    assert mirror.state(local_lane, h.opening)["phase"] == "held"
    assert mirror.state(local_lane, h.opening)["wire_submissions"] == 0
    token, _ = await rpg.begin(h, "webull")
    job = HandoffJournal(h.factory).read(token)
    assert job["local_no_wire"] and job["reason"] == "distance_proven_no_wire"
    assert not h.adapter.cancels and not h.adapter.reads
    h.state.last_quote = replace(h.state.last_quote, ask_price=3.02, bid_price=3.01, last_price=3.02)
    h.service._latest_quotes_by_symbol[h.state.symbol] = {"ask": 3.02, "received_at": h.clock[0]}
    await rpg.feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == "placed"
    assert len(h.adapter.opens) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["buy_flip", "1545"])
async def test_actual_rpg_authorization_cannot_resurrect_after_current_gate(monkeypatch, gate):
    h = await rpg.runtime(monkeypatch, "webull", strategy_overrides=old.ALL_ON)
    FanoutSegmentIdentityStore(h.factory).record(h.state.symbol, h.state.fanout_segment_id, True,
        "CONTROLLED current held RPG segment", now=h.clock[0])
    h.state.last_quote = replace(h.state.last_quote, ask_price=2.5, bid_price=2.49, last_price=2.5)
    h.service._latest_quotes_by_symbol[h.state.symbol] = {"ask": 2.5, "received_at": h.clock[0]}
    token, _ = await rpg.begin(h, "webull")
    await rpg.feedback(h)
    journal = HandoffJournal(h.factory)
    job = journal.read(token)
    assert job["phase"] == "price_wait"
    old_authorization = TradeIntentEvent.model_validate(job["authorization"]["event"])
    assert old_authorization.payload.metadata["rpg_handoff_token"] == str(token)
    rpg.tick_clock(h)
    if gate == "buy_flip":
        h.state.atr_state = "long"
    else:
        h.service.settings.strategy_schwab_1m_v2_entry_window_end_hour_et = 15
        h.service.settings.strategy_schwab_1m_v2_entry_window_end_minute_et = 45
        h.clock[0] = h.clock[0].replace(hour=19, minute=45)
    await rpg.feedback(h)
    assert journal.read(token)["phase"] == "expired"
    await h.service._handle_stream_message({"data": old_authorization.model_dump_json()})
    assert not h.adapter.opens


@pytest.mark.asyncio
async def test_real_legacy_nfq_hold_transfers_and_old_queue_is_fenced(lane):
    # OFF only creates an actual legacy owner; after migration final settings are ON.
    await mirror.test_nfq_held_transfer_invalidates_old_queue_without_two_owners(lane)
    assert lane[0].settings.oms_v2_webull_mirror_retained_hold_enabled


@pytest.mark.asyncio
async def test_real_legacy_nfq_uncertain_transfer_never_second_posts(lane, monkeypatch):
    await mirror.test_nfq_uncertain_transfer_retains_exact_pending_client_and_never_resubmits(lane, monkeypatch)


@pytest.mark.asyncio
async def test_current_retained_queue_and_duplicates_spend_one_wire(lane):
    await mirror.test_queue_and_duplicate_copies_spend_only_one_actual_wire(lane)


@pytest.mark.asyncio
async def test_real_collision_guard_refuses_other_armed_webull_owner(lane):
    service, client, factory, _ = lane
    assert service._fanout_webull_collision_reason.__func__ is OmsRiskService._fanout_webull_collision_reason
    event = mirror.event_for(lane)
    service._armed_hard_stops["CONTROLLED other owner"] = SimpleNamespace(
        broker_account_name=event.payload.broker_account_name, symbol=event.payload.symbol)
    with factory() as session:
        assert service._fanout_webull_collision_reason(session=session,
            broker_account_name=event.payload.broker_account_name, symbol=event.payload.symbol) == "fanout_webull_collision_armed"
    mirror.quote(lane, event, "5.1")
    await service.process_trade_intent(event)
    assert client.calls.get("place", 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["token", "quantity", "price"])
async def test_real_queue_claim_rejects_changed_serial_identity(lane, field):
    # Original queue token is issued by real hold machinery, not a manufactured permit.
    await mirror.test_changed_serial_claim_cannot_wire(lane, field)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["buy_flip", "window_closed", "segment_end"])
async def test_terminal_cancel_fences_actual_retained_queue(lane, reason):
    await mirror.test_controlled_actual_terminal_cancel_fences_queued_generation(lane, reason)


@pytest.mark.asyncio
async def test_restored_queue_fences_actual_old_token(lane):
    await mirror.test_restart_invalidates_queue_but_retains_free_hold(lane)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport_after_post", "crash_after_report"])
async def test_unknown_dispatch_remains_addressable_never_second_posts(lane, monkeypatch, failure):
    await mirror.test_unknown_wire_stays_addressable_without_second_post_after_restart(lane, monkeypatch, failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rejected", "accepted", "partially_filled", "filled"])
async def test_primary_sibling_outcome_never_retires_webull_hold(lane, outcome):
    await mirror.test_xhg_primary_outcome_cannot_retire_webull_leg(lane, outcome)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["partially_filled", "filled"])
async def test_webull_own_fill_terminal_no_rebuy(lane, outcome):
    await mirror.test_mirror_fill_is_one_leg_terminal_and_cannot_rebuy(lane, outcome)


@pytest.mark.asyncio
@pytest.mark.parametrize("broker,quantity,filled", [("schwab",197,31),("webull",98,17)])
async def test_actual_handoff_partial_fill_accounts_old_buy_no_replacement(monkeypatch, broker, quantity, filled):
    await rpg.test_controlled_partial_race_accounts_original_buy_and_protects_actual_position(monkeypatch, broker, quantity, filled)


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal,proven", [("rpg_old_buy_still_owned", True),
    ("CONTROLLED authorization changed after reservation", False)])
async def test_t43_abort_proof_not_arbitrary_refusal(lane, monkeypatch, refusal, proven):
    await mirror.test_pre_wire_callback_refusal_keeps_free_hold_and_can_retry(lane, monkeypatch, refusal, proven)


@pytest.mark.asyncio
async def test_rth_retained_hold_cannot_widen_to_pm_or_next_day(lane):
    await mirror.test_rth_hold_does_not_widen_into_premarket_or_next_session(lane)


@pytest.mark.parametrize("rth", [False, True])
def test_keep_frozen_waiting_buy_without_legacy_expiry(rth, caplog):
    keep.test_waiting_buy_keeps_exact_line_for_hours_without_new_timer(rth, True, caplog)


@pytest.mark.parametrize("rth", [False, True])
def test_keep_sell_cancels_only_waiting_owned_legs(rth):
    keep.test_sell_cancels_one_owned_waiting_order_and_mirror(rth)


@pytest.mark.parametrize("cleanup", ["fill", "fill_after_owner_clear", "window", "gap", "session"])
def test_keep_real_canonical_cleanup(cleanup):
    if cleanup != "gap":
        keep.test_waiting_freeze_canonical_cleanup(cleanup)
        return
    # Legacy unit helper replaces only the reader with lambda True. With
    # restoration ON that denies readiness before its old cleanup branch.
    # Actual gap transitions cancel first, before revoking line readiness.
    strategy, state, clock = keep.seeded(all_on=True)
    keep.track(strategy, state, flip="BUY")
    assert strategy.begin_gap_hold(state.symbol, detected_at_ms=clock[0],
                                   last_bar_age_s=181, last_print_age_s=181)
    assert not state.resting_active and not state.resting_buy_frozen
    assert state.resting_frozen_floor_bar_ms == 0
    assert not strategy.line_buy_ready(state.symbol)
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_fanout_intents()


def test_actual_gap_transition_cancels_frozen_rth_both_owned_legs():
    strategy, state, clock = keep.seeded(all_on=True)
    clock[0] = keep.ms("2026-10-06T14:00:02Z")
    state.bars[-1].timestamp_ms = clock[0]-60_000
    state.resting_is_broker_order = state.webull_resting_active = True
    keep.track(strategy, state, flip="BUY")
    assert state.resting_buy_frozen and state.resting_active
    assert strategy.begin_gap_hold(state.symbol, detected_at_ms=clock[0],
                                   last_bar_age_s=181, last_print_age_s=181)
    primary_cancel, = strategy.drain_pending_intents()
    mirror_cancel, = strategy.drain_webull_direct_intents()
    assert all(d.intent_type == "cancel" and d.metadata["reason"] == "bar_gap"
               for d in (primary_cancel, mirror_cancel))
    assert not state.resting_active and not state.resting_buy_frozen
    assert not strategy.line_buy_ready(state.symbol)


@pytest.mark.parametrize("stream", [False, True])
def test_keep_later_proxy_cross_with_controlled_executable_ask_once(stream):
    keep.test_all_on_later_olox_price_proxy_crosses_once_with_sizing_and_grace(stream)


def test_keep_completed_thin_bars_not_evaluation_count():
    keep.test_three_completed_thin_bars_cancel_not_three_evaluations()


def test_keep_liquid_bar_reset_and_reclaim_unchanged():
    keep.test_liquid_bar_resets_thin_streak_and_reclaim_is_untouched()


@pytest.mark.parametrize("eh", [False, True])
def test_clear_removal_barriers_before_both_session_new_episode(monkeypatch, eh):
    original = clear._strategy

    def with_completed_bar(**kwargs):
        strategy, state, clock, writes = original(**kwargs)
        # A controlled completed line prerequisite, not a line-readiness waiver.
        state.bars.append(OHLCVBar(clock[0]-60_000, 2.6, 2.6, 2.6, 2.6, 25_000))
        state.atr_state, state.atr_trail = "short", 2.5
        return strategy, state, clock, writes

    monkeypatch.setattr(clear, "_strategy", with_completed_bar)
    clear.test_removal_cancel_then_terminal_clear_readd_new_episode_both_sessions(eh)


@pytest.mark.parametrize("fault", ["missing_receipt", "unknown_cancel", "late_fill", "position", "nfq_hold", "rpg_unknown", "missing_webull", "unclassified_abort"])
def test_clear_unknown_or_owned_rows_never_become_absence(fault):
    clear.test_acceptance_faults_fail_closed(fault)


@pytest.mark.parametrize("symbol", ["AIXI", "XHG", "AIFA"])
def test_clear_real_recorded_rows_with_explicit_controlled_terminal_receipts(symbol):
    clear.test_recorded_terminal_rows_with_controlled_receipts_clear(symbol)


def test_list_primary_coalesces_working_rows_without_detail(fake_sdk):
    primary.test_working_orders_share_list_no_detail(fake_sdk)


def test_actual_adapter_constructor_caches_on_and_uses_list(fake_sdk):
    client = primary._FakeClient({"today": primary.listed(primary.row())})
    settings = Settings(_env_file=None, webull_base_url="api.controlled.invalid",
        webull_app_key="CONTROLLED-exact-composition", webull_app_secret="CONTROLLED-not-secret")
    adapter = WebullBrokerAdapter(settings,
        accounts_by_name={"live:orb": WebullAccountConfig(account_id="ACC1")}, client=client)
    assert adapter._list_primary_enabled is settings.webull_list_primary_reads_enabled is True
    assert all(getattr(adapter.settings, name) == value for name, value in EXACT.items())
    assert primary.read(adapter).event_type == "accepted"
    assert client.calls == {"today": 1}


@pytest.mark.parametrize("bad", [primary.listed(), primary.listed(primary.row("foreign")), {"orders": None}])
def test_list_primary_missing_is_unknown_not_flat(fake_sdk, bad):
    primary.test_aifa_111109_unreadable_or_missing_is_unknown_not_flat(fake_sdk, bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["CANCELLED", "REJECTED"])
@pytest.mark.parametrize("previous_quantity", [0, 1, 2])
async def test_list_primary_terminal_partial_real_oms_lifecycle(fake_sdk, status, previous_quantity):
    await primary.test_terminal_partial_real_oms_lifecycle(fake_sdk, status, previous_quantity)


def test_list_primary_strict_permit_and_failed_attempts_count():
    primary.test_query_ceiling_counts_failures_and_reserves_strict_permit()
