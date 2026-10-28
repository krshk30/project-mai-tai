"""C11720 per-CONFIRM accounting. CONTROLLED samples, not actual fleet boot."""

from copy import deepcopy

import pytest

from tools.l12_recorded_boot_timeline import (
    FLEET_FILES, FLEET_NAMES, ConfirmationSeedTracker, RecordedSessionMap,
    _aware_ns, decode_friday_fleet)


ANCHOR = 1793001600000
CONFIRM = ANCHOR + 3 * 3600000 + 1851000


def fleet():
    mapping = RecordedSessionMap(target_day="2026-10-26", ruling_sha256="a" * 64,
        chronology_review_sha256="b" * 64)
    confirms = [dict(symbol="YMAT", event_id="retained-confirm-1", original_ms=1792755431000,
        mapped_ms=CONFIRM, mapped_ns=CONFIRM * 1000000, raw_ref="CONTROLLED_SCANNER_ROW")]
    return dict(version="FRIDAY18_PER_CONFIRM_V1", source_sha256=dict(FLEET_FILES),
        names=list(FLEET_NAMES), mapping=mapping.receipt(), confirmations=confirms,
        first_confirm_by_name={name: confirms[0] if name == "YMAT" else None for name in FLEET_NAMES},
        missing_confirm_names=[name for name in FLEET_NAMES if name != "YMAT"])


def tracker(**kwargs):
    return ConfirmationSeedTracker(fleet(), **dict(dict(limit_bars=None,
        limit_metric=None, limit_review_sha256=None), **kwargs))


def sample(now, *, seeded=True):
    return dict(status="OBSERVED_SEED_SAMPLE_NOT_FULL_TIMELINE_QUALIFICATION", observed_ms=now,
        symbols=[dict(symbol="YMAT", seed_complete=seeded, history_anchor_ms=ANCHOR,
            history_completed_ns=now * 1000000)])


def test_observed_latency_and_both_bar_measures_without_default_PASS_limit():
    value = tracker()
    value.observe(sample(CONFIRM + 9000))
    report = value.report()
    measured = report["per_confirmation"][0]
    assert report["seed_denominator"] == 18 and report["observed_first_confirm_seeds"] == 1
    assert measured["elapsed_model_ms"] == 9000 and measured["elapsed_model_ns"] == 9000000000
    assert measured["closed_minute_boundaries"] == 1 and measured["elapsed_minute_ceiling"] == 1
    assert report["limit_bars"] is None and report["status"] == "BAR_LIMIT_UNBOUND_NOT_PASS"
    assert report["permission_granted"] is False


@pytest.mark.parametrize("metric", ["CLOSED_MINUTE_BOUNDARIES", "ELAPSED_MINUTE_CEILING"])
def test_exact_reviewed_limit_is_separate_from_measurements_and_native_permission(metric):
    value = tracker(limit_bars=1, limit_metric=metric, limit_review_sha256="c" * 64)
    value.observe(sample(CONFIRM + 121000))
    row = next(row for row in value.report()["per_name"] if row["symbol"] == "YMAT")
    assert row["within_reviewed_bar_limit"] is False
    assert value.report()["permission_granted"] is False


@pytest.mark.parametrize("metric,within", [("CLOSED_MINUTE_BOUNDARIES", True), ("ELAPSED_MINUTE_CEILING", False)])
def test_bar_count_convention_cannot_be_chosen_after_observing_the_result(metric, within):
    value = tracker(limit_bars=0, limit_metric=metric, limit_review_sha256="c" * 64)
    value.observe(sample(CONFIRM + 1))
    row = next(row for row in value.report()["per_name"] if row["symbol"] == "YMAT")
    assert row["within_reviewed_bar_limit"] is within


@pytest.mark.parametrize("fault", ["bool", "negative", "metric", "digest", "unbound_partial"])
def test_no_implicit_or_unreviewed_bar_limit(fault):
    args = dict(limit_bars=1, limit_metric="CLOSED_MINUTE_BOUNDARIES", limit_review_sha256="c" * 64)
    if fault == "bool":
        args["limit_bars"] = True
    elif fault == "negative":
        args["limit_bars"] = -1
    elif fault == "metric":
        args["limit_metric"] = "CHOOSE_FAVORABLE_AFTER_RESULT"
    elif fault == "digest":
        args["limit_review_sha256"] = None
    else:
        args["limit_bars"] = None
    with pytest.raises(ValueError):
        tracker(**args)


