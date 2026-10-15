"""Published-output denominator controls; not reconstructed warning generation."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "retained_lead_summary", Path(__file__).parents[2] / "scripts/momwarn1_retained_lead_summary.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_positive_only_denominator_is_separate_from_population_and_unknown_method():
    signals = "2026-10-12,momentum_30s,TEST,t1\n2026-10-12,momentum_30s,TEST,t2\n2026-10-12,momentum_30s,TEST,t3\n"
    leads = [["momentum_30s", "2026-10-12", "TEST", value] for value in (8, -1, 18)]
    result = module.summarize(signals, leads)
    assert result["ledgers"]["momentum_30s"] == dict(population=3, positive=2, nonpositive=1,
                                                  positive_only_median_s=13, all_population_median_s=8)
    assert result["repeated_strategy_day_symbol_rows"] == 2
    assert result["original_generation_method"] == "UNKNOWN_NOT_RECOVERED"
    assert result["actual_network_warning_or_ACK"] == "UNMEASURED"


@pytest.mark.parametrize("leads", [[], [["momentum_60s", "2026-10-12", "TEST", 8]],
                                  [["momentum_30s", "2026-10-12", "TEST", float("nan")]]])
def test_population_key_mismatch_or_nan_refuses_without_repairing_inputs(leads):
    with pytest.raises(ValueError):
        module.summarize("2026-10-12,momentum_30s,TEST,t1\n", leads)


def test_reordered_published_outputs_do_not_claim_per_event_alignment():
    signals = "2026-10-12,momentum_30s,AAA,t1\n2026-10-12,momentum_60s,BBB,t2\n"
    leads = [["momentum_60s", "2026-10-12", "BBB", 18], ["momentum_30s", "2026-10-12", "AAA", 8]]
    result = module.summarize(signals, leads)
    assert result["source_key_multiplicities_match"]
    assert not result["ordered_source_keys_match"] and result["ordered_source_key_mismatches"] == 2
    assert result["original_generation_method"] == "UNKNOWN_NOT_RECOVERED"
