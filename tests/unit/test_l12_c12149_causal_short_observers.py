"""C12149 controlled observers and real mathematical/decision kernels, no PG."""

import asyncio
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from project_mai_tai import brokerdecides1_entry as entry
from project_mai_tai.brokerdecides1 import AdmissionMemory, AdmissionSnapshot, Decision, NativeAccountBook
from project_mai_tai.strategy_core.schwab_1m_v2 import SymbolState, OHLCVBar
from project_mai_tai.strategy_core.session_line_restore import (
    HistoryBar, RebuildInput, RebuildResult, SessionLineSnapshot, build_session_line,
)
from tools.l12_causal_short_observer import CausalShortObserver

NOW = 1793002200000
SHORT = 1793002080000


def setup(*, limit=50, segment=SHORT, real_strategy=False):
    state = SymbolState("YMAT")
    state.atr_state, state.atr_short_flip_bar_ts = "short", segment
    state.bars.append(OHLCVBar(NOW - 60000, 1.0, 1.1, .9, 1.0, 100))
    settings = SimpleNamespace(strategy_schwab_1m_v2_brokerdecides1_enabled=True,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
        strategy_schwab_1m_v2_brokerdecides1_max_age_ms=20000)
    if real_strategy:
        from project_mai_tai.settings import Settings
        from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy

        # Recorded paired97ffc965 context, not a historical restored-owner profile.
        strategy = SchwabV2Strategy(Settings(**vars(settings),
            strategy_schwab_1m_v2_flip_owned_first_entry_enabled=False,
            strategy_schwab_1m_v2_retry_one_enabled=False))
        strategy._symbol_states["YMAT"] = state
        strategy._now_ms = lambda: NOW
    else:
        strategy = SimpleNamespace(_symbol_states={"YMAT": state}, settings=settings, _now_ms=lambda: NOW)
    bot = SimpleNamespace(strategy=strategy, _line_published={})
    observer = CausalShortObserver(bot, names=("YMAT",), clock_ns=lambda: NOW * 1000000,
        maximum_records=limit, active_windows=lambda symbol, clock: [dict(event_id="window-1")])
    context = ContextVar("CONTROLLED_C12149", default=None)
    return bot, state, observer, context


def published(state="short", key=SHORT):
    bars = (HistoryBar("YMAT", 1, 1.1, .9, 1, 100, NOW - 60000),)
    request = RebuildInput("YMAT", 1, 1, NOW - 600000, NOW - 60000, bars)
    snapshot = SessionLineSnapshot((("atr_state", state), ("atr_short_flip_bar_ts", key)), (), (), 0)
    return RebuildResult(request, snapshot)


def controlled_snapshot(*, complete):
    books = tuple(NativeAccountBook(account=account, provider=provider, wire_started_ms=NOW - 2,
        wire_completed_ms=NOW - 1, working=frozenset(), positions=frozenset(), complete=complete,
        reason="CONTROLLED_TEST_ONLY_NOT_NATIVE") for account, provider in (
            ("live:schwab_1m_v2", "schwab"), ("live:orb", "webull")))
    return AdmissionSnapshot(epoch="CONTROLLED", generation=1, started_ms=NOW - 2,
        completed_ms=NOW - 1, books=books, inflight=frozenset(), filled_segments=frozenset(),
        local_complete=complete, reason="CONTROLLED_TEST_ONLY_NOT_NATIVE")


def test_entry_delegates_once_exact_args_result_and_window_context():
    bot, state, observer, context = setup()
    calls, decision = [], Decision(False, "controlled_reason", None)

    def original(strategy, symbol):
        calls.append((strategy, symbol))
        return decision

    current = dict(symbol="YMAT")
    token = context.set(current)
    try:
        result = observer.wrap_entry_decision(original, evaluation_context=context)(bot.strategy, "YMAT")
    finally:
        context.reset(token)
    assert result is decision and calls == [(bot.strategy, "YMAT")]
    assert observer.records[0]["window_ids"] == ["window-1"]
    assert current["actual_l12_decisions"][0]["reason"] == "controlled_reason"
    assert state.atr_short_flip_bar_ts == SHORT


