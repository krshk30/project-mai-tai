"""The substring trap, encoded as tests.

⛔ The load-bearing test is `test_the_three_HARD_STOP_populations_are_distinguished`. Substring
matching on that one token gave a wrong answer twice in 24 hours — a 75% inflated headline on
2026-08-04, and a 385-vs-1 misread on 2026-08-05 that nearly routed unbuilt work into an attended
real-money deploy. Every case below is a real production string.
"""
from __future__ import annotations

from project_mai_tai.trade_reasons import (
    parse_reason,
    reason_emitter,
    reason_rule,
    reason_rule_in,
)

# Verified against production, 2026-08-05 (14-day window, counts in the module docstring).
MANAGED_EXIT_STOP = "oms_v2_managed_exit:CW_HARD_STOP"
SOFTWARE_STOP = "HARD_STOP"
NATIVE_GUARD = "HARD_STOP_NATIVE_BACKUP"
FREE_TEXT = "schwab_1m_v2 ATR Flip CW-v2-resting"


def test_the_three_HARD_STOP_populations_are_distinguished() -> None:
    """⛔ THE test. All three contain 'HARD_STOP'; all three are different code paths."""
    assert reason_rule(MANAGED_EXIT_STOP) == "CW_HARD_STOP"
    assert reason_rule(SOFTWARE_STOP) == "HARD_STOP"
    assert reason_rule(NATIVE_GUARD) == "HARD_STOP_NATIVE_BACKUP"

    # The 2026-08-05 near-miss: "is this on the native-guard path?"
    on_guard = {"HARD_STOP_NATIVE_BACKUP"}
    assert reason_rule_in(NATIVE_GUARD, on_guard) is True
    assert reason_rule_in(MANAGED_EXIT_STOP, on_guard) is False, "the 385-vs-1 misread"
    assert reason_rule_in(SOFTWARE_STOP, on_guard) is False

    # ⛔ THE TRAP DIRECTION — a SHORT needle in a LONG rule. This is the 2026-08-04 inflation:
    # asking for HARD_STOP and sweeping in HARD_STOP_NATIVE_BACKUP (a different code path with
    # the opposite profile: ~1 reject per episode, not ~41). Exact membership must say False.
    # ⚠️ Assertions using a needle LONGER than the rule pass under substring matching too, so
    # they prove nothing — the mutation caught that omission here.
    software_only = {"HARD_STOP"}
    assert reason_rule_in(SOFTWARE_STOP, software_only) is True
    assert reason_rule_in(NATIVE_GUARD, software_only) is False, "the 2026-08-04 75% inflation"
    assert reason_rule_in(MANAGED_EXIT_STOP, software_only) is False

    # ...and what a substring match would have said instead: all three, every time.
    assert all("HARD_STOP" in r.upper() for r in (MANAGED_EXIT_STOP, SOFTWARE_STOP, NATIVE_GUARD))


def test_emitter_rule_split() -> None:
    assert parse_reason(MANAGED_EXIT_STOP) == ("oms_v2_managed_exit", "CW_HARD_STOP")
    assert reason_emitter(MANAGED_EXIT_STOP) == "oms_v2_managed_exit"


def test_a_bare_rule_has_no_emitter_and_is_NOT_mangled() -> None:
    """A parser assuming the colon is always present would return ('HARD_STOP_NATIVE_BACKUP', '')
    and every rule comparison would silently miss."""
    assert parse_reason(NATIVE_GUARD) == ("", "HARD_STOP_NATIVE_BACKUP")
    assert parse_reason("FLOOR_BREACH") == ("", "FLOOR_BREACH")
    assert parse_reason("ENTRY_P3_SURGE") == ("", "ENTRY_P3_SURGE")


def test_free_text_reasons_survive_verbatim() -> None:
    """723 rows carry prose with spaces and no colon."""
    assert parse_reason(FREE_TEXT) == ("", FREE_TEXT)
    assert parse_reason("schwab_1m_v2 resting-entry cancel") == (
        "", "schwab_1m_v2 resting-entry cancel",
    )


def test_a_rule_containing_a_colon_survives() -> None:
    assert parse_reason("emitter:LEVEL:2") == ("emitter", "LEVEL:2")


def test_malformed_and_empty_are_safe() -> None:
    for bad in (None, "", "   ", ":", ":FOO", "FOO:"):
        emitter, rule = parse_reason(bad)
        assert emitter == ""
        assert reason_rule_in(bad, {"ANYTHING"}) is False
    # An empty reason must never match an empty-string rule set entry.
    assert reason_rule_in("", {""}) is False


def test_membership_is_case_insensitive_on_the_rule() -> None:
    assert reason_rule_in("oms_v2_managed_exit:cw_hard_stop", {"CW_HARD_STOP"}) is True
    assert reason_rule_in(MANAGED_EXIT_STOP, {"cw_hard_stop"}) is True


def test_scale_levels_are_distinct_rules() -> None:
    """SCALE_PCT2 / SCALE_PCT4_AFTER2 / SCALE_FAST4 all share 'SCALE'."""
    assert reason_rule_in("SCALE_PCT2", {"SCALE_PCT2"}) is True
    assert reason_rule_in("SCALE_PCT4_AFTER2", {"SCALE_PCT2"}) is False
    assert reason_rule_in("SCALE_FAST4", {"SCALE_PCT4_AFTER2"}) is False
