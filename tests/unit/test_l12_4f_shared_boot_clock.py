"""CONTROLLED clock/driver contracts, not real boot or native qualification."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
import time

import pytest

from tests.unit.test_l12_recorded_boot_driver_contract import prepared, mapping, event
from tests.unit.test_l12_c12309_publish_control import ControlledService, schedule
from tools.l12_boot_owner_clock import (
    AsyncioPort, BOOT_CLOCK_REFERENCE_HEAD, BOOT_CLOCK_REFERENCE_SHA,
    BOOT_DEFERRAL_CLOCK_POLICY, OwnerProtocolRefusal, boot_deferral_clock,
    require_boot_deferral_review,
)
from tools.l12_private_schema_evidence import BOUNDED_L11_APP_COMPONENT
from tools.l12_recorded_boot_driver import PublishControl, RecordedBootDriver, require_4f_boot_driver


def review():
    return dict(version="L12_4F_INSTANCE_ELAPSED_CLOCK_REVIEW_V1",
        app_component=deepcopy(BOUNDED_L11_APP_COMPONENT),
        clock_policy=BOOT_DEFERRAL_CLOCK_POLICY, reference_head=BOOT_CLOCK_REFERENCE_HEAD,
        reference_file_sha256=BOOT_CLOCK_REFERENCE_SHA, review_sha256="a" * 64,
        scope="BOOT_ONLY_ELAPSED_NOT_NATIVE_OR_GLOBAL_TIMEOUTS")


def test_same_elapsed_contract_59999ms_then60000ms_restores_exact_instance_port():
    def prior():
        return 999.0
    bot, clock = SimpleNamespace(_keepwatch_boot_retry_clock=prior), SimpleNamespace(now_ns=10 ** 18)
    real_monotonic, real_wait_for = time.monotonic, asyncio.wait_for
    with boot_deferral_clock(bot, clock) as receipt:
        assert bot._keepwatch_boot_retry_clock() == 0
        clock.now_ns += 59_999_000_000
        assert bot._keepwatch_boot_retry_clock() == 59.999
        clock.now_ns += 1_000_000
        assert bot._keepwatch_boot_retry_clock() == 60
        assert time.monotonic is real_monotonic and asyncio.wait_for is real_wait_for
    assert bot._keepwatch_boot_retry_clock is prior and receipt["restored"] is True
    assert receipt["full_state_publisher"] == "UNEXERCISED"
    assert receipt["warmup_timeout_monotonic"] == "UNMODELED"


def test_repeated_callbacks_do_not_consume_elapsed_budget_or_change_headers():
    bot = SimpleNamespace(_keepwatch_boot_retry_clock=lambda: 1, native_header=123, created_at=456)
    clock = SimpleNamespace(now_ns=20)
    with boot_deferral_clock(bot, clock):
        assert [bot._keepwatch_boot_retry_clock() for _ in range(20)] == [0] * 20
        assert clock.now_ns == 20 and bot.native_header == 123 and bot.created_at == 456


@pytest.mark.parametrize("start", [-1, True, 1.5, None])
def test_invalid_origin_refuses_before_port_change(start):
    def prior():
        return 1
    bot = SimpleNamespace(_keepwatch_boot_retry_clock=prior)
    with pytest.raises(OwnerProtocolRefusal):
        with boot_deferral_clock(bot, SimpleNamespace(now_ns=start)):
            pytest.fail("invalid origin entered")
    assert bot._keepwatch_boot_retry_clock is prior


@pytest.mark.parametrize("value", [9, True, 10.5, None])
def test_rewind_or_noninteger_refuses_without_clock_repair(value):
    def prior():
        return 1
    bot, clock = SimpleNamespace(_keepwatch_boot_retry_clock=prior), SimpleNamespace(now_ns=10)
    with pytest.raises(OwnerProtocolRefusal):
        with boot_deferral_clock(bot, clock) as receipt:
            clock.now_ns = value
            bot._keepwatch_boot_retry_clock()
    assert clock.now_ns == value and bot._keepwatch_boot_retry_clock is prior
    assert receipt["restored"] is True


@pytest.mark.parametrize("kind", [RuntimeError, asyncio.CancelledError, KeyboardInterrupt])
def test_primary_failure_or_cancellation_preserved_and_restoration_recorded(kind):
    def prior():
        return 1
    bot = SimpleNamespace(_keepwatch_boot_retry_clock=prior)
    primary = kind("CONTROLLED")
    with pytest.raises(kind) as caught:
        with boot_deferral_clock(bot, SimpleNamespace(now_ns=10)) as receipt:
            raise primary
    assert caught.value is primary and bot._keepwatch_boot_retry_clock is prior
    assert receipt["restored"] is True and receipt["final_observed_ns"] == 10


def test_fresh_two_arm_instances_do_not_reuse_elapsed_origin():
    receipts = []
    for origin in (100, 100_000_000_000):
        bot, clock = SimpleNamespace(_keepwatch_boot_retry_clock=lambda: 3), SimpleNamespace(now_ns=origin)
        with boot_deferral_clock(bot, clock) as receipt:
            assert bot._keepwatch_boot_retry_clock() == 0
            clock.now_ns += 5_000_000_000
            assert bot._keepwatch_boot_retry_clock() == 5
        receipts.append(receipt)
    assert receipts[0]["origin_ns"] != receipts[1]["origin_ns"]
    assert all(row["restored"] for row in receipts)


def test_port_never_changes_another_service_instance():
    first = SimpleNamespace(_keepwatch_boot_retry_clock=lambda: 1)
    other = SimpleNamespace(_keepwatch_boot_retry_clock=lambda: 7)
    prior = other._keepwatch_boot_retry_clock
    with boot_deferral_clock(first, SimpleNamespace(now_ns=0)):
        assert other._keepwatch_boot_retry_clock is prior and other._keepwatch_boot_retry_clock() == 7


@pytest.mark.parametrize("fault", ["version", "component", "policy", "head", "filesha",
    "review", "scope", "extra", "missing"])
def test_literal_source_and_independent_review_contract_has_no_descendant_fallback(fault):
    value = review()
    if fault == "component":
        value["app_component"]["head"] = "f" * 40
    elif fault == "extra":
        value["permission"] = True
    elif fault == "missing":
        del value["scope"]
    else:
        key = {"version": "version", "policy": "clock_policy", "head": "reference_head",
            "filesha": "reference_file_sha256", "review": "review_sha256", "scope": "scope"}[fault]
        value[key] = "0" * 64 if fault == "review" else "UNREVIEWED"
    with pytest.raises(OwnerProtocolRefusal):
        require_boot_deferral_review(value)


def make_driver(**kwargs):
    return RecordedBootDriver(prepared=prepared(), mapping=mapping(), events=[event()],
        maximum_owner_actions=3, **kwargs)


def test_existing_driver_explicit_mode_copies_review_and_preserves_budgets():
    value = review()
    driver = make_driver(boot_deferral_review=value, maximum_publish_boundaries=2161,
        publish_control_ruling_sha256="b" * 64)
    value["review_sha256"] = "c" * 64
    assert driver.boot_deferral_review["review_sha256"] == "a" * 64
    assert driver.publish_control.observe_boot_retry is True
    assert len(driver.publish_control.boundaries) == 2161 and driver.maximum_owner_actions == 3


def test_clock_binding_requires_existing_bounded_publish_path():
    with pytest.raises(ValueError, match="publish_control_boundaries"):
        make_driver(boot_deferral_review=review())


def test_legacy_driver_does_not_silently_enable_new_clock_or_observer():
    driver = make_driver(maximum_publish_boundaries=2161, publish_control_ruling_sha256="b" * 64)
    assert driver.boot_deferral_review is None and driver.publish_control.observe_boot_retry is False


@pytest.mark.parametrize("value", [None, SimpleNamespace(boot_deferral_review=review())])
def test_new_4f_entry_requires_exact_existing_driver_not_duck_type(value):
    with pytest.raises(ValueError, match="literal_4f_requires"):
        require_4f_boot_driver(value)


def test_new_4f_entry_refuses_legacy_driver_instead_of_real_clock_fallback():
    with pytest.raises(ValueError, match="literal_4f_requires"):
        require_4f_boot_driver(make_driver())


def test_new_4f_entry_rechecks_review_after_construction():
    driver = make_driver(boot_deferral_review=review(), maximum_publish_boundaries=2161,
        publish_control_ruling_sha256="b" * 64)
    driver.boot_deferral_review["review_sha256"] = "0" * 64
    with pytest.raises(OwnerProtocolRefusal):
        require_4f_boot_driver(driver)


def test_new_4f_entry_acceptance_is_source_contract_not_permission():
    driver = make_driver(boot_deferral_review=review(), maximum_publish_boundaries=2161,
        publish_control_ruling_sha256="b" * 64)
    assert require_4f_boot_driver(driver) is None
    assert not driver.consumed and driver.prepared["permission_granted"] is False


class RetryService(ControlledService):
    def __init__(self):
        super().__init__()
        self.observer_calls = 0
        self.retry = {"deferred_reason": "selected_history_active", "deferred_symbol": "YMAT",
            "excluded": {"DKI": {"status": "UNKNOWN", "entry_allowed": False}}}

    def _keepwatch_boot_retry_state(self):
        self.observer_calls += 1
        return self.retry


def setup_control():
    rows = schedule()
    return PublishControl(rows, observe_boot_retry=True), next(
        row for row in rows if row["kind"] == "state_publish_control")


def test_actual_class_method_observation_is_read_only_and_delegate_runs_once():
    control, step = setup_control()
    bot = RetryService()
    before = deepcopy(bot.retry)
    control.invoke(bot, type(bot), step, clock_ns=step["clock_ns"], copy_guard=lambda: True)
    assert bot.calls == 1 and bot.observer_calls == 2 and bot.retry == before
    recorded = control.records[0]
    bot.retry["excluded"].clear()
    assert recorded["after"]["boot_retry"]["excluded"]["DKI"]["entry_allowed"] is False
    assert recorded["full_publisher"] == "UNEXERCISED"
    assert recorded["monotonic_warmup_timeout"] == "UNKNOWN"


def test_wrapped_retry_observer_refuses_before_hold_delegate():
    control, step = setup_control()
    bot = RetryService()
    bot._keepwatch_boot_retry_state = lambda: {}
    with pytest.raises(ValueError, match="unwrapped_boot_retry"):
        control.invoke(bot, type(bot), step, clock_ns=step["clock_ns"], copy_guard=lambda: True)
    assert bot.calls == 0 and control.failed


@pytest.mark.parametrize("which", ["before", "after"])
def test_existing_copy_guard_loss_still_stops_control(which):
    control, step = setup_control()
    bot, calls = RetryService(), []

    def guard():
        calls.append(True)
        return (len(calls) != 1) if which == "before" else (len(calls) == 1)

    with pytest.raises(ValueError, match="copy_fence_lost"):
        control.invoke(bot, type(bot), step, clock_ns=step["clock_ns"], copy_guard=guard)
    assert bot.calls == (0 if which == "before" else 1) and control.failed


def test_hold_primary_failure_not_replaced_by_observer_success():
    control, step = setup_control()
    bot = RetryService()
    primary = RuntimeError("CONTROLLED")
    bot.error = primary
    with pytest.raises(RuntimeError) as caught:
        control.invoke(bot, type(bot), step, clock_ns=step["clock_ns"], copy_guard=lambda: True)
    assert caught.value is primary and bot.calls == 1 and control.failed


@pytest.mark.parametrize("value", [1, None, "yes"])
def test_observer_mode_is_explicit_bool(value):
    with pytest.raises(ValueError, match="observation_mode"):
        PublishControl(schedule(), observe_boot_retry=value)


@pytest.mark.asyncio
async def test_nonowner_wait_for_keeps_real_timeout_and_same_elapsed_clock():
    def prior():
        return 5
    bot, clock = SimpleNamespace(_keepwatch_boot_retry_clock=prior), SimpleNamespace(now_ns=123)
    port = AsyncioPort(SimpleNamespace())
    with boot_deferral_clock(bot, clock):
        with pytest.raises(TimeoutError):
            await port.wait_for(asyncio.Event().wait(), 0)
        assert clock.now_ns == 123 and bot._keepwatch_boot_retry_clock() == 0
    assert bot._keepwatch_boot_retry_clock is prior