def test_real_snapshot_unknown_does_not_claim_segment_check_resolved():
    # C11849 accepts the explicitly augmented real-constructor context, not an
    # invented OLD counterpart to entry_decision on the original namespace.
    bot, state, observer, context = setup(real_strategy=True)
    memory = AdmissionMemory()
    memory.snapshot = controlled_snapshot(complete=False)
    bot.strategy._brokerdecides1_memory = memory
    result = observer.wrap_entry_decision(entry.entry_decision, evaluation_context=context)(bot.strategy, "YMAT")
    assert bot.strategy._strict_first_rest_admitted(state, slot="first") is True  # OLD9c paired97ffc965 TRUE.
    assert result.reason == "outside_L12_exception" and result.allowed is True
    row = observer.records[0]
    assert row["before"]["line_segment"] == SHORT
    assert row["local_segment_operand"] == "OBSERVED_POSITIVE_KEY_ONLY"
    assert row["segment_check_exercised"] == "NOT_INFERRED_FROM_OTHER_REASON"
    assert row["permission_granted"] is False


def test_real_fill_segment_unknown_reason_recorded_without_inventing_key():
    bot, state, observer, context = setup(segment=0, real_strategy=True)
    memory = AdmissionMemory()
    memory.snapshot = controlled_snapshot(complete=True)  # Explicit synthetic kernel adversary, not runtime role proof.
    bot.strategy._brokerdecides1_memory = memory
    result = observer.wrap_entry_decision(entry.entry_decision, evaluation_context=context)(bot.strategy, "YMAT")
    assert bot.strategy._strict_first_rest_admitted(state, slot="first") is True  # OLD9c paired97ffc965 TRUE.
    assert result.reason == "outside_L12_exception" and result.allowed is True
    assert observer.records[0]["segment_check_exercised"] == "NOT_INFERRED_FROM_OTHER_REASON"
    assert state.atr_short_flip_bar_ts == 0


@pytest.mark.parametrize("fault", ["snapshot_UNKNOWN", "fill_segment_UNKNOWN"])
def test_actual_cap_exception_still_refuses_unknown_native_or_segment(fault):
    bot, state, observer, context = setup(segment=0 if fault == "fill_segment_UNKNOWN" else SHORT,
        real_strategy=True)
    state.cw_seed_cap_watch_start_ms = NOW - 60000
    state.cw_resting_taken = state.cw_reclaim_taken = True
    memory = AdmissionMemory()
    memory.snapshot = controlled_snapshot(complete=fault != "snapshot_UNKNOWN")
    bot.strategy._brokerdecides1_memory = memory
    result = observer.wrap_entry_decision(entry.entry_decision, evaluation_context=context)(bot.strategy, "YMAT")
    assert result.allowed is False and result.reason == fault
    assert observer.records[0]["permission_granted"] is False
    assert observer.records[0]["segment_check_exercised"] == (
        "ACTUAL_FILL_SEGMENT_REFUSAL" if fault == "fill_segment_UNKNOWN"
        else "NOT_INFERRED_FROM_OTHER_REASON")


@pytest.mark.parametrize("guard", ["restore", "retry_budget", "owner_evidence"])
def test_outside_exception_preserves_real_legacy_refusals_without_memory_read(guard):
    bot, state, _, _ = setup(real_strategy=True)
    strategy = bot.strategy
    strategy._flip_owned_first_entry_enabled = True
    if guard == "restore":
        strategy._flip_owner_restore_readable = False
    elif guard == "retry_budget":
        strategy._retry_one_enabled = True
        state.retry_one_budget_readable = False
    else:
        state.flip_owner_evidence_readable = False

    class NoRead:
        def assess(self, **kwargs):
            raise AssertionError("ordinary_legacy_guard_must_not_read_native_memory")

    strategy._brokerdecides1_memory = NoRead()
    assert entry.entry_decision(strategy, "YMAT").reason == "outside_L12_exception"
    assert strategy._strict_first_rest_admitted(state, slot="first") is False


def test_foreign_strategy_passes_through_once_without_observation():
    bot, state, observer, context = setup()
    other, calls, result = object(), [], Decision(True, "flag_off", None)

    def original(strategy, symbol):
        calls.append((strategy, symbol))
        return result

    assert observer.wrap_entry_decision(original, evaluation_context=context)(other, "YMAT") is result
    assert calls == [(other, "YMAT")] and observer.records == []


def test_each_real_nested_invocation_is_retained_not_recalled_or_collapsed():
    bot, state, observer, context = setup()
    calls = []

    def original(strategy, symbol):
        calls.append(symbol)
        return Decision(False, "snapshot_UNKNOWN", None)

    wrapped = observer.wrap_entry_decision(original, evaluation_context=context)
    wrapped(bot.strategy, "YMAT")
    wrapped(bot.strategy, "YMAT")
    assert calls == ["YMAT", "YMAT"] and len(observer.records) == 2


