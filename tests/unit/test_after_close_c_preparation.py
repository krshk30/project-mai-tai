"""Local mechanical controls only; no production receipts or service actions."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops/systemd/prepare_after_close_c.py"
spec = importlib.util.spec_from_file_location("after_close_c", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
SHA = "a" * 40
NOW = datetime(2026, 10, 9, 20, 10, tzinfo=timezone.utc)


def state(pid):
    return dict(MainPID=str(pid), NRestarts="0", ActiveState="active", SubState="running",
                ExecMainStartTimestamp="Fri 2026-10-09 20:05:00 UTC")


def repin_case(orb=True, line=True):
    before = {name: state(i + 1) for i, name in enumerate((*m.LABELS, "market-data"))}
    after = deepcopy(before)
    for i, name in enumerate(m.sequence(orb, line)):
        after[name] = state(i + 100)
    text = 'EXPECTED_SHA=' + "b" * 40 + '\nSNAPSHOT=/home/trader/old.json\nINSTALL_RECORD=/home/trader/old-record.json\n'
    for name, label in m.LABELS.items():
        text += f'EXPECTED_{label}PID={before[name]["MainPID"]}\n'
        text += f'EXPECTED_{label}START=\'Fri 2026-10-09 19:00:00 UTC\'\n'
    text += 'EXPECTED_DATE="$(TZ=America/New_York date +%F)"\n'
    text += 'REPORT="v2-restart-evidence-${EXPECTED_DATE//-/}.md"\n'
    text += '/home/trader/preopen-daily/daily.py paper\n'
    text += 'python report \\\n  --install-record "$INSTALL_RECORD" \\\n'
    text += '  --restarted oms \\\n  --restarted strategy \\\n  --restarted schwab-1m-v2 \\\n'
    text += f"  --expect-flag 'oms:{m.RPG_FLAG}=false' \\\n"
    text += f"  --expect-flag 'schwab-1m-v2:{m.RPG_FLAG}=false' \\\n"
    text += f"  --expect-flag 'schwab-1m-v2:{m.L_FLAG}=false' \\\n"
    text += "  --expect-flag 'oms:UNCHANGED=true' \\\n  --no-schema-change\n"
    record = dict(snapshot_captured_at_utc="2026-10-09T20:00:00+00:00",
                  completed_at_utc=NOW.isoformat(), source_journal="/home/trader/c/journal.json",
                  snapshot="/home/trader/c/before.json", install_record="/home/trader/c/install.json",
                  service_actions={name: "restarted" if name in m.sequence(orb, line)
                                   else "deliberately_untouched" for name in before})
    return text, before, after, record


@pytest.mark.parametrize("orb,line,expected", [
    (False, False, ["control"]), (True, False, ["orb-schwab", "control"]),
    (False, True, ["schwab-1m-v2", "control"]),
    (True, True, ["schwab-1m-v2", "orb-schwab", "control"]),
])
def test_literal_order_one_restart_per_service(orb, line, expected):
    assert m.sequence(orb, line) == expected
    assert not {"oms", "strategy", "market-data"} & set(expected)


@pytest.mark.parametrize("value", [None, "a" * 8, "a" * 39, "a" * 41, "A" * 40, "z" * 40])
def test_full_final_sha_required(value):
    with pytest.raises(m.Refusal, match="full final SHA"):
        m.full_sha(value)


def test_only_two_authorized_env_changes_preserve_raw_unrelated_bytes():
    raw = (f"# untouched\nSECRET='opaque value'\n{m.RPG_FLAG}=false\n{m.L_FLAG}=false\nOTHER=true\n").encode()
    expected = raw.replace((m.L_FLAG + "=false").encode(), (m.L_FLAG + "=true").encode())
    expected += (m.O_FLAG + "=true\n").encode()
    assert m.env_candidate(raw, orb=True, line=True) == expected
    assert m.env_candidate(raw, orb=False, line=False) == raw


@pytest.mark.parametrize("raw", [f"{m.RPG_FLAG}=true\n", "OTHER=false\n",
                               f"{m.RPG_FLAG}=false\nOTHER=1\nOTHER=2\n"])
def test_ambiguous_env_or_rpg_refused(raw):
    with pytest.raises(m.Refusal):
        m.env_candidate(raw.encode(), orb=True, line=True)


def test_all_other_process_flags_untouched_including_oms():
    before = {"oms": {m.RPG_FLAG: "false", "MAI_TAI_OTHER": "true"},
              "orb-schwab": {}, "schwab-1m-v2": {m.RPG_FLAG: "false", m.L_FLAG: "false"}}
    after = deepcopy(before)
    after["orb-schwab"][m.O_FLAG] = "true"
    after["schwab-1m-v2"][m.L_FLAG] = "true"
    m.validate_process_flags(before, after, orb=True, line=True)
    after["oms"]["MAI_TAI_OTHER"] = "false"
    with pytest.raises(m.Refusal, match="unapproved"):
        m.validate_process_flags(before, after, orb=True, line=True)


@pytest.mark.parametrize("stamp", ["2026-10-09T19:59:59+00:00", "2026-10-10T20:10:00+00:00"])
def test_after_close_date_bound(stamp):
    with pytest.raises(m.Refusal):
        m.after_close(datetime.fromisoformat(stamp), "2026-10-09")
    m.after_close(NOW, "2026-10-09")


def flat():
    return dict(rc=0, errors=[], blockers=[], unknown=[], started_at=NOW.isoformat(),
                completed_at=(NOW + timedelta(seconds=10)).isoformat(),
                sql=dict(complete=True, observed_at=NOW.isoformat(), managed_rows=[], virtual_rows=[],
                         working_orders=[], inflight_intents=[]),
                brokers={account: dict(complete=True, identity_bound=True, started_at=NOW.isoformat(),
                                       working_orders=[], holdings=[])
                         for account in ("live:orb", "live:schwab_1m_v2")})


def test_fresh_direct_gate_not_snapshot_only():
    m.flat_receipt(flat(), 0, NOW, NOW + timedelta(seconds=10))


@pytest.mark.parametrize("label", ["managed_rows", "virtual_rows", "working_orders", "inflight_intents"])
def test_actual_nonempty_sql_refuses(label):
    receipt = flat()
    receipt["sql"][label] = [{"symbol": "TEST"}]
    with pytest.raises(m.Refusal):
        m.flat_receipt(receipt, 0, NOW, NOW + timedelta(seconds=10))


@pytest.mark.parametrize("change", ["missing_broker", "unbound", "working", "stale", "unknown", "rc"])
def test_broker_unknown_stale_orders_and_failed_gate_refuse(change):
    receipt = flat()
    broker = receipt["brokers"]["live:orb"]
    if change == "missing_broker":
        del receipt["brokers"]["live:orb"]
    elif change == "unbound":
        broker["identity_bound"] = False
    elif change == "working":
        broker["working_orders"] = [{"id": "actual-control"}]
    elif change == "stale":
        broker["started_at"] = (NOW - timedelta(seconds=121)).isoformat()
    elif change == "unknown":
        receipt["unknown"] = ["unmeasured"]
    else:
        receipt["rc"] = 1
    with pytest.raises((m.Refusal, KeyError)):
        m.flat_receipt(receipt, 0, NOW, NOW + timedelta(seconds=10))


@pytest.mark.parametrize("orb,line", [(True, True), (True, False), (False, True), (False, False)])
def test_pure_repin_exact_new_group_and_untouched_contract(orb, line):
    text, before, after, record = repin_case(orb, line)
    candidate = m.preopen_candidate(text, SHA, before, after, record, orb=orb, line=line).decode()
    assert set(re.findall(r"--restarted ([a-z0-9-]+)", candidate)) == m.restart_group(orb, line)
    assert f'EXPECTED_OMS_PID={before["oms"]["MainPID"]}\n' in candidate
    assert f'EXPECTED_STRATEGY_PID={before["strategy"]["MainPID"]}\n' in candidate
    assert "oms:UNCHANGED=true" not in candidate and "--no-schema-change" in candidate
    assert f"EXPECTED_SHA={SHA}" in candidate
    assert (f"schwab-1m-v2:{m.L_FLAG}=true" in candidate) is line
    assert (f"orb-schwab:{m.O_FLAG}=true" in candidate) is orb
    assert "EXPECTED_DATE=\"$(TZ=America/New_York date +%F)\"" in candidate
    assert "daily.py paper" in candidate
    assert (f"{m.RPG_FLAG}=false" in candidate) is line
    assert subprocess.run(["bash", "-n"], input=candidate, text=True, capture_output=True).returncode == 0


@pytest.mark.parametrize("change", ["untouched", "same_pid", "failed", "restarted_oms", "unsafe_path", "late_start"])
def test_wrong_group_failed_or_unproven_start_refuses(change):
    text, before, after, record = repin_case()
    if change == "untouched":
        after["oms"]["MainPID"] = "300"
    elif change == "same_pid":
        after["control"]["MainPID"] = before["control"]["MainPID"]
    elif change == "failed":
        after["control"]["SubState"] = "failed"
    elif change == "restarted_oms":
        record["service_actions"]["oms"] = "restarted"
    elif change == "unsafe_path":
        record["snapshot"] = "/home/trader/../escape"
    else:
        after["control"]["ExecMainStartTimestamp"] = "Fri 2026-10-09 21:05:00 UTC"
    with pytest.raises(m.Refusal):
        m.preopen_candidate(text, SHA, before, after, record, orb=True, line=True)


def test_runtime_preserves_historical_inputs_and_rebinds_actual_bytes():
    old = dict(approved_sha="b" * 40, tree="c" * 40)
    runtime = dict(approved_sha=old["approved_sha"], artifacts={"binding.json": "old", "daily.py": "retained"},
                   evidence_inputs={"/historical/input": "immutable"}, release_sha256="historical")
    install = dict(approved_sha=SHA, tree="d" * 40, restarted=m.sequence(True, True),
                   snapshot="/current/snapshot", install_record="/current/record", source_journal="/current/journal")
    binding = dict(approved_sha=SHA, tree=install["tree"], historical_binding=old, current_install=install)
    binding_hash = m.digest((json.dumps(binding, indent=2, sort_keys=True) + "\n").encode())
    result_binding, result = m.runtime_candidate(runtime, old, b"new gate", {"binding.json": binding_hash},
                                                 {path: "e" * 64 for path in ("/current/snapshot", "/current/record", "/current/journal")}, install)
    assert result_binding == binding
    assert result["artifacts"]["daily.py"] == "retained"
    assert result["evidence_inputs"]["/historical/input"] == "immutable"
    assert result["release_sha256"] == "historical"
    assert result["gate_sha256"] == m.digest(b"new gate")
    assert runtime["approved_sha"] == "b" * 40


@pytest.mark.parametrize("orb,line", [(True, True), (True, False), (False, True), (False, False)])
def test_fi_group_includes_strategy_and_keeps_unactivated_line(orb, line):
    text, before, after, record = repin_case(orb, line)
    for index, owner in enumerate(m.restart_group(orb, line, True)):
        after[owner] = state(400 + index)
    record["service_actions"] = {
        owner: "restarted" if owner in m.restart_group(orb, line, True) else "deliberately_untouched"
        for owner in before}
    candidate = m.preopen_candidate(text, SHA, before, after, record, orb=orb, line=line, fi=True).decode()
    assert m.sequence(orb, line, True)[0] == "oms"
    assert m.sequence(orb, line, True)[1] == "schwab-1m-v2"
    assert m.sequence(orb, line, True)[-1] == "control"
    assert set(re.findall(r"--restarted ([a-z0-9-]+)", candidate)) == m.restart_group(orb, line, True)
    assert "oms:UNCHANGED=true" in candidate
    assert f"schwab-1m-v2:{m.L_FLAG}={'true' if line else 'false'}" in candidate
    assert f'EXPECTED_OMS_PID={after["oms"]["MainPID"]}\n' in candidate
    assert f'EXPECTED_STRATEGY_PID={after["strategy"]["MainPID"]}\n' in candidate
    assert subprocess.run(["bash", "-n"], input=candidate, text=True, capture_output=True).returncode == 0


@pytest.mark.parametrize("actual", [SHA, "b" * 40])
def test_remote_main_exact_full_binding_no_branch_update(monkeypatch, actual):
    commands = []

    def command(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, actual + "\trefs/heads/main\n", "")

    monkeypatch.setattr(m.subprocess, "run", command)
    if actual == SHA:
        assert m.verify_main(Path("/local/repository"), SHA)["verified_main_sha"] == SHA
    else:
        with pytest.raises(m.Refusal, match="moved/unbound"):
            m.verify_main(Path("/local/repository"), SHA)
    assert commands == [["git", "-C", "/local/repository", "ls-remote", "--exit-code", "--refs",
                         "origin", "refs/heads/main"]]


def test_cli_cannot_execute_or_hide_native_c6_missing_branch(capsys):
    assert m.main(["--expected-sha", SHA, "--orb"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["can_execute"] is False
    assert any("requires a v2 restart" in item for item in value["unsupported"])
    assert value["deploy_environment"]["MAI_TAI_RUN_MIGRATIONS"] == "0"
    with pytest.raises(SystemExit) as exc:
        m.main(["--execute"])
    assert exc.value.code == 2


def test_no_service_mutation_api_is_present():
    text = SCRIPT.read_text()
    assert "os.replace(" not in text and ".write_bytes(" not in text
    assert "systemctl" not in text and "git checkout" not in text
