"""Real service seed predicate, controlled state; NOT actual restored-row proof."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from tools.l12_service_boot_replay import observe_combined_seed_state


NOW = int(datetime.fromisoformat("2026-10-26T07:00:00-04:00").timestamp() * 1000)
ANCHOR = int(datetime.fromisoformat("2026-10-26T04:00:00-04:00").timestamp() * 1000)


def controlled():
    bot = object.__new__(SchwabV2BotService)
    settings = SimpleNamespace(strategy_schwab_1m_v2_keepwatch1_enabled=True,
        strategy_schwab_1m_v2_brokerdecides1_enabled=True, protected_symbol_set=set(),
        atr_simulation_aggregate_receipt_path="CONTROLLED_NOT_FILE_VERIFIED")
    bot.settings = settings
    bot.strategy = SimpleNamespace(_now_ms=lambda: NOW, _cw_armed_segment_safety_enabled=True)
    bot._keepwatch_enabled = True
    bot._watchlist = {"AAA", "BBB"}
    bot._keepwatch = SimpleNamespace(retained=lambda anchor: {"CCC"})
    bot._keepwatch_proven = {name: SimpleNamespace(anchor_ms=ANCHOR, through_ms=NOW - 60000,
        started_ns=(NOW - 1000) * 1000000, completed_ns=NOW * 1000000,
        massive_sha256="a" * 64, schwab_sha256="b" * 64) for name in ("AAA", "BBB", "CCC")}
    bot._rest_warmup_done = {"AAA", "BBB", "CCC"}
    bot._boot_warmup_timeout_released = set()
    bot._boot_exclusion_sources_readable = True
    bot._boot_scanner_selected = {"AAA", "BBB"}
    bot._boot_state_restoration_complete = True
    bot._db_seeded = set()  # Deliberately empty: not the ON predicate.
    def forbidden():
        raise AssertionError("ON observation must never query a database or provider")
    bot.session_factory = forbidden
    return SimpleNamespace(bot=bot, settings=settings, now_ms=NOW)


def test_actual_ON_predicate_counts_retained_names_without_DB_or_readiness_assignment():
    case = controlled()
    before = dict(case.bot._keepwatch_proven)
    result = observe_combined_seed_state(case)
    assert result["watched"] == ["AAA", "BBB", "CCC"]
    assert result["seed_complete"] == 3 and result["seed_denominator"] == 3
    assert result["seed_N_of_N"] is True and result["blockers"] == []
    assert result["permission_granted"] is False
    assert result["actual_0400_to_0700_and_first_flip"] == "UNRUN"
    assert result["l14_receipt_file_verified"] is False
    assert case.bot._keepwatch_proven == before and case.bot._db_seeded == set()


@pytest.mark.parametrize("fault,blocker", [
    ("missing", "KEEPWATCH_FULL_SESSION_HISTORY_SEED_INCOMPLETE"),
    ("wrong_anchor", "KEEPWATCH_FULL_SESSION_HISTORY_SEED_INCOMPLETE"),
    ("empty", "EMPTY_WATCHED_POPULATION_NOT_0_OF_0_PASS"),
    ("exclusions", "INELIGIBLE_EXCLUSION_UNREADABLE"),
    ("scanner", "EMPTY_TRADEABLE_SCANNER_POPULATION"),
    ("warmup", "REST_WARMUP_INCOMPLETE"),
    ("latch", "FLEET_BOOT_RESTORATION_LATCH_CLOSED"),
    ("safety", "ARMED_SEGMENT_SAFETY_OFF_NOT_ALL_ON")])
def test_incomplete_observations_name_component_without_changing_the_hold(fault, blocker):
    case = controlled()
    if fault == "missing":
        case.bot._keepwatch_proven.pop("CCC")
        case.bot._db_seeded.add("CCC")  # Cannot launder a missing provider receipt.
    elif fault == "wrong_anchor":
        case.bot._keepwatch_proven["CCC"].anchor_ms -= 86400000
    elif fault == "empty":
        case.bot._watchlist.clear()
        case.bot._keepwatch.retained = lambda anchor: set()
    elif fault == "exclusions":
        case.bot._boot_exclusion_sources_readable = False
    elif fault == "scanner":
        case.bot._boot_scanner_selected.clear()
    elif fault == "warmup":
        case.bot._rest_warmup_done.remove("CCC")
    elif fault == "latch":
        case.bot._boot_state_restoration_complete = False
    else:
        case.bot.strategy._cw_armed_segment_safety_enabled = False
    before = case.bot._boot_state_restoration_complete
    result = observe_combined_seed_state(case)
    assert blocker in result["blockers"]
    assert case.bot._boot_state_restoration_complete is before
    assert result["permission_granted"] is False
    if fault in {"missing", "wrong_anchor"}:
        assert result["seed_complete"] == 2 and result["seed_denominator"] == 3
    if fault == "empty":
        assert result["seed_N_of_N"] is False


@pytest.mark.parametrize("fault", ["keepwatch_flag", "l12_flag", "internal_keepwatch", "foreign_type", "clock"])
def test_observer_refuses_OFF_profile_foreign_service_or_clock_rewrite(fault):
    case = controlled()
    if fault == "keepwatch_flag":
        case.settings.strategy_schwab_1m_v2_keepwatch1_enabled = False
    elif fault == "l12_flag":
        case.settings.strategy_schwab_1m_v2_brokerdecides1_enabled = False
    elif fault == "internal_keepwatch":
        case.bot._keepwatch_enabled = False
    elif fault == "foreign_type":
        case.bot = SimpleNamespace()
    else:
        case.now_ms += 1
    with pytest.raises(ValueError):
        observe_combined_seed_state(case)