@pytest.mark.parametrize("error", [ValueError("CONTROLLED"), asyncio.CancelledError()])
def test_entry_primary_exception_identity_survives_observer_failure(error, monkeypatch):
    bot, state, observer, context = setup()

    def original(*args):
        raise error

    def fail(row):
        raise RuntimeError("controlled_observer")

    monkeypatch.setattr(observer, "_append", fail)
    with pytest.raises(type(error)) as caught:
        observer.wrap_entry_decision(original, evaluation_context=context)(bot.strategy, "YMAT")
    assert caught.value is error
    assert "causal_observer_failure_type=RuntimeError" in caught.value.__notes__


@pytest.mark.asyncio
async def test_rebuild_delegates_once_with_kwargs_and_reads_real_publication_after_return():
    bot, state, observer, context = setup()
    calls = []

    async def original(symbol, ledger, *, prefix_through_ms):
        calls.append((symbol, ledger, prefix_through_ms))
        bot._line_published[symbol] = published()
        return True

    ledger = object()
    assert await observer.wrap_rebuild(original)("YMAT", ledger, prefix_through_ms=NOW - 60000) is True
    assert calls == [("YMAT", ledger, NOW - 60000)]
    assert observer.records[0]["status"] == "OBSERVED_SHORT_KEY_WRITE_THROUGH_NOT_ENTRY_PERMISSION"
    assert observer.records[0]["published"]["prefix_count"] == 1


@pytest.mark.asyncio
async def test_cached_true_is_not_a_new_sell_publication():
    bot, state, observer, context = setup()
    bot._line_published["YMAT"] = published()

    async def original(*args):
        return True

    await observer.wrap_rebuild(original)("YMAT")
    assert observer.records[0]["status"] == "CACHED_CURRENT_LINE_NOT_NEW_SELL"


@pytest.mark.asyncio
async def test_cached_snapshot_does_not_claim_fresh_publication_or_repair_live_buy_state():
    bot, state, observer, context = setup(segment=0)
    bot._line_published["YMAT"] = published()
    state.atr_state = "long"

    async def original(*args):
        return True

    await observer.wrap_rebuild(original)("YMAT")
    assert observer.records[0]["status"] == "CACHED_CURRENT_LINE_NOT_NEW_SELL"
    assert state.atr_state == "long" and state.atr_short_flip_bar_ts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("key,live", [(0, 0), (SHORT, 0), (SHORT, SHORT - 60000)])
async def test_successful_short_key_contradiction_is_recorded_never_repaired(key, live):
    bot, state, observer, context = setup(segment=live)

    async def original(*args):
        bot._line_published["YMAT"] = published(key=key)
        return True

    await observer.wrap_rebuild(original)("YMAT")
    assert observer.records[0]["status"] == "STOP_SUCCESSFUL_SHORT_KEY_PUBLICATION_CONTRADICTION"
    assert state.atr_short_flip_bar_ts == live


@pytest.mark.asyncio
async def test_long_prefix_and_no_sell_not_false_p1():
    bot, state, observer, context = setup(segment=0)
    state.atr_state = "long"

    async def original(*args):
        bot._line_published["YMAT"] = published(state="long", key=0)
        return True

    await observer.wrap_rebuild(original)("YMAT")
    assert observer.records[0]["status"] == "OBSERVED_NONSHORT_PREFIX_NO_SELL_GUARANTEE"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("CONTROLLED"), asyncio.CancelledError()])
async def test_rebuild_exception_or_cancel_is_not_refusal_or_pass(error):
    bot, state, observer, context = setup()

    async def original(*args):
        raise error

    with pytest.raises(type(error)) as caught:
        await observer.wrap_rebuild(original)("YMAT")
    assert caught.value is error
    assert observer.records[0]["status"] == "ORIGINAL_CALL_RAISED_NOT_REFUSAL_OR_PASS"


