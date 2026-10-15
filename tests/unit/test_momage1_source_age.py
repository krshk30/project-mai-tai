"""Controlled source-age contract; not measured provider calibration or live outcomes."""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

from project_mai_tai.momentum_paper import warning_source_age as age
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.momentum_paper.warning_window import WarningWindow
from tests.unit.test_momentum_warning import START, bar


@pytest.fixture
def controlled_bound():
    assert age.MAX_AGGREGATE_SOURCE_END_AGE_MS == 7601


def delayed_bar(*, age_ms=2000, high="12", close="12", receive_age_ms=0):
    original = bar(low="10", high=high)
    observed = (original.end_ms + age_ms) * 1_000_000
    item = replace(original, close=Decimal(close),
                   received_utc_ns=observed - receive_age_ms * 1_000_000,
                   published_utc_ns=observed)
    return item, observed


def fresh_consumer():
    return MomentumWarningConsumer(prior_closes={"TEST": Decimal("10")},
                                   condition_version="CONTROLLED", coverage_started_ms=START)


def assert_delayed_warning_and_detection():
    item, observed = delayed_bar()
    model = fresh_consumer()
    outcome, records = model.observe_aggregate(item, observed_ns=observed, legacy_active=set())
    assert outcome.status == "ADMITTED", outcome
    assert {record.strategy_code for record in records} == {"momentum_30s_warn", "momentum_60s_warn"}
    assert all(record.event_type == "DETECTED" for record in records)
    assert all(record.payload["observed_decision_ns"] == observed for record in records)
    records = model.engine.seal_warning_decisions(records, observed_decision_ns=observed)
    assert len(records) == 2  # The actual paper handler seals before persisting.
    assert item.start_ms == START and item.end_ms == START + 1000
    return model, records


def test_a_two_second_source_age_fresh_receive_admits_and_detects(controlled_bound):
    model, _ = assert_delayed_warning_and_detection()
    assert model.coverage is None  # Initial admission does not borrow cooling coverage.


@pytest.mark.parametrize("age_ms,reason", [(7602, "aggregate_source_end_stale"),
                                         (-1, "aggregate_bucket_incomplete")])
def test_b_above_bound_or_incomplete_refused(controlled_bound, age_ms, reason):
    item, observed = delayed_bar(age_ms=age_ms)
    outcome, records = fresh_consumer().observe_aggregate(item, observed_ns=observed, legacy_active=set())
    assert (outcome.status, outcome.reason) == ("REFUSED", reason)
    assert not records


def test_c_receive_age_over_one_second_still_refused(controlled_bound):
    item, observed = delayed_bar(receive_age_ms=1001)
    outcome, records = fresh_consumer().observe_aggregate(item, observed_ns=observed, legacy_active=set())
    assert (outcome.status, outcome.reason) == ("REFUSED", "aggregate_clock_or_stale")
    assert not records


def test_d_mutation_original_one_second_bound_breaks_positive_control(controlled_bound, monkeypatch):
    assert_delayed_warning_and_detection()
    monkeypatch.setattr(age, "MAX_AGGREGATE_SOURCE_END_AGE_MS", 1000)
    with pytest.raises(AssertionError):
        assert_delayed_warning_and_detection()


def test_exact_source_boundary_inclusive_without_deleting_gate(controlled_bound):
    assert age.aggregate_source_age_reason(end_ms=START, observed_ms=START + 7601) is None
    assert age.aggregate_source_age_reason(end_ms=START, observed_ms=START + 7602) == "aggregate_source_end_stale"


