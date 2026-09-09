"""P6 — tests for the LIVE_LOCKED drift audit.

⛔ The audit itself cannot run in CI (no env file), so what CI CAN pin is that its comparison logic
is right — above all that it compares by MEANING, not by string. A `True` vs `"true"` false positive
on every boolean would make the report noise, and a noisy detector gets ignored, which is the same
outcome as not having one.
"""

from __future__ import annotations

import sys

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


# ---------------------------------------------------------------------------
# ⛔⭐⭐ THE SECOND AXIS (2026-09-09) — an `unset` key runs the CODE DEFAULT, not the mirror.
#
# THE CONTROL THAT MATTERS: remove a divergent key from the env and the audit must FAIL.
# Under the old logic every one of these went green — that WAS the defect.
# ---------------------------------------------------------------------------

from scripts.audit_live_locked_drift import UNKNOWN, code_defaults, split_unset  # noqa: E402


def test_unset_key_whose_default_diverges_is_RED_not_benign():
    """⛔ THE CONTROL. `oms_v2_cw_target_pct` dropped from the env: the mirror says 5.0, but with
    no override production runs settings.py's 2.0. The old code called this benign and exited 0."""
    unset = [("oms_v2_cw_target_pct", 5.0)]
    benign, divergent, unknown = split_unset(unset, {"oms_v2_cw_target_pct": 2.0})
    assert benign == []
    assert unknown == []
    assert divergent == [("oms_v2_cw_target_pct", 5.0, 2.0)]


def test_unset_key_whose_default_agrees_stays_green():
    """PINS THE OTHER DIRECTION. A detector that reds on every unset key is noise, and a noisy
    detector is a disabled one — this is the benign case the original docstring described."""
    benign, divergent, unknown = split_unset([("a_flag", True)], {"a_flag": True})
    assert divergent == [] and unknown == []
    assert benign == [("a_flag", True)]


def test_a_mirrored_key_that_is_not_a_settings_field_is_RED():
    """A mirror entry that cannot reach production is drift by another route."""
    benign, divergent, unknown = split_unset([("ghost_key", 1)], {"ghost_key": UNKNOWN})
    assert benign == [] and divergent == []
    assert unknown == [("ghost_key", 1)]


def test_bool_true_is_not_benign_against_an_integer_default():
    """⛔ `True == 1` in Python. Without a type guard a mirror of True would read benign against a
    default of 1 — the discriminating case silently removed."""
    _, divergent, _ = split_unset([("a_flag", True)], {"a_flag": 1})
    assert [d[0] for d in divergent] == ["a_flag"]


def test_code_defaults_are_read_from_the_class_not_a_populated_instance(monkeypatch):
    """⛔⭐⭐ THE SELF-COMPARISON TRAP. The cron wrapper sources the service env before running
    this. If defaults came from `Settings()` they would be filled FROM that env, so the audit would
    compare the env against itself and could never come out false. Set the env to a non-default
    value and the reported default must be UNMOVED."""
    monkeypatch.setenv("MAI_TAI_OMS_V2_CW_TARGET_PCT", "5.0")
    assert code_defaults(["oms_v2_cw_target_pct"])["oms_v2_cw_target_pct"] == 2.0


def test_the_live_mirror_has_divergent_defaults_so_this_axis_is_EXERCISED():
    """⛔ NAME THE DENOMINATOR. If every mirrored key happened to agree with its default this whole
    axis would be UNEXERCISED and the tests above would prove nothing about production. Measured
    2026-09-09 on 7eca22a7: 13 of 26. Pinned as non-zero, not as an exact count, so an ordinary
    settings change does not turn this red for the wrong reason."""
    from project_mai_tai.backtest.replay import LIVE_LOCKED

    defaults = code_defaults(list(LIVE_LOCKED))
    _, divergent, unknown = split_unset(sorted(LIVE_LOCKED.items()), defaults)
    assert unknown == [], f"mirror names non-Settings keys: {[u[0] for u in unknown]}"
    assert len(divergent) > 0, "no mirrored key diverges from its default — axis is UNEXERCISED"
    names = {d[0] for d in divergent}
    assert {"oms_v2_cw_target_pct", "oms_v2_cw_hard_stop_pct"} <= names


