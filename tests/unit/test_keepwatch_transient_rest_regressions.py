"""Run existing causal controls with F1 ON; no provider or permission replacement.

The retained print operands remain real, their provider bars explicitly controlled.
These are C7A/KWSTALE1 compatibility checks, not the original 12:38 NCPL race.
"""
import hashlib
import json
from pathlib import Path

import pytest

from tests.unit import test_keepwatch1_quiet_gap_resume as gap
from tests.unit import test_keepwatch_stale_overflow_counter as overflow
from project_mai_tai.settings import Settings


def enable_f1(bot):
    bot.settings = bot.settings.model_copy(update={
        "strategy_schwab_1m_v2_keepwatch_transient_rest_hold_enabled": True})
    bot.strategy.settings = bot.settings
    assert bot.settings.strategy_schwab_1m_v2_keepwatch_transient_rest_hold_enabled is True


def enable_seeded(monkeypatch):
    original = gap.seeded
    async def supplied(*args, **kwargs):
        result = await original(*args, **kwargs)
        enable_f1(result[0])
        return result
    monkeypatch.setattr(gap, "seeded", supplied)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["one", "second"])
async def test_f1_on_preserves_real_KWSTALE1_owner_rejection_lifetimes(monkeypatch, caplog, case):
    original = overflow.published
    async def supplied(*args, **kwargs):
        result = await original(*args, **kwargs)
        enable_f1(result[0])
        return result
    monkeypatch.setattr(overflow, "published", supplied)
    if case == "one":
        await overflow.test_one_rejection_is_held_once_across_six_later_owner_tasks(caplog)
    else:
        await overflow.test_a_later_second_rejection_still_holds_and_cancels_once(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,minute,filename,digest,count", gap.ORIGINAL_PRINT_CASES)
async def test_f1_on_keeps_original_C7A_print_operands_and_recovery(monkeypatch, symbol, minute, filename, digest, count):
    enable_seeded(monkeypatch)
    await gap.test_retained_oddlot_prints_recover_via_real_history_and_owned_bar_paths(
        monkeypatch, symbol, minute, filename, digest, count)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,event", [(False, "unsized_quote"),
    (True, "same_closed_positive"), (True, "forming_positive")])
async def test_f1_on_keeps_C7A_OFF_and_positive_generation_fences(monkeypatch, caplog, enabled, event):
    enable_seeded(monkeypatch)
    await gap.test_OFF_legacy_read_and_ON_closed_positive_generation_race(monkeypatch, caplog, enabled, event)


def test_f1_audit_addition_keeps_exact_preexisting_flag_denominators():
    root = Path(__file__).parents[2]
    raw = (root / "ops/health/expected_flags.json").read_bytes()
    name = "strategy_schwab_1m_v2_keepwatch_transient_rest_hold_enabled"
    lines = raw.splitlines(keepends=True)
    added = [line for line in lines if ('"name": "' + name + '"').encode() in line]
    assert len(added) == 1
    # Exact installed04cd artifact, including all preexisting values/metadata/
    # whitespace. Only the new audit row may be removed to recover these bytes.
    baseline_raw = b"".join(line for line in lines if line not in added)
    assert hashlib.sha256(baseline_raw).hexdigest() == "bfc76f3ee72bf1d70b41a464ef59e7294a5ede89ab651598cfae5f2bd41a4063"
    current, baseline = json.loads(raw), json.loads(baseline_raw)
    own, = [row for row in current["flags"] if row["name"] == name]
    assert own["expected"] is True and own["owning_service"] == "schwab-1m-v2"
    assert not own.get("also_check_services")
    assert Settings.model_fields[name].default is False
    assert len({row["name"] for row in current["flags"]}) == len(current["flags"])
    numeric = json.loads((root / "ops/health/expected_numeric.json").read_text())["settings"]
    projected = lambda rows: sum(1 + len(row.get("also_check_services", [])) for row in rows + numeric)
    assert projected(baseline["flags"]) == 181
    assert projected(current["flags"]) == 182
