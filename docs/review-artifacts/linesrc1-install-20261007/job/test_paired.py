"""[codex] Controlled future paired actions against actual captured baseline bytes."""
from copy import deepcopy
from datetime import timedelta
import json
import subprocess
from unittest.mock import Mock

import pytest
import archived_readonly as archived
import oms_health_readonly as health
import release_policy as p
import runner
from test_runner import HERE, NOW, FX, env, fleet, new_oms, phase_fleet
from test_runner import new_v2, held_text


def test_three_literal_actions_no_stopped_oms_flat_phase():
    assert p.PHASES == (("stop", p.V2), ("restart", "oms"), ("start", p.V2))
    fx = FX()
    runner.Sequence(fx).run()
    assert [call for call in fx.calls if ":" in call] == ["stop:"+p.V2, "restart:oms", "start:"+p.V2]
    assert fx.calls.index("gates2") < fx.calls.index("start:"+p.V2)
    assert fx.calls.count("v2_gate") == 1


@pytest.mark.parametrize("failure", ["restart:oms", "checkpoint2", "gates2"])
def test_restart_or_fresh_proof_failure_never_starts_v2_or_recovers(failure):
    fx = FX(failure)
    with pytest.raises(p.Stop): runner.Sequence(fx).run()
    assert "start:"+p.V2 not in fx.calls
    assert fx.calls.count("restart:oms") <= 1 and fx.calls[-1] == "abort"


@pytest.mark.parametrize("phase", range(4))
def test_exact_phase_identity_and_all_eleven_untouched(phase):
    state = phase_fleet(phase)
    p.states(fleet(), state, phase)
    for role in set(p.SERVICES) - {p.V2, "oms"}:
        damaged = deepcopy(state)
        damaged[role]["InvocationID"] = "0"*32
        with pytest.raises(p.Stop): p.states(fleet(), damaged, phase)


@pytest.mark.parametrize("field,value", [("MainPID", 0), ("NRestarts", 1), ("ActiveState", "inactive"),
    ("Result", "signal"), ("ExecMainStatus", 1), ("InvocationID", fleet()["oms"]["InvocationID"]),
    ("ExecMainStartTimestampMonotonic", 100), ("EnvironmentFiles", "FOREIGN")])
def test_new_oms_must_be_one_clean_new_owner(field, value):
    state = phase_fleet(2)
    state["oms"][field] = value
    with pytest.raises(p.Stop): p.states(fleet(), state, 2)


def test_oms_pin_once_phase2_and_phase3_rejects_external_restart(tmp_path):
    fx = runner.Real(tmp_path, {}, tmp_path)
    fx.before = fleet()
    state = phase_fleet(2)
    fx.fleet = lambda: state
    fx.checkpoint(2)
    original = deepcopy(fx.started_owners["oms"])
    state = phase_fleet(3)
    state["oms"]["MainPID"] += 1
    with pytest.raises(p.Stop, match="OMS moved"): fx.checkpoint(3)
    assert fx.started_owners["oms"] == original
    assert not (tmp_path/"phase-3.json").exists()


def test_phase2_requires_strict_flat_and_new_oms_health_without_general_inactive_v2_gate(tmp_path):
    fx = runner.Real(tmp_path, {}, tmp_path)
    fx.before = fleet()
    fx.started_owners = {"oms": new_oms()}
    fx.fleet = lambda: phase_fleet(2)
    fx.proc = Mock(); fx.source = Mock(); fx.census = Mock(); fx.redis = Mock(); fx.flat = Mock()
    fx.reader = Mock(return_value=b'{"rc":0,"service":"oms"}')
    fx.gates(2)
    fx.flat.assert_called_once_with()
    fx.proc.assert_called_once_with(new_oms(), "true", "oms")
    assert fx.reader.call_args.args[:2] == ("oms_health_readonly.py", "--start")
    fx.reader.side_effect = p.Stop("stale fresh-book proof")
    with pytest.raises(p.Stop): fx.gates(2)


def test_systemctl_restart_returned_success_but_oms_down_is_stop(tmp_path):
    fx = runner.Real(tmp_path, {}, tmp_path)
    fx.before = fleet()
    state = phase_fleet(1)
    state["oms"].update(MainPID=0, ActiveState="inactive", SubState="dead")
    fx.fleet = lambda: state
    fx.command = Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
    with pytest.raises(p.Stop): fx.action("restart", "oms")
    assert fx.command.call_count == 1
    assert fx.command.call_args.args[0] == ["systemctl", "restart", "project-mai-tai-oms.service"]