def test_window_and_engine_independently_enforce_same_bound(controlled_bound):
    item, observed = delayed_bar()
    window = WarningWindow("TEST")
    window.observe(item, observed_ns=observed)
    assert window.evaluate(observed_ms=observed // 1_000_000).warning_windows
    assert not window.evaluate(observed_ms=item.end_ms + 7602).warning_windows
    engine = fresh_consumer().engine
    arguments = dict(strategy_code="momentum_30s_warn", symbol="TEST", reference_price=Decimal("10"),
                     reference_start_ms=item.start_ms, reference_end_ms=item.end_ms,
                     signal_price=item.close, signal_start_ms=item.start_ms, signal_end_ms=item.end_ms,
                     aggregate_received_utc_ns=item.received_utc_ns,
                     aggregate_published_utc_ns=item.published_utc_ns)
    late = (item.end_ms + 7602) * 1_000_000
    assert not engine.detect_from_bucket(**{**arguments, "aggregate_received_utc_ns": late,
                                            "aggregate_published_utc_ns": late}, observed_decision_ns=late)
    assert engine.detect_from_bucket(**arguments, observed_decision_ns=observed)


@pytest.mark.parametrize("receive_age_ns,accepted", [(1_000_000_000, True),
                                                   (1_000_000_001, False)])
def test_engine_direct_receive_gate_exact_boundary(controlled_bound, receive_age_ns, accepted):
    item, observed = delayed_bar()
    records = fresh_consumer().engine.detect_from_bucket(
        strategy_code="momentum_30s_warn", symbol="TEST", reference_price=Decimal("10"),
        reference_start_ms=item.start_ms, reference_end_ms=item.end_ms,
        signal_price=item.close, signal_start_ms=item.start_ms, signal_end_ms=item.end_ms,
        aggregate_received_utc_ns=observed - receive_age_ns,
        aggregate_published_utc_ns=observed, observed_decision_ns=observed,
    )
    assert bool(records) is accepted


@pytest.mark.parametrize("receive_age_ns,accepted", [(1_000_000_000, True),
                                                   (1_000_000_001, False)])
def test_warning_direct_receive_gate_exact_boundary(controlled_bound, receive_age_ns, accepted):
    item, observed = delayed_bar()
    item = replace(item, received_utc_ns=observed - receive_age_ns)
    outcome, records = fresh_consumer().observe_aggregate(item, observed_ns=observed, legacy_active=set())
    assert (outcome.status == "ADMITTED") is accepted
    assert bool(records) is accepted


@pytest.mark.parametrize("high,close,warning,detection", [
    ("10.9999", "10.9999", False, False), ("11", "11", True, False),
    ("12", "11.99", True, False), ("12", "12", True, True),
])
def test_warning_ten_percent_and_detection_twenty_percent_close_unchanged(
    controlled_bound, high, close, warning, detection,
):
    item, observed = delayed_bar(high=high, close=close)
    outcome, records = fresh_consumer().observe_aggregate(item, observed_ns=observed, legacy_active=set())
    assert (outcome.status == "ADMITTED") is warning
    assert bool(records) is detection


def test_shared_sixteen_cap_and_eligibility_unchanged(controlled_bound):
    item, observed = delayed_bar()
    outcome, records = fresh_consumer().observe_aggregate(
        item, observed_ns=observed, legacy_active={f"X{index}" for index in range(16)},
    )
    assert outcome.reason == "warning_cap_full" and not records
    model = fresh_consumer()
    model.engine.prior_closes.clear()
    outcome, records = model.observe_aggregate(item, observed_ns=observed, legacy_active=set())
    assert outcome.reason == "warning_symbol_or_time_filter" and not records


def test_final_seal_rechecks_transport_and_source_age(controlled_bound):
    item, observed = delayed_bar()
    model = fresh_consumer()
    _, records = model.observe_aggregate(item, observed_ns=observed, legacy_active=set())
    assert records
    assert not model.engine.seal_warning_decisions(records, observed_decision_ns=observed + 1_000_000_001)
    assert not model.engine.active_events


def test_no_queued_earlier_print_can_fill_delayed_bucket_decision(controlled_bound):
    from tests.unit.test_momentum_warning import trade

    model, _ = assert_delayed_warning_and_detection()
    _, observed = delayed_bar()
    before_decision = trade(observed // 1_000_000 - START - 1, "12.01")
    assert not any(row.event_type == "FILLED" for row in model.observe_trade(
        before_decision, sip_timestamp_ns=before_decision.sip_ts_ms * 1_000_000,
        observed_ns=observed,
    ))
    assert all(event["fill"] is None for event in model.engine.active_events)


def test_e_retained_aehl_and_whole_replay_counts_are_conditional_not_real_entries():
    root = Path("/Users/velkris/.codex/task-queues/analysis/momwarn1-wed-no-warnings-20261014")
    if not (root / "MOMAGE1-candidate-7601-replay.json").exists():
        pytest.skip("retained offline replay receipt unavailable; no fabricated tape")
    candidate = json.loads((root / "MOMAGE1-candidate-7601-replay.json").read_text())
    base = json.loads((root / "MOMAGE1-base-replay.json").read_text())
    assert candidate["source_age_bound_ms"] == 7601 and base["source_age_bound_ms"] == 1000
    assert candidate["input_sha256"] == base["input_sha256"]
    assert candidate["aehl_561"]["counts"]["bars"] == 561
    assert candidate["whole_20000"]["counts"]["bars"] == 20000
    assert base["aehl_561"]["counts"]["bars_with_price_warning"] == 0
    assert base["whole_20000"]["counts"]["bars_with_price_warning"] == 0
    assert candidate["aehl_561"]["counts"]["bars_with_price_warning"] == 92
    assert candidate["whole_20000"]["counts"]["bars_with_price_warning"] == 707
    assert len(candidate["aehl_561"]["conditional_known_prior_detections"]) == 2
    assert candidate["actual_exported_prior_closes"] == {"AEHL": "8.55"}
    assert candidate["actual_paper_entries"].startswith("UNKNOWN_")
