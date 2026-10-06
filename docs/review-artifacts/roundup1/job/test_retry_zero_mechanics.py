"""Controlled process/env snapshots and literal rehearsal; never production proof."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import attended
import release_policy as policy
import retry_zero_readonly as retry

NOW = datetime(2026, 10, 7, 10, 20, tzinfo=timezone.utc)
RAW = (policy.RETRY_ENABLED + "=true\0" + policy.RETRY_MAX + "=0\0SECRET=not-returned\0").encode()


def current(pid="1234"):
    return dict(MainPID=pid, ActiveState="active", SubState="running", NRestarts="0",
                ExecMainStartTimestampMonotonic="100", InvocationID="a" * 32)


def controlled_receipt(now=NOW):
    rows = [retry.proof(service, current(str(1234 + i)), RAW, current(str(1234 + i)))
            for i, service in enumerate(retry.SERVICES)]
    return dict(verdict="PASS", kind="explicit-retry-zero-process-proof", checked=2, total=2,
                measured_at_utc=now.isoformat(), rows=rows)


def test_explicit_zero_values_exclude_secret_and_hash_entire_raw():
    result = retry.proof("oms", current(), RAW, current())
    assert result["values"] == retry.EXPECTED
    assert result["environ_sha256"] == policy.digest(RAW)
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("key", [policy.RETRY_ENABLED, policy.RETRY_MAX])
@pytest.mark.parametrize("bad", [None, "1", "-1", "false", "", "bad", "00", "0.0"])
def test_raw_missing_default_nonzero_negative_malformed_each_key_blocks(key, bad):
    values = dict(retry.EXPECTED)
    if bad is None:
        values.pop(key)
    else:
        values[key] = bad
    raw = b"\0".join((name + "=" + value).encode() for name, value in values.items())
    with pytest.raises(policy.Stop):
        retry.values(raw)


@pytest.mark.parametrize("key", [policy.RETRY_ENABLED, policy.RETRY_MAX])
@pytest.mark.parametrize("alias", [False, True])
def test_raw_duplicate_or_case_alias_blocks(key, alias):
    extra = key.lower() if alias else key
    with pytest.raises(policy.Stop, match="alias/duplicate"):
        retry.values(RAW + (extra + "=" + retry.EXPECTED[key]).encode())
    if alias:
        with pytest.raises(policy.Stop):
            retry.values(RAW.replace(key.encode(), extra.encode()))


@pytest.mark.parametrize("raw", [b"", b"malformed", b"\xff=bad", b"x" * (retry.MAX + 1)])
def test_raw_absent_malformed_utf8_overflow_blocks(raw):
    with pytest.raises((policy.Stop, UnicodeError)):
        retry.values(raw)


@pytest.mark.parametrize("field,value", [("MainPID", "0"), ("MainPID", "-1"), ("MainPID", "01"),
    ("ActiveState", "inactive"), ("SubState", "dead"), ("NRestarts", "1"),
    ("ExecMainStartTimestampMonotonic", "0"), ("InvocationID", "")])
def test_each_state_field_bad_or_drift_blocks(field, value):
    bad = dict(current(), **{field: value})
    with pytest.raises(policy.Stop):
        retry.proof("oms", bad, RAW, bad)
    changed = dict(current(), **{field: value})
    with pytest.raises(policy.Stop, match="incomplete/changed"):
        retry.proof("oms", current(), RAW, changed)


def test_missing_field_and_foreign_service_blocks():
    before = current()
    before.pop("InvocationID")
    with pytest.raises(policy.Stop):
        retry.proof("oms", before, RAW, before)
    with pytest.raises(policy.Stop):
        retry.proof("control", current(), RAW, current())


def test_collect_brackets_each_live_owner_read(monkeypatch):
    calls = []
    def state(service):
        calls.append(("state", service))
        return current()
    def environ(pid):
        calls.append(("environ", pid))
        return RAW
    monkeypatch.setattr(retry, "state", state)
    monkeypatch.setattr(retry, "environ", environ)
    result = retry.collect()
    assert result["checked"] == 2
    assert calls == [("state", "schwab-1m-v2"), ("environ", "1234"), ("state", "schwab-1m-v2"),
                     ("state", "oms"), ("environ", "1234"), ("state", "oms")]


def test_collect_unreadable_does_not_infer_zero(monkeypatch, capsys):
    monkeypatch.setattr(retry, "state", lambda service: current())
    def unreadable(pid):
        raise OSError("CONTROLLED hidden secret")
    monkeypatch.setattr(retry, "environ", unreadable)
    assert retry.main() == 2
    found = capsys.readouterr()
    assert not found.out and "hidden secret" not in found.err


@pytest.mark.parametrize("output", [b"", b"MainPID=1234\n", b"x" * 4097,
    b"MainPID=1234\nMainPID=1234\nActiveState=active\nSubState=running\nNRestarts=0\nInvocationID=" + b"a" * 32 + b"\n"])
def test_unreadable_state_reply_not_zero(monkeypatch, output):
    monkeypatch.setattr(retry.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=output, stderr=b""))
    with pytest.raises(policy.Stop):
        retry.state("oms")


def test_receipt_fresh_complete_control():
    value = controlled_receipt()
    assert retry.validate(value, NOW - timedelta(seconds=1), NOW) == value


@pytest.mark.parametrize("change", [
    lambda r: r.update(verdict="UNKNOWN"), lambda r: r.update(checked=1),
    lambda r: r.update(checked=True), lambda r: r.update(total=153),
    lambda r: r.update(rows=r["rows"][:1]),
    lambda r: r.update(rows=[r["rows"][0], r["rows"][0]]),
    lambda r: r.update(measured_at_utc=(NOW - timedelta(seconds=1)).isoformat()),
    lambda r: r.update(measured_at_utc=(NOW + timedelta(seconds=1)).isoformat()),
    lambda r: r["rows"][0]["values"].update({policy.RETRY_MAX: "1"}),
    lambda r: r["rows"][0]["after"].update(MainPID="9999"),
    lambda r: r["rows"][0].update(environ_sha256="")])
def test_receipt_stale_incomplete_changed_values_never_pass(change):
    receipt = copy.deepcopy(controlled_receipt())
    change(receipt)
    with pytest.raises(policy.Stop):
        retry.validate(receipt, NOW, NOW)


@pytest.mark.parametrize("raw", [b"", (policy.RETRY_ENABLED + "=false\n").encode(),
    (policy.RETRY_ENABLED.lower() + "=true\n").encode(),
    (policy.RETRY_ENABLED + "=true\n" + policy.RETRY_ENABLED + "=true\n").encode(),
    (policy.RETRY_ENABLED + "=true\nBAD='unterminated\n").encode()])
def test_env_retained_enabled_missing_off_alias_duplicate_malformed_blocks(raw):
    with pytest.raises(policy.Stop):
        attended.env_candidate(raw)


def test_env_zero_update_preserves_enabled_and_protected_bytes():
    retained = ("export " + policy.RETRY_ENABLED + "='true'\nPROTECTED=IPDN,MI,NXL\n").encode()
    raw = retained + (policy.RETRY_MAX + "=1\n").encode()
    value = attended.env_candidate(raw)
    assert value.startswith(retained)
    assert value.count((policy.RETRY_MAX + "=0\n").encode()) == 1
    assert len(value.splitlines()) == len(raw.splitlines()) + 3
    assert attended.env_candidate(value) == value


@pytest.mark.parametrize("raw", [policy.RETRY_MAX + "=1\n" + policy.RETRY_MAX + "=0\n",
                                  policy.RETRY_MAX.lower() + "=1\n"])
def test_env_numeric_duplicate_alias_never_repaired(raw):
    with pytest.raises(policy.Stop, match="duplicate|case-alias"):
        attended.env_candidate((policy.RETRY_ENABLED + "=true\n" + raw).encode())


def test_catalog_semantically_equal_unreviewed_bytes_block():
    from test_install_plan_contract import candidate_file
    raw = Path(__file__).with_name(policy.NUMERIC_ARTIFACT).read_bytes()
    baseline = candidate_file("ops/health/expected_numeric.json").encode()
    assert retry.catalog(raw, baseline)
    with pytest.raises(policy.Stop, match="artifact hash drift"):
        retry.catalog(raw + b"\n", baseline)


@pytest.mark.parametrize("change", [lambda rows: rows[0].update(expected=1), lambda rows: rows.pop(0)])
def test_catalog_baseline_alteration_blocks_even_with_reviewed_artifact(change):
    from test_install_plan_contract import candidate_file
    raw = Path(__file__).with_name(policy.NUMERIC_ARTIFACT).read_bytes()
    baseline = json.loads(candidate_file("ops/health/expected_numeric.json"))
    change(baseline["settings"])
    with pytest.raises(policy.Stop, match="alters baseline"):
        retry.catalog(raw, policy.canonical(baseline))


@pytest.mark.parametrize("action", ["stop", "start", "restart", "reload"])
def test_no_control_action_allowed_without_scope_update(tmp_path, action):
    fx = attended.Real(tmp_path, {}, tmp_path)
    with pytest.raises(policy.Stop, match="out-of-scope"):
        fx.action(action, "control")


def test_literal_catalog_drift_stops_before_first_source_env_write(monkeypatch, tmp_path):
    from test_literal_rehearsal import setup
    fx, gate, env = setup(monkeypatch, tmp_path)
    before = env.read_bytes()
    (fx.job / policy.NUMERIC_ARTIFACT).write_text("CONTROLLED unreviewed catalog")
    with pytest.raises(policy.Stop, match="artifact hash drift"):
        attended.Sequence(fx).run()
    assert env.read_bytes() == before and policy.digest(gate.read_bytes()) == policy.BASELINE_GATE
    assert not any("switch" in args or args[:2] == ["systemctl", "stop"] for args in fx.calls)
    assert (fx.attempt / "STOP.json").exists()


def test_literal_env_changed_after_admission_stops_before_source_switch(monkeypatch, tmp_path):
    from test_literal_rehearsal import setup
    fx, _, env = setup(monkeypatch, tmp_path)
    fx.initial()
    env.write_bytes(env.read_bytes() + b"CONTROLLED_CONCURRENT_EDIT=1\n")
    with pytest.raises(policy.Stop, match="env changed after prewrite"):
        fx.prepare()
    assert not any("switch" in args for args in fx.calls)


@pytest.mark.parametrize("enabled", [None, "false"])
def test_literal_enabled_missing_off_stops_before_any_write(monkeypatch, tmp_path, enabled):
    from test_literal_rehearsal import setup
    fx, _, env = setup(monkeypatch, tmp_path)
    raw = env.read_bytes().replace((policy.RETRY_ENABLED + "=true\n").encode(), b"")
    if enabled is not None:
        raw += (policy.RETRY_ENABLED + "=" + enabled + "\n").encode()
    env.write_bytes(raw)
    with pytest.raises(policy.Stop, match="retry-enabled"):
        attended.Sequence(fx).run()
    assert env.read_bytes() == raw
    assert not any("switch" in args or args[:2] == ["systemctl", "stop"] for args in fx.calls)


@pytest.mark.parametrize("key,value", [(policy.RETRY_MAX, None), (policy.RETRY_MAX, "1"),
    (policy.RETRY_MAX, "-1"), (policy.RETRY_MAX, "bad"), (policy.RETRY_ENABLED, "false"),
    (policy.RETRY_MAX.lower(), "0")])
def test_literal_wrong_new_process_values_stop_before_catalog_timer(monkeypatch, tmp_path, key, value):
    from test_literal_rehearsal import setup
    fx, gate, _ = setup(monkeypatch, tmp_path, retry_env={key: value})
    with pytest.raises(policy.Stop, match="retry process"):
        attended.Sequence(fx).run()
    assert not (tmp_path / "daily").exists()
    assert policy.digest(gate.read_bytes()) == policy.BASELINE_GATE
    assert (fx.attempt / "STOP.json").exists() and not (fx.attempt / "COMPLETE.json").exists()
