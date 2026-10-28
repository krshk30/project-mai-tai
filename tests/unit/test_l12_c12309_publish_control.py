"""CONTROLLED lifecycle delegation and pure schedule; no PG or corrected APP claim."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tools.l12_recorded_boot_driver import (
    PUBLISH_CONTROL_SCOPE, PUBLISH_INTERVAL_NS, PublishControl, publish_control_steps,
)

START = 1793001600000000000
RULING = "a" * 64


def event(kind, offset=0):
    return dict(kind=kind, clock_ns=START + offset, original_clock_ns=1792742400000000000 + offset,
        watchlist=["YMAT"] if kind in {"membership", "scanner_delta"} else None,
        raw_ref="CONTROLLED", raw_sha256="b" * 64)


def schedule(events=None, budget=3, ruling=RULING):
    return publish_control_steps(events if events is not None else [event("observe_end", 10_000_000_000)],
        start_ns=START, maximum_boundaries=budget, ruling_sha256=ruling)


class ControlledService:
    """Injected unit service, never a production instance or completeness proof."""

    def __init__(self):
        self.calls = 0
        self.error = None
        self.strategy = SimpleNamespace(_entries_held=True)
        self._boot_state_restoration_complete = False
        self._watchlist = {"YMAT"}
        self._boot_scanner_selected = {"YMAT"}
        self._boot_exclusion_sources_readable = True
        self._rest_warmup_done = set()
        self._boot_warmup_timeout_released = set()

    def _cw_boot_hold_check(self):
        self.calls += 1
        if self.error is not None:
            raise self.error


def control():
    steps = schedule()
    return PublishControl(steps), [step for step in steps if step["kind"] == "state_publish_control"]


def invoke(control, bot, step, guard=lambda: True):
    return control.invoke(bot, type(bot), step, clock_ns=step["clock_ns"], copy_guard=guard)


def test_phase_zero_exact_five_seconds_includes_start_and_terminal():
    rows = schedule()
    clocks = [row["clock_ns"] for row in rows if row["kind"] == "state_publish_control"]
    assert clocks == [START, START + 5_000_000_000, START + 10_000_000_000]
    assert PUBLISH_INTERVAL_NS == 5_000_000_000
    assert all(row["original_clock_ns"] is None for row in rows if row["kind"] == "state_publish_control")


def test_same_clock_source_order_retained_control_after_mutations_before_observations():
    original = [event("membership"), event("history_poll"), event("observe_seed"),
        event("history_poll", 5_000_000_000), event("observe_end", 5_000_000_000)]
    before = deepcopy(original)
    rows = schedule(original)
    assert [row["kind"] for row in rows] == ["membership", "history_poll", "state_publish_control",
        "observe_seed", "history_poll", "state_publish_control", "observe_end"]
    assert [row for row in rows if row["kind"] != "state_publish_control"] == original == before


def test_observation_sample_does_not_delegate_control():
    tracker, _ = control()
    assert tracker.report()["actual"] == 0 and not tracker.report()["complete"]


@pytest.mark.parametrize("budget", [0, -1, True, 100001, 2])
def test_invalid_or_insufficient_budget_refuses_no_growth(budget):
    with pytest.raises(ValueError):
        schedule(budget=budget)


@pytest.mark.parametrize("ruling", [None, "0" * 64, "g" * 64, "a"])
def test_exact_nonzero_ruling_pin_required(ruling):
    with pytest.raises(ValueError):
        schedule(ruling=ruling)


def test_mutation_after_observation_same_clock_is_not_repaired():
    with pytest.raises(ValueError, match="not_reordered"):
        schedule([event("observe_seed"), event("history_poll")])


def test_source_clock_rewind_is_not_sorted_away():
    with pytest.raises(ValueError, match="cannot_repair"):
        schedule([event("history_poll", 5_000_000_000), event("membership")])


def test_input_cannot_insert_its_own_control_boundary():
    with pytest.raises(ValueError, match="cannot_repair"):
        schedule([event("state_publish_control")])


def test_actual_method_delegates_once_per_sealed_step_and_never_grants_permission():
    tracker, steps = control()
    bot = ControlledService()
    for step in steps:
        invoke(tracker, bot, step)
    report = tracker.report()
    assert bot.calls == 3 and report["actual"] == report["expected"] == 3
    assert report["complete"] is True and report["permission_granted"] is False
    assert report["scope"] == PUBLISH_CONTROL_SCOPE
    assert report["monotonic_warmup_timeout"] == "UNKNOWN"
    assert bot.strategy._entries_held and not bot._boot_state_restoration_complete


def test_duplicate_boundary_is_failure_not_retry():
    tracker, steps = control()
    bot = ControlledService()
    invoke(tracker, bot, steps[0])
    with pytest.raises(ValueError, match="exact_once"):
        invoke(tracker, bot, steps[0])
    with pytest.raises(ValueError, match="exact_once"):
        invoke(tracker, bot, steps[1])
    assert bot.calls == 1 and not tracker.report()["complete"]


def test_foreign_boundary_or_source_pin_stops_before_delegation():
    tracker, steps = control()
    bot = ControlledService()
    foreign = dict(steps[0], raw_sha256="c" * 64)
    with pytest.raises(ValueError, match="exact_once"):
        invoke(tracker, bot, foreign)
    assert tracker.failed and bot.calls == 0


def test_instance_wrapped_method_refused():
    tracker, steps = control()
    bot = ControlledService()
    bot._cw_boot_hold_check = lambda: None
    with pytest.raises(ValueError, match="actual_holdcheck"):
        invoke(tracker, bot, steps[0])
    assert bot.calls == 0


@pytest.mark.parametrize("when", ["before", "after"])
def test_copy_guard_loss_is_stop_and_retains_attempt(when):
    tracker, steps = control()
    bot, calls = ControlledService(), []

    def guard():
        calls.append(1)
        return when == "after" and len(calls) == 1

    with pytest.raises(ValueError, match="copy_fence_lost"):
        invoke(tracker, bot, steps[0], guard)
    assert bot.calls == (1 if when == "after" else 0)
    assert tracker.records[0]["status"] == "FAILED_NO_RETRY" and tracker.failed


@pytest.mark.parametrize("error", [ValueError("CONTROLLED"), KeyboardInterrupt()])
def test_original_exception_identity_propagates_and_marks_no_retry(error):
    tracker, steps = control()
    bot = ControlledService()
    bot.error = error
    with pytest.raises(type(error)) as caught:
        invoke(tracker, bot, steps[0])
    assert caught.value is error and bot.calls == 1
    assert tracker.records[0]["exception_type"] == type(error).__name__ and tracker.failed


def test_actual_frozen_APP_hold_kernel_is_called_not_reimplemented():
    from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
    from project_mai_tai.settings import Settings
    tracker, steps = control()
    bot = SchwabV2BotService(Settings(_env_file=None,
        strategy_schwab_1m_v2_keepwatch1_enabled=True,
        strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=True,
        strategy_schwab_1m_v2_line_chart_restoration_enabled=True,
        strategy_schwab_1m_v2_brokerdecides1_enabled=False,
        massive_api_key=""))
    bot.strategy._now_ms = lambda: START // 1_000_000
    bot._watchlist = {"YMAT"}
    bot._boot_scanner_selected = {"YMAT"}
    bot._boot_exclusion_sources_readable = True
    assert bot._keepwatch_enabled and not bot._keepwatch_proven
    assert bot.session_factory is None
    calls = []

    def segments():
        calls.append("actual_kernel_census")
        return []

    bot.strategy.cw_armed_segments = segments
    tracker.invoke(bot, SchwabV2BotService, steps[0], clock_ns=START, copy_guard=lambda: True)
    assert calls == ["actual_kernel_census"]
    assert bot.strategy._entries_held is True
    assert bot._boot_state_restoration_complete is False
    assert not bot._keepwatch_proven  # No history/native proof fabricated by the retry.
    assert tracker.records[0]["corrected_APP_carry"].startswith("PENDING_")


@pytest.mark.asyncio
async def test_existing_scheduler_settles_same_clock_owner_before_control():
    from tools.l12_boot_owner_clock import scheduled
    calls = []
    clock = SimpleNamespace(now_ns=START)

    class Owner:
        async def advance_before(self, stamp):
            clock.now_ns = stamp

        async def wake_due(self):
            calls.append("wake")

        async def settle(self):
            calls.append("settle")

    rows = schedule([event("membership"), event("history_poll"), event("observe_end")])
    async for step in scheduled(rows, clock, Owner()):
        calls.append(step["kind"])
    assert calls.index("history_poll") < calls.index("state_publish_control") < calls.index("observe_end")
    assert calls[calls.index("state_publish_control") - 1] == "wake"
    assert "settle" in calls[calls.index("history_poll") + 1:calls.index("state_publish_control")]
