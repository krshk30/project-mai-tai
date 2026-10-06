"""Dated operator policy preserves raw paper UNKNOWN2; never changes checker."""
import copy
from datetime import timedelta

import pytest
import closeout
import release_policy as policy
from test_attended_release import NOW, catalog_control, fleet


def inactive_paper():
    return dict(fleet()["momentum-paper"], MainPID=0, ActiveState="inactive", SubState="dead", Result="success",
                ExecMainCode=1, ExecMainStatus=0, InactiveEnterTimestamp="Tue 2026-10-06 13:40:01 UTC")


def paper_output():
    catalog, raw = catalog_control()
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        for name, service in closeout.PAPER_FLAGS:
            if line.startswith("PASS flag=" + name + " service=" + service + " "):
                lines[i] = "UNKNOWN flag=" + name + " service=" + service + " reason=momentum-paper is not active"
    return "\n".join(lines).replace("PASS; checked=151/151 mismatches=0 unknown=0",
                                   "UNKNOWN; checked=149/151 mismatches=0 unknown=2") + "\n"


def admit(raw=None, before=None, after=None, now=NOW, rc=2):
    before = inactive_paper() if before is None else before
    after = copy.deepcopy(before) if after is None else after
    return closeout.flag_result(rc, paper_output() if raw is None else raw, catalog_control()[0],
                                paper_before=before, paper_after=after, now=now)


def test_exact_named_paper149_151_unknown2_keeps_original_verdict_and_rc():
    value = admit()
    assert value["coverage"] == "expected-dated-inactive-paper"
    assert (value["checked"], value["total"], value["unknown"]) == (149, 151, 2)
    assert value["original_rc"] == 2 and value["original_verdict"] == "UNKNOWN"


@pytest.mark.parametrize("field,value", [("MainPID", 20), ("ActiveState", "failed"), ("SubState", "failed"),
    ("Result", "exit-code"), ("ExecMainCode", 2), ("ExecMainStatus", 1), ("NRestarts", 1),
    ("InactiveEnterTimestamp", "Mon 2026-10-05 13:40:01 UTC"),
    ("InactiveEnterTimestamp", "Tue 2026-10-06 13:41:01 UTC"),
    ("InactiveEnterTimestamp", "Tue 2026-10-06 13:40:31 UTC"), ("InactiveEnterTimestamp", "")])
def test_any_unexpected_paper_state_or_stop_time_blocks(field, value):
    before = inactive_paper()
    before[field] = value
    with pytest.raises(policy.Stop):
        admit(before=before)


def test_paper_changed_during_checker_or_wrong_window_blocks():
    after = inactive_paper()
    after["InvocationID"] = "f" * 32
    with pytest.raises(policy.Stop):
        admit(after=after)
    for now in (NOW - timedelta(days=1), NOW - timedelta(hours=2)):
        with pytest.raises(policy.Stop):
            admit(now=now)


def test_no_midnight_clock_abort_after_first_stop_with_unchanged_dated_paper():
    assert admit(now=NOW + timedelta(hours=9))["original_verdict"] == "UNKNOWN"


@pytest.mark.parametrize("change", [lambda s: s.replace("reason=momentum-paper is not active", "reason=read failure"),
    lambda s: s.replace("momentum_paper_enabled service=momentum-paper", "momentum_paper_enabled service=oms"),
    lambda s: s.replace("149/151", "149/149"), lambda s: s.replace("unknown=2", "unknown=1"),
    lambda s: s.replace("mismatches=0", "mismatches=1"), lambda s: s.replace("UNKNOWN flag=", "PASS flag="),
    lambda s: s.replace("UNKNOWN; checked=149", "PASS; checked=149"),
    lambda s: s + s.splitlines()[0] + "\n", lambda s: s.replace("PASS flag=", "UNKNOWN flag=", 1)])
def test_any_other_unknown_reason_failure_duplicate_or_population_blocks(change):
    with pytest.raises(policy.Stop):
        admit(raw=change(paper_output()))


def test_no_raw_unknown2_admission_without_fresh_state_proof():
    with pytest.raises(policy.Stop):
        closeout.flag_result(2, paper_output(), catalog_control()[0])
    with pytest.raises(policy.Stop):
        admit(rc=0)
