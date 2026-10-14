"""Controlled call-site distinctions, not proof of the live flood's origin."""

from dataclasses import replace

import pytest

from project_mai_tai.momentum_paper.warning_window import CoverageEvidence
from tests.unit.test_momentum_warning import START, EPOCH, bar, consumer, trade


def active_consumer():
    model = consumer()
    for item in (bar(), bar(1, high="12")):
        model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    assert len(model.engine.active_events) == 2
    assert model.windows["TEST"].retained_buckets == 2
    return model


def window_state(model):
    return (dict(model.windows), dict(model._latest), dict(model.last_warning_ns),
            model._stream_epoch, model._sequence, model._drops, model.coverage)


def test_stale_duplicate_precedes_duplicate_filter_emits_once_and_preserves_windows():
    model = active_consumer()
    item = trade(2001, "12.1")
    sip_ns = item.sip_ts_ms * 1_000_000
    model.observe_trade(item, sip_timestamp_ns=sip_ns, observed_ns=sip_ns)
    before = window_state(model)
    assert not any(event.feed_gap for event in model.engine._active.values())
    assert model.observe_trade(item, sip_timestamp_ns=sip_ns, observed_ns=sip_ns + 1_000_000_000) == ()
    assert model.last_status == "raw_duplicate_or_out_of_order"
    records = model.observe_trade(item, sip_timestamp_ns=sip_ns, observed_ns=sip_ns + 1_000_000_001)
    assert model.last_status == "raw_future_or_stale"
    assert len(records) == 2 and all(record.event_type == "FEED_GAP" for record in records)
    assert all(record.payload == {"started_ms": item.sip_ts_ms,
                                  "ended_ms": item.sip_ts_ms + 1000} for record in records)
    assert model._last_raw_ns == {"TEST": sip_ns}
    for _ in range(5):
        assert model.observe_trade(item, sip_timestamp_ns=sip_ns, observed_ns=sip_ns + 1_000_000_001) == ()
    assert window_state(model) == before
    assert all(event.feed_gap for event in model.engine._active.values())


@pytest.mark.parametrize("age_ns,refused", [(1_000_000_000, False), (1_000_000_001, True)])
def test_raw_freshness_exact_one_second_and_one_ns_over(age_ns, refused):
    model = active_consumer()
    item = trade(2001, "12.1")
    sip_ns = item.sip_ts_ms * 1_000_000
    before = window_state(model)
    records = model.observe_trade(item, sip_timestamp_ns=sip_ns, observed_ns=sip_ns + age_ns)
    assert (model.last_status == "raw_future_or_stale") is refused
    assert bool([record for record in records if record.event_type == "FEED_GAP"]) is refused
    assert ("TEST" in model._last_raw_ns) is not refused
    assert all(event.feed_gap is refused for event in model.engine._active.values())
    assert window_state(model) == before


@pytest.mark.parametrize("future_ns", [1, 1_000_000])
def test_future_raw_print_is_refused_without_clearing_windows(future_ns):
    model = active_consumer()
    item = trade(2001, "12.1")
    sip_ns = item.sip_ts_ms * 1_000_000
    before = window_state(model)
    records = model.observe_trade(item, sip_timestamp_ns=sip_ns, observed_ns=sip_ns - future_ns)
    assert model.last_status == "raw_future_or_stale"
    assert model._last_raw_ns == {}
    assert len(records) == 2 and all(record.event_type == "FEED_GAP" for record in records)
    assert all(record.payload["started_ms"] == item.sip_ts_ms for record in records)
    assert all(record.payload["ended_ms"] == (sip_ns - future_ns) // 1_000_000 for record in records)
    assert window_state(model) == before


@pytest.mark.parametrize("kind", ["sequence", "drops", "epoch"])
def test_real_aggregate_discontinuity_clears_windows_and_records_fixed_start_gap(kind):
    model = active_consumer()
    previous = model.windows["TEST"]
    proof = CoverageEvidence(EPOCH, START - 60_000, START + 2000, 0, True)
    model.set_coverage(proof)
    model._raw_first_ms["TEST"] = START
    model._raw_coverage["TEST"] = proof
    item = bar(2)
    changes = {"sequence": {"sequence": 4}, "drops": {"dropped_total": 1},
               "epoch": {"gateway_instance_id": "replacement-gateway"}}
    item = replace(item, **changes[kind])
    _, records = model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    gaps = [record for record in records if record.event_type == "FEED_GAP"]
    assert len(gaps) == 2
    assert all(record.payload == {"started_ms": model.engine.coverage_started_ms,
                                  "ended_ms": START + 3000} for record in gaps)
    assert model.windows["TEST"] is not previous
    assert model.windows["TEST"].retained_buckets == 1
    assert model._raw_first_ms == model._raw_coverage == {}
    assert model.last_warning_ns == {} and model.coverage is None
    assert model.epoch == item.epoch and model._sequence == item.sequence
    next_item = replace(bar(3), sequence=item.sequence + 1,
                        gateway_instance_id=item.gateway_instance_id,
                        generation=item.generation, dropped_total=item.dropped_total)
    _, next_records = model.observe_aggregate(next_item, observed_ns=next_item.published_utc_ns, legacy_active=set())
    assert not any(record.event_type == "FEED_GAP" for record in next_records)
    assert model.windows["TEST"].retained_buckets == 2
