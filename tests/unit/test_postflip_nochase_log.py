"""Pure formatting controls; no strategy, broker or historical replay proof."""

import pytest

from project_mai_tai.strategy_core.postflip_nochase_log import PostFlipNoChaseLog


def operands(**changes):
    value = dict(allowed=False, refusal_reason="retry_segment_mismatch",
                 atr_state="long", short_segment=0, retry_segment=1000,
                 flip_bar_ms=61000)
    return value | changes


def test_exact_message_once_per_observed_flip_not_per_broker_or_poll():
    observer = PostFlipNoChaseLog("POM")
    assert observer.line(**operands()) == (
        "[V2-POSTFLIP-NOCHASE] sym=POM flip_bar=61000 "
        "reason=already_flipped_not_chasing next=waiting_for_sell_flip")
    assert observer.line(**operands()) is None
    assert observer.line(**operands(flip_bar_ms=121000)) is not None
    assert observer.line(**operands()) is None


@pytest.mark.parametrize("changes", [
    {"allowed": True}, {"allowed": None}, {"allowed": 0},
    {"refusal_reason": "position_evidence_missing_or_stale"},
    {"refusal_reason": "country_owner_admission_unproven"},
    {"atr_state": "short"}, {"short_segment": 1000},
    {"short_segment": False}, {"retry_segment": 0},
    {"retry_segment": True}, {"flip_bar_ms": None},
    {"flip_bar_ms": True}, {"flip_bar_ms": 1000},
])
def test_unknown_or_other_refusal_is_not_relabelled(changes):
    observer = PostFlipNoChaseLog("POM")
    assert observer.line(**operands(**changes)) is None
    assert observer.last_logged_flip_bar_ms == 0


def test_observer_does_not_mutate_supplied_admission_operands():
    value = operands()
    original = dict(value)
    PostFlipNoChaseLog("POM").line(**value)
    assert value == original


def test_symbol_instances_are_separate_and_log_injection_refused():
    assert PostFlipNoChaseLog("POM").line(**operands()) is not None
    assert PostFlipNoChaseLog("SPRC").line(**operands()) is not None
    assert PostFlipNoChaseLog("POM\nERROR").line(**operands()) is None
