"""P6 — tests for the LIVE_LOCKED drift audit.

⛔ The audit itself cannot run in CI (no env file), so what CI CAN pin is that its comparison logic
is right — above all that it compares by MEANING, not by string. A `True` vs `"true"` false positive
on every boolean would make the report noise, and a noisy detector gets ignored, which is the same
outcome as not having one.
"""

from __future__ import annotations

from scripts.audit_live_locked_drift import audit, coerce_matches, parse_env_file


def test_parses_only_mai_tai_assignments():
    text = "\n".join(
        [
            "# a comment",
            "",
            "MAI_TAI_FOO=true",
            "PATH=/usr/bin",
            "MAI_TAI_BAR=2",
            "   MAI_TAI_BAZ=hello   ",
        ]
    )
    assert parse_env_file(text) == {
        "MAI_TAI_FOO": "true",
        "MAI_TAI_BAR": "2",
        "MAI_TAI_BAZ": "hello",
    }


def test_commented_out_assignment_is_not_an_override():
    """⛔ A commented override is NOT set. Reading it as set would hide real drift."""
    assert parse_env_file("#MAI_TAI_FOO=true") == {}


def test_later_assignment_wins_matching_systemd():
    assert parse_env_file("MAI_TAI_FOO=a\nMAI_TAI_FOO=b") == {"MAI_TAI_FOO": "b"}


def test_quotes_are_stripped():
    assert parse_env_file('MAI_TAI_FOO="true"') == {"MAI_TAI_FOO": "true"}


# ---------------------------------------------------------------------------
# ⛔⭐ Compare by MEANING. These are the false positives that would kill the report.
# ---------------------------------------------------------------------------


def test_bool_true_matches_the_string_true():
    assert coerce_matches(True, "true") is True
    assert coerce_matches(True, "TRUE") is True
    assert coerce_matches(False, "false") is True


def test_bool_mismatch_is_detected():
    assert coerce_matches(False, "true") is False
    assert coerce_matches(True, "false") is False


def test_numbers_compare_numerically_not_textually():
    assert coerce_matches(2, "2") is True
    assert coerce_matches(0.5, "0.50") is True
    assert coerce_matches(180.0, "180") is True
    assert coerce_matches(2, "3") is False


def test_a_bool_never_matches_an_arbitrary_string():
    """⛔ `True` vs `"1"` must NOT match — an env that says 1 is not what pydantic parsed here."""
    assert coerce_matches(True, "1") is False
    assert coerce_matches(True, "yes") is False


def test_non_numeric_text_against_a_number_is_drift_not_a_crash():
    assert coerce_matches(5, "abc") is False


# ---------------------------------------------------------------------------
# audit() partitioning
# ---------------------------------------------------------------------------


def test_audit_partitions_into_drift_agree_and_unset():
    mirror = {"a_flag": True, "b_flag": False, "c_value": 5}
    env = {"MAI_TAI_A_FLAG": "true", "MAI_TAI_B_FLAG": "true"}
    drifted, agreed, unset = audit(mirror, env)
    assert [d[0] for d in agreed] == ["a_flag"]
    assert [d[0] for d in drifted] == ["b_flag"]
    assert [d[0] for d in unset] == ["c_value"]


def test_drift_row_carries_both_values_so_the_report_can_be_read():
    drifted, _, _ = audit({"b_flag": False}, {"MAI_TAI_B_FLAG": "true"})
    assert drifted == [("b_flag", False, "true")]


def test_unset_is_reported_not_silently_treated_as_agreement():
    """⛔ A setting with no env override is the mirror's ONLY live path — the loudest case, not the
    quietest. Folding it into 'agree' would hide exactly the values nothing else can check."""
    _, agreed, unset = audit({"c_value": 5}, {})
    assert agreed == []
    assert unset == [("c_value", 5)]


def test_the_three_known_08_19_drifters_are_detected_from_a_realistic_env():
    """⛔ A known-bad tape from the real 08-19 reading — the audit must go red on it."""
    mirror = {
        "strategy_schwab_1m_v2_cw_v2_reclaim_enabled": False,
        "strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled": False,
        "oms_v2_eh_entry_enabled": False,
        "strategy_schwab_1m_v2_atr_flip_quantity": 2,
    }
    env = {
        "MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_V2_RECLAIM_ENABLED": "true",
        "MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_V2_EH_RESTING_ENTRY_ENABLED": "true",
        "MAI_TAI_OMS_V2_EH_ENTRY_ENABLED": "true",
        "MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_FLIP_QUANTITY": "2",
    }
    drifted, agreed, unset = audit(mirror, env)
    assert len(drifted) == 3
    assert [a[0] for a in agreed] == ["strategy_schwab_1m_v2_atr_flip_quantity"]
    assert unset == []