def test_buy_clears_live_short_but_exact_fresh_buy_lineage_is_observed():
    bot, state, observer, context = setup()
    before = observer._state("YMAT")
    state.atr_state, state.atr_short_flip_bar_ts = "long", 0
    state.slotclear_fresh_buy_bar_ms = NOW - 60000
    state.slotclear_fresh_buy_segment_id = SHORT
    current = dict(symbol="YMAT")
    observer.signal(state, dict(flip="BUY", decision_short_segment_id=SHORT,
        decision_cur_bar_ts=NOW - 60000, observation_phase="replay"), before=before, context=current, state_only=True)
    row = observer.records[0]
    assert row["after"]["atr_short_flip_bar_ts"] == 0 and row["after"]["line_segment"] == SHORT
    assert row["signal"]["decision_short_segment_id"] == SHORT
    assert current["actual_causal_signals"][0]["state_only"] is True


def test_no_flip_does_not_manufacture_signal_or_change_state():
    bot, state, observer, context = setup()
    before = observer._state("YMAT")
    observer.signal(state, dict(flip=None), before=before, context=None, state_only=True)
    assert observer.records == [] and observer._state("YMAT") == before


def test_sample_uses_full_request_prefix_not_truncated_state_deque():
    bot, state, observer, context = setup()
    result = published()
    bars = tuple(HistoryBar("YMAT", 1, 1, 1, 1, 100, NOW - (400 - i) * 60000) for i in range(400))
    bot._line_published["YMAT"] = RebuildResult(RebuildInput("YMAT", 2, 4, NOW - 400 * 60000,
        NOW - 60000, bars), result.snapshot)
    sample = observer.sample("YMAT")
    assert sample["published"]["prefix_count"] == 400 and len(state.bars) == 1
    assert observer.records == []


def test_report_keeps_all34_windows_and_unobserved_calls_unknown():
    bot, state, observer, context = setup()
    result = observer.report([dict(event_id=f"w{i}") for i in range(34)])
    assert result["window_denominator"] == 34
    assert len(result["windows"]) == 34
    assert all("NO_ACTUAL_L12_CALL_OBSERVED" in row["missing_witnesses"] for row in result["windows"])
    assert result["permission_granted"] is False and result["observed_contradictions"] == []


def test_bounded_overflow_is_explicit_not_silent_record_drop():
    bot, state, observer, context = setup(limit=1)
    wrapped = observer.wrap_entry_decision(lambda *a: Decision(False, "snapshot_UNKNOWN", None), evaluation_context=context)
    wrapped(bot.strategy, "YMAT")
    with pytest.raises(ValueError, match="INCOMPLETE_STOP"):
        wrapped(bot.strategy, "YMAT")
    assert len(observer.records) == 1


def test_scoped_module_patch_restores_original_after_exception():
    bot, state, observer, context = setup()
    original = entry.entry_decision
    with pytest.raises(RuntimeError):
        with patch.object(entry, "entry_decision", observer.wrap_entry_decision(original, evaluation_context=context)):
            raise RuntimeError("CONTROLLED")
    assert entry.entry_decision is original


@pytest.mark.parametrize("budget", [None, 0, -1, True])
def test_observer_cannot_guess_or_disable_finite_budget(budget):
    with pytest.raises(ValueError):
        setup(limit=budget)


def real_prefix(last_buy=False):
    anchor = NOW - 10 * 60000
    prices = [5.0] * 9 + [1.0] + ([10.0] if last_buy else [])
    bars = tuple(HistoryBar("YMAT", p, p + .01, p - .01, p, 100, anchor + i * 60000)
        for i, p in enumerate(prices))
    return RebuildInput("YMAT", 1, 1, anchor, bars[-1].timestamp_ms, bars)


def test_real_period5_full_short_prefix_needs_zero_additional_bars():
    request = real_prefix()
    snapshot = build_session_line(request, 5, 3.5)
    indicator = dict(snapshot.indicator)
    assert indicator["atr_state"] == "short"
    assert indicator["atr_short_flip_bar_ts"] == NOW - 60000
    assert dict(snapshot.signal)["flip"] == "SELL"


def test_real_buy_return_carries_previous_short_even_when_live_indicator_clears():
    request = real_prefix(last_buy=True)
    snapshot = build_session_line(request, 5, 3.5)
    assert dict(snapshot.indicator)["atr_state"] == "long"
    assert dict(snapshot.indicator)["atr_short_flip_bar_ts"] == 0
    assert dict(snapshot.signal)["flip"] == "BUY"
    assert dict(snapshot.signal)["decision_short_segment_id"] == NOW - 60000


