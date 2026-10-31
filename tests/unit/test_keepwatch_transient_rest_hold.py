"""C7-F1 controlled owned-attempt/rest operands; no native or NCPL replay claim.

Real service hold/entry fence and strategy cancel builder are exercised. The
source ledger's stale-rebuild state is an explicit unit input, not a reproduced
historical race. The original NCPL causal replay remains separately required.
"""
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from project_mai_tai.services.keepwatch_rest_hold import RestHoldPolicy, RestHoldProof
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.session_line_restore import SessionLineRestoration


ANCHOR = int(datetime(2026, 10, 29, 8, tzinfo=UTC).timestamp() * 1000)
SYMBOL = "NCPL"


def proof():
    return RestHoldProof(SYMBOL, ANCHOR, 1, object(), object(), ("CONTROLLED", 1),
                         None, 0, ("CONTROLLED-rest-generation",), object())


def test_only_first_distinct_attempt_is_deferred_and_failed_cancel_cannot_rearm():
    policy, first = RestHoldPolicy(), proof()
    assert policy.cancel_required(SYMBOL, first) is False
    assert policy.cancel_required(SYMBOL, replace(first)) is False
    assert policy.cancel_required(SYMBOL, replace(first, attempt=object())) is True
    assert policy.cancel_required(SYMBOL, first) is True
    assert policy.cancel_required(SYMBOL, replace(first, attempt=object())) is True


@pytest.mark.parametrize("field", ["ledger", "state", "source_identity", "work", "trade_token", "rest_identity", "epoch", "anchor_ms"])
def test_scope_change_cancels_in_same_attempt_and_latches(field):
    policy, first = RestHoldPolicy(), proof()
    assert policy.cancel_required(SYMBOL, first) is False
    values = dict(ledger=object(), state=object(), source_identity=("changed",),
                  work=object(), trade_token=1, rest_identity=("changed",), epoch=2, anchor_ms=ANCHOR+86400000)
    assert policy.cancel_required(SYMBOL, replace(first, **{field: values[field]})) is True
    assert policy.cancel_required(SYMBOL, first) is True


def test_hard_reason_before_rest_ack_does_not_grant_late_grace():
    policy, first = RestHoldPolicy(), proof()
    assert policy.cancel_required(SYMBOL) is True
    assert policy.cancel_required(SYMBOL, first) is True
    policy.clear_proven(SYMBOL)  # Explicit controlled proof transition.
    assert policy.cancel_required(SYMBOL, first) is False


def test_bounded_policy_refuses_overflow_and_does_not_touch_other_symbol():
    policy, first = RestHoldPolicy(), proof()
    policy.MAX_PENDING = 1
    assert policy.cancel_required(SYMBOL, first) is False
    assert policy.cancel_required("PCLA", replace(first, symbol="PCLA")) is True
    assert len(policy._pending) == 1
    assert policy.cancel_required(SYMBOL, first) is False
    policy.retain_members({"PCLA"})
    assert not policy._pending


def controlled_service(*, enabled=True, mirror=True):
    bot = SchwabV2BotService(Settings(_env_file=None,
        strategy_schwab_1m_v2_keepwatch1_enabled=True,
        strategy_schwab_1m_v2_line_chart_restoration_enabled=True,
        strategy_schwab_1m_v2_keepwatch_sized_trade_gap_enabled=True,
        strategy_schwab_1m_v2_keepwatch_transient_rest_hold_enabled=enabled,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_account_name="paper:controlled",
        strategy_schwab_1m_v2_webull_account_name="paper:controlled-webull" if mirror else "",
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=mirror))
    bot.strategy._now_ms = lambda: ANCHOR + 8*3600000 + 61000
    bot._watchlist = {SYMBOL}
    state = bot.strategy.watchlist_state(SYMBOL)
    state.resting_active = True
    state.resting_is_broker_order = True
    state.resting_schwab_generation = "CONTROLLED-original-primary"
    state.resting_schwab_quantity = 36
    state.resting_level = state.resting_trigger = 8.42
    state.fanout_segment_id = ANCHOR+8*3600000
    state.webull_resting_active = mirror
    state.webull_resting_generation_id = "CONTROLLED-original-mirror" if mirror else ""
    state.resting_webull_generation = "CONTROLLED-original-webull" if mirror else ""
    state.resting_webull_quantity = 36 if mirror else 0
    ledger = SessionLineRestoration(SYMBOL, ANCHOR, 1)
    ledger.incomplete_reason = "stale_rebuild"
    bot._line_sessions[SYMBOL] = ledger
    identity = ("CONTROLLED-source", 1)
    bot._line_source_identity[SYMBOL] = identity
    operation = object()
    scope = (SYMBOL, ANCHOR, 1)
    bot._keepwatch_history_context.set((operation, scope))
    bot._keepwatch_history_scopes[operation] = scope
    operands = dict(ledger=ledger, epoch=1, state=state, source_identity=identity,
        expected_work=None, trade_through=ANCHOR+8*3600000, trade_token=0, anchor=ANCHOR)
    return bot, state, operands