@pytest.mark.parametrize("role,oldhandoff", [("oms", "true"), (p.V2, "false")])
def test_old_loader_and_new_both_on_handoff_off_are_distinct(role, oldhandoff):
    old = env().replace((p.HANDOFF_FLAG+"=false").encode(), (p.HANDOFF_FLAG+"="+oldhandoff).encode())
    raw = old.replace(b"\n", b"\0")
    assert p.process_values(raw, "false", oldhandoff)[p.HANDOFF_FLAG] == oldhandoff
    new = p.env_candidate(env()).replace(b"\n", b"\0")
    proof = p.process_values(new, "true")
    assert proof[p.FLAG] == proof[p.RETAINED_FLAG] == "true" and proof[p.HANDOFF_FLAG] == "false"
    with pytest.raises(p.Stop): p.process_values(new.replace((p.HANDOFF_FLAG+"=false").encode(),
        (p.HANDOFF_FLAG+"=true").encode()), "true")


@pytest.mark.parametrize("damage", [None, "old", "stale", "future", "duplicate", "degraded", "errors"])
def test_new_oms_health_positive_and_exact_fences(damage):
    row = dict(service_name="oms", status="healthy", raw_status="healthy", effective_status="healthy",
               observed_at_raw=NOW.isoformat())
    overview = dict(errors=[], services=[row])
    start = NOW - timedelta(seconds=1)
    if damage == "old": row["observed_at_raw"] = (start-timedelta(seconds=1)).isoformat()
    elif damage == "stale": start=NOW-timedelta(seconds=200); row["observed_at_raw"]=(NOW-timedelta(seconds=121)).isoformat()
    elif damage == "future": row["observed_at_raw"]=(NOW+timedelta(seconds=1)).isoformat()
    elif damage == "duplicate": overview["services"].append(deepcopy(row))
    elif damage == "degraded": row["raw_status"]="degraded"
    elif damage == "errors": overview["errors"]=["unknown"]
    if damage:
        with pytest.raises(p.Stop): health.prove(overview, start, NOW)
    else: assert health.prove(overview, start, NOW)["rc"] == 0


def test_archive_actual_bounded_receipt_and_typed_unknown(monkeypatch, capsys):
    recorded = json.loads((HERE/"archived-baseline.json").read_bytes())
    assert recorded["snapshot_type"] == archived.TYPE and recorded["count"] == 1
    assert recorded["rows"][0]["id"] == "136db58b-4394-5999-a255-8434c21434ae"
    monkeypatch.setattr(archived, "collect", Mock(side_effect=RuntimeError("secret not printed")))
    assert archived.main() == 2
    raw = capsys.readouterr()
    assert json.loads(raw.out)["verdict"] == "UNKNOWN" and "secret" not in raw.out and not raw.err


def test_strict_flat_helper_byte_identical_to_parent_transferred_plan():
    baseline = subprocess.run(["git", "show", "25bf89139045fd0a120a01872ad572ef4702acd0:"
        "docs/review-artifacts/linesrc1-install-20261007/job/strict_flat_readonly.py"],
        capture_output=True, check=True).stdout
    assert (HERE/"strict_flat_readonly.py").read_bytes() == baseline


@pytest.mark.parametrize("damage", [None, "missing_warmup", "failed_warmup", "error", "unsafe", "later_hold"])
def test_long_closeout_safe_release_requires_exact_official_proof_not_forever_held(damage):
    text = held_text() + "2026-10-07 23:36:10,000 INFO [V2-BOOT-HOLD] released - restoration_complete=1 reconstructed_uncapped=0\n"
    raw = "Overall: PASS\nFinal call: PASS; full official proof\n| REST warmup | seeded fallback=369s | PASS |\n| BOOT-HOLD released | exact | PASS |\n"
    if damage == "error": text += "2026-10-07 23:36:11,000 ERROR real defect\n"
    elif damage == "unsafe": text=text.replace("reconstructed_uncapped=0", "reconstructed_uncapped=1")
    elif damage == "later_hold": text += "2026-10-07 23:36:11,000 WARNING [V2-BOOT-HOLD] HELD restoration_complete=0\n"
    elif damage == "missing_warmup": raw=raw.replace("| REST warmup | seeded fallback=369s | PASS |\n", "")
    elif damage == "failed_warmup": raw=raw.replace("| REST warmup | seeded fallback=369s | PASS |", "| REST warmup | unproven | FAIL |")
    def check():
        proof=p.held_logs(text, new_v2(), NOW+timedelta(seconds=500), NOW, allow_official_release=True)
        return p.report_disposition(0, raw, proof)
    if damage:
        with pytest.raises(p.Stop): check()
    else: assert check() == raw


def test_after_clean_stop_long_readonly_gate_wait_does_not_add_clock_abort():
    state=new_v2()
    stopped=NOW-timedelta(seconds=500)
    assert p.held_logs(held_text(),state,NOW+timedelta(seconds=2),stopped)["entry_allowed"] is False