# ---------------------------------------------------------------------------
# ⛔⭐⭐ END-TO-END: THE EXIT CODE IS THE GATE.
# A classification that nothing acts on is not a watchdog. cron only sees the exit code, so the
# controls above are proven at the level cron actually reads.
# ---------------------------------------------------------------------------


def _env_text_for(keys_to_write) -> str:
    from project_mai_tai.backtest.replay import LIVE_LOCKED

    lines = []
    for key in keys_to_write:
        value = LIVE_LOCKED[key]
        text = "true" if value is True else "false" if value is False else str(value)
        lines.append(f"MAI_TAI_{key.upper()}={text}")
    return "\n".join(lines) + "\n"


def _run_audit(tmp_path, env_text):
    from scripts.audit_live_locked_drift import main

    env_file = tmp_path / "project-mai-tai.env"
    env_file.write_text(env_text, encoding="utf-8")
    argv = sys.argv
    sys.argv = ["audit_live_locked_drift.py", "--env-file", str(env_file)]
    try:
        return main()
    finally:
        sys.argv = argv


def test_a_fully_mirrored_env_exits_ZERO(tmp_path, capsys):
    """The green control. Without this, a script that always exits 1 would 'pass' every red test
    below while being useless."""
    from project_mai_tai.backtest.replay import LIVE_LOCKED

    rc = _run_audit(tmp_path, _env_text_for(sorted(LIVE_LOCKED)))
    assert rc == 0, capsys.readouterr().out


def test_REMOVING_A_DIVERGENT_KEY_FROM_THE_ENV_MAKES_THE_AUDIT_FAIL(tmp_path, capsys):
    """⛔⭐⭐ THE CONTROL THAT MATTERS (operator-directed 2026-09-09).

    Drop `oms_v2_cw_target_pct` — the operator's +5% target — from the env exactly as a rebuild
    would, and the audit MUST exit non-zero. Before today it exited 0 and printed
    'the mirrored value IS the live path', which was the opposite of the truth: with the line gone
    production runs settings.py's 2.0.
    """
    from project_mai_tai.backtest.replay import LIVE_LOCKED

    keys = [k for k in sorted(LIVE_LOCKED) if k != "oms_v2_cw_target_pct"]
    rc = _run_audit(tmp_path, _env_text_for(keys))
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "UNSET AND DIVERGENT" in out
    assert "oms_v2_cw_target_pct" in out


def test_removing_a_BENIGN_key_does_not_cry_wolf(tmp_path, capsys):
    """PINS THE OTHER DIRECTION at the gate. Dropping a key whose default already equals the
    mirror is genuinely harmless and must stay green, or the alarm gets muted within a week."""
    from project_mai_tai.backtest.replay import LIVE_LOCKED

    defaults = code_defaults(list(LIVE_LOCKED))
    benign_keys = [k for k, v in LIVE_LOCKED.items() if defaults.get(k) == v]
    assert benign_keys, "no benign key exists — this control cannot be run"
    keys = [k for k in sorted(LIVE_LOCKED) if k != benign_keys[0]]
    rc = _run_audit(tmp_path, _env_text_for(keys))
    assert rc == 0, capsys.readouterr().out


def test_an_unreadable_env_refuses_with_2_rather_than_reporting_clean(tmp_path):
    """UNKNOWN is not PASS. A missing env file must not read as 'no overrides'."""
    from scripts.audit_live_locked_drift import main

    argv = sys.argv
    sys.argv = ["audit_live_locked_drift.py", "--env-file", str(tmp_path / "nope.env")]
    try:
        assert main() == 2
    finally:
        sys.argv = argv