@pytest.mark.parametrize("mirror", [False, True])
def test_real_hold_retains_existing_rest_but_blocks_new_entry_then_next_attempt_cancels(mirror):
    bot, state, operands = controlled_service(mirror=mirror)
    bot._keepwatch_hold_unknown(SYMBOL, "prefix_publication_unproven", transient_scope=operands)
    assert state.resting_active and state.webull_resting_active is mirror
    assert not bot.strategy.drain_pending_intents()
    assert not bot.strategy.drain_webull_direct_intents()
    assert SYMBOL in bot._keepwatch_unknown and not bot._keepwatch_entry_allowed(SYMBOL)
    assert SYMBOL not in bot._keepwatch_cancel_requested
    bot._keepwatch_hold_unknown(SYMBOL, "prefix_publication_unproven", transient_scope=operands)
    assert state.resting_active
    operation = object()
    scope = (SYMBOL, ANCHOR, 1)
    bot._keepwatch_history_context.set((operation, scope))
    bot._keepwatch_history_scopes[operation] = scope
    bot._keepwatch_hold_unknown(SYMBOL, "prefix_publication_unproven", transient_scope=operands)
    assert not state.resting_active and not state.webull_resting_active
    assert SYMBOL in bot._keepwatch_cancel_requested
    assert bot.strategy.drain_pending_intents()
    assert bool(bot.strategy.drain_webull_direct_intents()) is mirror


@pytest.mark.parametrize("reason", ["empty_provider_conflicts_with_trade", "bar_inbox_overflow",
    "bar_owner_stopped_unexpectedly", "provider_or_publication_error_ValueError",
    "prefix_reconciliation_unproven", "minute_not_closed", "observation_epoch_missing"])
def test_other_reasons_cancel_immediately_even_after_soft_deferral(reason):
    bot, state, operands = controlled_service()
    bot._keepwatch_hold_unknown(SYMBOL, "prefix_publication_unproven", transient_scope=operands)
    bot._keepwatch_hold_unknown(SYMBOL, reason, transient_scope=operands)
    assert not state.resting_active and not state.webull_resting_active
    assert SYMBOL in bot._keepwatch_unknown


@pytest.mark.parametrize("mutation", ["disabled", "not_stale", "no_attempt", "unowned_attempt",
    "ledger", "epoch", "source", "state", "trade", "quarantine", "membership", "soft_rest", "missing_generation"])
def test_unproved_soft_scope_cancels_immediately(mutation):
    bot, state, operands = controlled_service(enabled=mutation != "disabled")
    ledger = operands["ledger"]
    if mutation == "not_stale":
        ledger.incomplete_reason = "coverage_unproven"
    elif mutation == "no_attempt":
        bot._keepwatch_history_context.set(None)
    elif mutation == "unowned_attempt":
        bot._keepwatch_history_scopes.clear()
    elif mutation == "ledger":
        bot._line_sessions[SYMBOL] = SessionLineRestoration(SYMBOL, ANCHOR, 1)
    elif mutation == "epoch":
        ledger.epoch += 1
    elif mutation == "source":
        bot._line_source_identity[SYMBOL] = ("CONTROLLED-changed", 2)
    elif mutation == "state":
        operands["state"] = object()
    elif mutation == "trade":
        ledger.observe_trade(operands["trade_through"], track_generation=True)
    elif mutation == "quarantine":
        bot._keepwatch_bar_quarantine[SYMBOL] = ANCHOR
    elif mutation == "membership":
        bot._watchlist.clear()
    elif mutation == "soft_rest":
        state.resting_is_broker_order = False
    elif mutation == "missing_generation":
        state.resting_schwab_generation = state.resting_webull_generation = ""
    bot._keepwatch_hold_unknown(SYMBOL, "prefix_publication_unproven", transient_scope=operands)
    assert not state.resting_active and not state.webull_resting_active
    assert SYMBOL in bot._keepwatch_unknown


def test_diagnostic_failure_does_not_erase_pending_or_grant_placement(monkeypatch):
    bot, state, operands = controlled_service()
    def broken(*args, **kwargs):
        raise RuntimeError("CONTROLLED-diagnostic-failure")
    monkeypatch.setattr(bot, "_keepwatch_log_unknown", broken)
    bot._keepwatch_hold_unknown(SYMBOL, "prefix_publication_unproven", transient_scope=operands)
    bot._keepwatch_forget_unknown_episode(SYMBOL)
    assert state.resting_active and SYMBOL in bot._keepwatch_unknown
    assert not bot._keepwatch_entry_allowed(SYMBOL)
    bot._keepwatch_hold_unknown(SYMBOL, "bar_inbox_overflow")
    assert not state.resting_active


def test_cancel_callback_failure_latches_immediate_retry_without_new_grace(monkeypatch):
    bot, state, operands = controlled_service()
    bot._keepwatch_hold_unknown(SYMBOL, "prefix_publication_unproven", transient_scope=operands)
    real_cancel = bot.strategy._queue_resting_cancel
    def broken(*args, **kwargs):
        raise RuntimeError("CONTROLLED-cancel-failure")
    monkeypatch.setattr(bot.strategy, "_queue_resting_cancel", broken)
    with pytest.raises(RuntimeError):
        bot._keepwatch_hold_unknown(SYMBOL, "bar_inbox_overflow")
    monkeypatch.setattr(bot.strategy, "_queue_resting_cancel", real_cancel)
    bot._keepwatch_hold_unknown(SYMBOL, "prefix_publication_unproven", transient_scope=operands)
    assert not state.resting_active


def test_default_is_off():
    assert Settings(_env_file=None).strategy_schwab_1m_v2_keepwatch_transient_rest_hold_enabled is False