def test_seed_before_confirm_is_not_zero_latency_success():
    value = tracker()
    value.observe(sample(CONFIRM - 1))
    assert value.report()["observed_first_confirm_seeds"] == 0
    value.observe(sample(CONFIRM))
    assert value.report()["per_confirmation"][0]["elapsed_model_ns"] == 0


def test_missing_confirmation_keeps_FADE_only_name_UNKNOWN_in_18_denominator():
    report = tracker().report()
    aard = next(row for row in report["per_name"] if row["symbol"] == "AARD")
    assert aard["status"] == "CONFIRM_CLOCK_UNKNOWN" and aard["within_reviewed_bar_limit"] is None
    assert report["seed_denominator"] == 18


def test_first_observed_seed_is_not_replaced_by_faster_later_measurement():
    value = tracker()
    value.observe(sample(CONFIRM + 61000))
    first = deepcopy(value.report()["per_confirmation"])
    value.observe(sample(CONFIRM + 62000))
    assert value.report()["per_confirmation"] == first


def test_reconfirm_episodes_have_separate_record_id_measurements():
    inputs = fleet()
    second = dict(inputs["confirmations"][0], event_id="retained-confirm-2",
        mapped_ms=CONFIRM + 60000, mapped_ns=(CONFIRM + 60000) * 1000000)
    inputs["confirmations"].append(second)
    value = ConfirmationSeedTracker(inputs, limit_bars=None, limit_metric=None, limit_review_sha256=None)
    value.observe(sample(CONFIRM + 9000))
    value.observe(sample(CONFIRM + 61000))
    rows = value.report()["per_confirmation"]
    assert [row["elapsed_model_ms"] for row in rows] == [9000, 1000]


@pytest.mark.parametrize("fault", ["sample_type", "bool_clock", "anchor", "ns_disagrees", "rewind"])
def test_adverse_observer_sample_cannot_become_current_seed(fault):
    value, row = tracker(), sample(CONFIRM)
    if fault == "sample_type":
        row["status"] = "SYNTHETIC_READY"
    elif fault == "bool_clock":
        row["observed_ms"] = True
    elif fault == "anchor":
        row["symbols"][0]["history_anchor_ms"] -= 86400000
    elif fault == "ns_disagrees":
        row["observed_clock_ns"] = (CONFIRM + 1) * 1000000
    else:
        value.observe(sample(CONFIRM + 1))
    with pytest.raises(ValueError):
        value.observe(row)


def test_csv_microseconds_preserved_for_market_event_clock():
    assert _aware_ns("2026-10-23 11:18:51.470992+00") == 1792754331470992000


def test_missing_or_hash_changed_fleet_never_becomes_empty_population():
    mapping = RecordedSessionMap(target_day="2026-10-26", ruling_sha256="a" * 64,
        chronology_review_sha256="b" * 64)
    with pytest.raises(ValueError, match="three_Friday"):
        decode_friday_fleet({}, mapping)
    with pytest.raises(ValueError, match="SHA"):
        decode_friday_fleet({name: b"" for name in FLEET_FILES}, mapping)


@pytest.mark.asyncio
async def test_new_scanner_delta_tie_uses_membership_before_owner_wake():
    from tools.l12_boot_owner_clock import scheduled

    calls = []

    class Owner:
        async def advance_before(self, value):
            calls.append("advance")

        async def wake_due(self):
            calls.append("wake")

        async def settle(self):
            calls.append("settle")

    steps = [dict(kind="scanner_delta", clock_ns=1), dict(kind="history_poll", clock_ns=1)]
    async for row in scheduled(steps, None, Owner()):
        calls.append(row["kind"])
    assert calls == ["advance", "scanner_delta", "settle", "advance", "wake", "history_poll", "settle"]
