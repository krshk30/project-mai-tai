"""Constructor-only CONTROLLED ports; no service start, native input or PG."""

from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests import line_restore_acceptance_factory as factory
from tests.unit.test_l12_c11810_carried_windows import tracker
from tools.l12_recorded_boot_driver import RecordedBootDriver
from tools.l12_recorded_boot_timeline import (
    C11810_CARRIED, OLD_LOG_SHA, SOURCE_ANCHOR_MS, c11810_candidate_sha256,
)


@pytest.mark.parametrize("subscribe,expected_calls", [(True, 1), (False, 0)])
def test_explicit_no_initial_subscription_does_not_call_add_and_legacy_default_does(monkeypatch, subscribe, expected_calls):
    added = []
    monkeypatch.setattr(factory.LineRestoreCase, "clock", lambda self: nullcontext())
    monkeypatch.setattr(factory.LineRestoreCase, "add", lambda self: added.append(self.symbol))
    monkeypatch.setattr(factory, "SchwabV2BotService", lambda settings: SimpleNamespace(strategy=SimpleNamespace()))
    value = factory.make_line_restore_case(symbol="YMAT", now_ms=1793001600000, subscribe_initial=subscribe)
    assert len(added) == expected_calls
    assert value.symbol == "YMAT"


@pytest.mark.parametrize("subscribe", [None, 0, "false"])
def test_initial_subscription_requires_actual_bool_before_construction(subscribe):
    with pytest.raises(ValueError, match="initial_subscription_bool"):
        factory.make_line_restore_case(symbol="YMAT", now_ms=1793001600000, subscribe_initial=subscribe)


def driver_inputs():
    observed = tracker()
    # CONTROLLED constructor marker only. No actual log/candidate assertion and
    # never passed to a service/timeline/PG; Main's actual source review is separate.
    observed.candidate.update(raw_log_sha256=OLD_LOG_SHA,
        record_origin="ACTUAL_FIXED_SHA_AND_LINE_COUNT_DELIVERY")
    review = dict(version="C11810_CONTROLLED_CARRIED_BOOT_MEMBERSHIP_V1",
        members=sorted(C11810_CARRIED), candidate_sha256=c11810_candidate_sha256(observed.candidate),
        ruling_sha256="a" * 64, review_sha256="b" * 64,
        scope="RECONSTRUCTED_BOOT_MEMBERSHIP_NOT_ORIGINAL_CONFIRM_OR_READINESS")
    kwargs = dict(prepared=dict(mapping=observed.mapping.receipt(), permission_granted=False,
        population=dict(names=list(observed.fleet["names"]))), mapping=observed.mapping,
        events=[dict(kind="history_poll", clock_ns=SOURCE_ANCHOR_MS * 1000000,
            watchlist=None, raw_ref="CONTROLLED_NOT_AN_ACTUAL_POLL", raw_sha256="c" * 64)],
        maximum_owner_actions=1, confirmation_tracker=observed, initial_carried_review=review)
    return kwargs, review


def test_exact_carried_review_is_copied_not_mutable_input_authority():
    kwargs, review = driver_inputs()
    original = deepcopy(review)
    driver = RecordedBootDriver(**kwargs)
    review["members"].clear()
    assert driver.initial_carried_review == original


@pytest.mark.parametrize("fault", ["missing_member", "extra_member", "wrong_candidate", "zero_review", "scope"])
def test_unknown_carried_population_or_review_never_sets_membership(fault):
    kwargs, review = driver_inputs()
    if fault == "missing_member":
        review["members"].pop()
    elif fault == "extra_member":
        review["members"].append("YMAT")
    elif fault == "wrong_candidate":
        review["candidate_sha256"] = "d" * 64
    elif fault == "zero_review":
        review["review_sha256"] = "0" * 64
    else:
        review["scope"] = "ORIGINAL_CONFIRMED_READY"
    with pytest.raises(ValueError):
        RecordedBootDriver(**kwargs)
