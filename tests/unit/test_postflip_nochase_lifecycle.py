"""19 portable controls replacing deferred severity tests; no historical claims."""

import pytest

from project_mai_tai.strategy_core.postflip_nochase_log import PostFlipNoChaseLog
from tools.nochaselog1_supplied_state_replay import compare_supplied_state


def refusal(**changes):
    return dict(anchor_ms=1000, allowed=False, refusal_reason="retry_segment_mismatch",
                atr_state="long", short_segment=0, retry_segment=1000) | changes


def observed():
    observer = PostFlipNoChaseLog("POM")
    observer.observe_transition(flip="BUY", bar_ms=61000, anchor_ms=1000)
    return observer


@pytest.mark.parametrize("boundary", ["SELL", "indicator_reset", "different_session"])
def test_only_current_observed_buy_identity_can_label_nochase(boundary):
    observer = observed()
    args = refusal()
    if boundary == "SELL":
        observer.observe_transition(flip="SELL", bar_ms=121000, anchor_ms=1000)
    elif boundary == "indicator_reset":
        observer.reset_indicator()
    else:
        args["anchor_ms"] = 86401000
    assert observer.observed_line(**args) is None
    assert observer.last_logged_flip_bar_ms == 0


@pytest.mark.parametrize("bar,anchor", [
    (True, 1000), (None, 1000), (0, 1000),
    (61000, True), (61000, 0), (61000, 121000),
])
def test_malformed_transition_cannot_preserve_prior_affirmative_identity(bar, anchor):
    observer = observed()
    observer.observe_transition(flip="BUY", bar_ms=bar, anchor_ms=anchor)
    assert observer.current == (0, 0)
    assert observer.observed_line(**refusal()) is None
    assert observer.last_logged_flip_bar_ms == 0


def test_reconstruction_reset_does_not_repeat_same_or_older_flip():
    observer = observed()
    assert observer.observed_line(**refusal()) is not None
    observer.reset_indicator()
    observer.observe_transition(flip="BUY", bar_ms=61000, anchor_ms=1000)
    assert observer.observed_line(**refusal()) is None
    observer.observe_transition(flip="BUY", bar_ms=31000, anchor_ms=1000)
    assert observer.observed_line(**refusal()) is None
    assert observer.last_logged_flip_bar_ms == 61000


def test_next_observed_transition_identity_can_emit_once_after_sell():
    observer = observed()
    assert observer.observed_line(**refusal()) is not None
    observer.observe_transition(flip="SELL", bar_ms=121000, anchor_ms=1000)
    assert observer.observed_line(**refusal()) is None
    observer.observe_transition(flip="BUY", bar_ms=181000, anchor_ms=1000)
    assert "flip_bar=181000" in observer.observed_line(**refusal(retry_segment=121000))
    assert observer.observed_line(**refusal(retry_segment=121000)) is None


def test_same_symbol_new_lifetime_and_other_symbol_do_not_share_log_latches():
    first = observed()
    assert first.observed_line(**refusal()) is not None
    replacement = observed()
    other = PostFlipNoChaseLog("SPRC")
    other.observe_transition(flip="BUY", bar_ms=61000, anchor_ms=1000)
    assert replacement.observed_line(**refusal()) is not None
    assert other.observed_line(**refusal()).startswith("[V2-POSTFLIP-NOCHASE] sym=SPRC ")
    assert first.observed_line(**refusal()) is None


@pytest.mark.parametrize("retry", [False, None, 0, -1])
def test_observed_buy_does_not_invent_missing_retry_identity(retry):
    observer = observed()
    assert observer.observed_line(**refusal(retry_segment=retry)) is None
    assert observer.current == (1000, 61000)
    assert observer.last_logged_flip_bar_ms == 0


@pytest.mark.parametrize("events", [
    [],
    [{"origin": "HISTORICAL_PASS", "source_ref": "unproved"}],
    [{"origin": "CONTROLLED_STATE", "source_ref": ""}],
])
def test_supplied_state_comparator_refuses_empty_or_mislabelled_proof(events):
    class Old:
        pass

    class New:
        pass

    calls = []

    def forbidden(*args):
        calls.append(args)
        raise AssertionError("unbound input must refuse before invoking a world")

    with pytest.raises(ValueError):
        compare_supplied_state(events, old=Old(), proposed=New(),
                               apply_event=forbidden, capture=forbidden)
    assert calls == []