@pytest.mark.asyncio
async def test_actual_installer_both_ports_delegate_once_and_restore_after_primary():
    bot, state, observer, context = setup()
    calls = []

    async def rebuild(symbol, *, prefix_through_ms):
        calls.append(("rebuild", symbol, prefix_through_ms))
        bot._line_published[symbol] = published()
        return True

    def decision(strategy, symbol):
        calls.append(("decision", strategy, symbol))
        return Decision(False, "snapshot_UNKNOWN", None)

    bot._rebuild_session_line = rebuild
    module = SimpleNamespace(entry_decision=decision)
    primary = ValueError("CONTROLLED")
    with pytest.raises(ValueError) as caught:
        with observer.bound(module, evaluation_context=context):
            assert await bot._rebuild_session_line("YMAT", prefix_through_ms=NOW - 60000) is True
            assert module.entry_decision(bot.strategy, "YMAT").reason == "snapshot_UNKNOWN"
            raise primary
    assert caught.value is primary
    assert bot._rebuild_session_line is rebuild and module.entry_decision is decision
    assert calls == [("rebuild", "YMAT", NOW - 60000), ("decision", bot.strategy, "YMAT")]
    assert [row["kind"] for row in observer.records] == ["ACTUAL_LINE_REBUILD", "ACTUAL_L12_ENTRY_DECISION"]


def test_real_seed_observer_adds_line_fields_without_setting_readiness_or_short():
    from tests.line_restore_acceptance_factory import LineRestoreCase
    from tools.l12_service_boot_replay import observe_combined_seed_state

    case = LineRestoreCase(symbol="YMAT", now_ms=NOW, settings_overrides={
        "strategy_schwab_1m_v2_keepwatch1_enabled": True,
        "strategy_schwab_1m_v2_brokerdecides1_enabled": True,
    })
    observer = CausalShortObserver(case.bot, names=("YMAT",), clock_ns=lambda: NOW * 1000000,
        maximum_records=10, active_windows=lambda *a: [dict(event_id="CONTROLLED")])
    before = case.strategy.watchlist_state("YMAT").atr_short_flip_bar_ts
    sample = observe_combined_seed_state(case, causal_observer=observer)
    row = next(row for row in sample["symbols"] if row["symbol"] == "YMAT")
    assert row["seed_complete"] is False
    assert row["causal_line_witness"]["state"]["atr_short_flip_bar_ts"] == before == 0
    assert row["causal_line_witness"]["published"]["status"] == "NO_PUBLISHED_PREFIX_OBSERVED"
    assert observer.records == []


def test_each_readd_window_gets_its_own_first_buy_not_only_first_per_name():
    bot, state, observer, context = setup()
    active = ["w1"]
    observer.active_windows = lambda *a: [dict(event_id=active[0])]
    observed = dict(symbol="YMAT", flip="BUY", status="NO_BUY_DRAFT_ACTUAL_GUARD_REASONS_OR_UNKNOWN",
        bar_ms=NOW - 60000, actual_return_reasons=[], returned_BUY_draft=False)
    observer.evaluation(observed)
    active[0] = "w2"
    observer.evaluation(observed)
    result = observer.report([dict(event_id="w1"), dict(event_id="w2")])
    assert [row["first_BUY_evaluation_id"] for row in result["windows"]] == [0, 1]
    assert result["permission_granted"] is False


def test_actual_restored_buy_argument_is_not_relabelled_indicator_call():
    bot, state, observer, context = setup()
    current = dict(symbol="YMAT")
    observer.signal(state, dict(flip="BUY", decision_short_segment_id=SHORT),
        before=observer._state("YMAT"), context=current, state_only=False,
        origin="ACTUAL_RESTORED_SIGNAL_ARGUMENT")
    assert observer.records[0]["kind"] == "ACTUAL_RESTORED_SIGNAL_ARGUMENT"
    assert observer.records[0]["signal"]["decision_short_segment_id"] == SHORT


def test_actual_slot_context_is_recorded_without_guessing_non_strict_callers():
    bot, state, observer, context = setup()
    token = context.set(dict(symbol="YMAT", actual_strategy_slot="first"))
    wrapped = observer.wrap_entry_decision(lambda *a: Decision(False, "snapshot_UNKNOWN", None), evaluation_context=context)
    try:
        wrapped(bot.strategy, "YMAT")
    finally:
        context.reset(token)
    wrapped(bot.strategy, "YMAT")
    assert [row["slot_context"] for row in observer.records] == ["first", "UNKNOWN_NO_STRICT_SLOT_CONTEXT"]
