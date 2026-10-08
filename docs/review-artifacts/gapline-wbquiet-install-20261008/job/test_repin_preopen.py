"""Isolated re-pin tests using read-only Oct8 box scripts; no broker/service actions."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import types

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("repin_preopen", HERE / "repin_preopen.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
APP = "a" * 40
TREE = "b" * 40
NOW = datetime(2026, 10, 8, 22, 10, tzinfo=timezone.utc)
SNAPSHOT = "/home/trader/attempt/before-restart.json"
RECORD = "/home/trader/attempt/install-record.json"
JOURNAL = "/home/trader/attempt/sealed-actions.json"
RETIREMENT = '/home/trader/attempt/orb-retirement.json'


def put(root, path, raw, mode=0o600):
    target = m.location(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    target.chmod(mode)
    return target


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "box"
    root.mkdir()
    for source in (HERE / "fixtures").iterdir():
        if source.name == "runtime.json":
            continue
        path = m.GATE if source.name == "preopen.sh" else m.DAILY + "/" + source.name
        put(root, path, source.read_bytes(), 0o700 if path == m.GATE else 0o600)
    text = m.location(root, m.GATE).read_text()
    states = {owner: dict(MainPID=str(9000 + i), NRestarts="0", ActiveState="active", SubState="running",
                          ExecMainStartTimestamp="Thu 2026-10-08 22:05:00 UTC")
              for i, owner in enumerate(sorted(m.RESTARTED))}
    envs = {owner: {} for owner in m.RESTARTED}
    for expression in re.findall(r"--expect-flag '([^']+)'", text):
        owner, pair = expression.split(":", 1)
        key, value = pair.split("=", 1)
        if owner in envs:
            envs[owner][key] = m.OVERRIDES.get(key, value)
    envs["schwab-1m-v2"][m.GAP] = "true"
    for owner in ('oms', 'schwab-1m-v2'):
        envs[owner].update(m.OVERRIDES)
    envs["oms"]["MAI_TAI_OMS_V2_EH_FRESH_PRICE_ENABLED"] = "true"
    ack = json.loads(m.location(root, m.DAILY + "/upgrade-ack.json").read_bytes())
    obs = dict(head=APP, tree=TREE, clean=True, states=states, environments=envs, upgrade_state=ack["state"])
    services = {owner: dict(pid=10 + i) for i, owner in enumerate(sorted(m.DEFAULT_SERVICES | {"orb-schwab"}))}
    snapshot = dict(schema_version=3, captured_at_utc="2026-10-08T22:00:00+00:00", services=services,
                    alembic_version="20261005_0022",
                    live_exposure=dict(accounts_found=2, accounts_expected=2, open_managed_rows=0, nonzero_account_position_rows=0),
                    v2_watchlist={})
    record = dict(schema_version=1, snapshot_captured_at_utc=snapshot["captured_at_utc"], source_journal=JOURNAL,
                  service_actions={owner: "restarted" if owner in m.RESTARTED else "deliberately_untouched" for owner in services})
    put(root, SNAPSHOT, m.canonical(snapshot))
    put(root, RECORD, m.canonical(record))
    put(root, JOURNAL, b'{"actual_commands":["deploy oms","deploy schwab-1m-v2"]}\n')
    from test_retire_orb import receipt
    put(root, RETIREMENT, m.canonical(receipt()))
    adapter = (HERE / "fixtures" / "release_policy.py").read_text().split('ADAPTER = "', 1)[1].split('"')[0]
    # Test root substitutes an isolated reviewed adapter; production requires its actual fixed hash.
    adapter_raw = b"#!/bin/bash\nexit 0\n"
    adapter_hash = m.digest(adapter_raw)
    policy = m.location(root, m.DAILY + "/release_policy.py")
    policy.write_text(policy.read_text().replace(adapter, adapter_hash))
    put(root, m.REPO + "/ops/health/preopen_alert.sh", adapter_raw)
    flag_rows = [dict(name=key[len("MAI_TAI_"):].lower(), expected=value == "true", owning_service="schwab-1m-v2",
                      also_check_services=["oms"] if key != m.GAP else [])
                 for key, value in {**m.OVERRIDES, m.GAP: "true"}.items()]
    flag_rows.append(dict(name="oms_v2_eh_fresh_price_enabled", expected=True, owning_service="oms"))
    put(root, "/home/trader/restart_evidence/expected_flags.json", m.canonical(dict(flags=flag_rows)))
    put(root, "/home/trader/restart_evidence/expected_numeric.json", m.canonical(dict(settings=[dict(name="x", expected=0, owning_service="oms")])))
    historical = "/home/trader/historical/immutable.json"
    put(root, historical, b'{"old":"unaltered evidence"}\n')
    runtime = json.loads((HERE / "fixtures/runtime.json").read_bytes())
    runtime["gate_uid"] = os.getuid()
    runtime["adapter_sha256"] = adapter_hash
    runtime["evidence_inputs"] = {historical: m.digest(m.location(root, historical).read_bytes()),
                                  "/home/trader/restart_evidence/expected_flags.json": "0" * 64,
                                  "/home/trader/restart_evidence/expected_numeric.json": "0" * 64}
    for path in m.REQUIRED_INPUTS:
        if path not in runtime['evidence_inputs']:
            put(root, path, b'isolated dependency fixture\n')
            runtime['evidence_inputs'][path] = m.digest(m.location(root, path).read_bytes())
    runtime["artifacts"] = {name: m.digest(m.location(root, m.DAILY + "/" + name).read_bytes()) for name in runtime["artifacts"]}
    put(root, m.DAILY + "/runtime.json", m.canonical(runtime))
    put(root, m.DAILY + "/run.lock", b"")
    return root, obs, historical


def build(case):
    root, obs, _ = case
    return m.plan(root, APP, SNAPSHOT, RECORD, obs, NOW)


def test_current_box_template_repin_group_and_process_keys(case):
    changes = build(case)
    gate = changes[m.GATE].decode()
    assert set(re.findall(r"--restarted ([\w-]+)", gate)) == m.RESTARTED
    assert "orb-schwab:" not in gate and 'EXPECTED_CONTROL_PID=' in gate
    assert 'EXPECTED_ORB_PID=' not in gate and 'check_identity orb ' not in gate
    assert f"EXPECTED_SHA={APP}" in gate
    assert f"SNAPSHOT={SNAPSHOT}" in gate and f"INSTALL_RECORD={RECORD}" in gate
    assert "ATR_REPRICE_HANDOFF_ENABLED=true" not in gate
    assert "LINE_CHART_RESTORATION_ENABLED=true" in gate
    assert m.GAP + "=true" in gate
    assert "oms:MAI_TAI_OMS_V2_EH_FRESH_PRICE_ENABLED=true" in gate
    assert 'EXPECTED_DATE="$(TZ=America/New_York date +%F)"' in gate
    assert 'v2-restart-evidence-${EXPECTED_DATE//-/}.md' in gate
    assert "daily.py paper" in gate
    assert "restart_report.py report" in gate
    subprocess.run(["bash", "-n"], input=changes[m.GATE], check=True, capture_output=True)


def test_refresh_all_application_bindings_preserve_history_and_ack(case):
    root, _, historical = case
    old_binding = json.loads(m.location(root, m.DAILY + "/binding.json").read_bytes())
    old_ack = json.loads(m.location(root, m.DAILY + "/upgrade-ack.json").read_bytes())
    changes = build(case)
    binding = json.loads(changes[m.DAILY + "/binding.json"])
    assert binding["approved_sha"] == APP and binding["tree"] == TREE
    assert binding["historical_binding"] == old_binding
    ack = json.loads(changes[m.DAILY + "/upgrade-ack.json"])
    assert ack['historical_receipt'] == old_ack
    assert ack['state'] == old_ack['state']
    assert ack['application'] == APP and ack['active_for_current_install'] is True
    assert 'preserved_untouched_identity' in ack
    assert f'APP = "{APP}"' in changes[m.DAILY + "/upgrade_ack.py"].decode()
    assert 'APP = BINDING.get("approved_sha",' in m.location(root, m.DAILY + "/release_policy.py").read_text()
    runtime = json.loads(changes[m.DAILY + "/runtime.json"])
    assert runtime["approved_sha"] == APP and runtime["gate_sha256"] == m.digest(changes[m.GATE])
    for name, digest in runtime["artifacts"].items():
        path = m.DAILY + "/" + name
        assert digest == m.digest(changes.get(path, m.location(root, path).read_bytes()))
    assert runtime["evidence_inputs"][historical] == m.digest(m.location(root, historical).read_bytes())
    assert {SNAPSHOT, RECORD, JOURNAL} <= runtime["evidence_inputs"].keys()
    assert runtime["catalog_counts"] == dict(boolean=8, numeric=1, total=9)


def test_repin_flag_arguments_remain_parseable_on_next_install(case):
    root, obs, _ = case
    first = build(case)[m.GATE]
    catalog = json.loads(m.location(root, "/home/trader/restart_evidence/expected_flags.json").read_bytes())["flags"]
    assert re.findall(r"^  --expect-flag '([^']+)' \\\n", first.decode(), re.M)
    second = m.gate_candidate(first.decode(), APP, SNAPSHOT, RECORD, obs["states"], obs["environments"], catalog)
    assert second == first


def test_atomic_writes_backups_hashes_owner_mode_and_no_fake_pass(case):
    root, _, historical = case
    originals = {name: m.location(root, name).read_bytes() for name in build(case)}
    history = m.location(root, historical).read_bytes()
    receipt = m.apply(root, build(case), NOW)
    assert receipt["verdict"] == "REPINNED_CHECKS_NOT_RUN"
    for name, row in receipt["files"].items():
        assert Path(row["backup"]).read_bytes() == originals[name]
        assert m.digest(m.location(root, name).read_bytes()) == row["after"]
        assert row["before"] == m.digest(originals[name])
    assert m.location(root, m.GATE).stat().st_mode & 0o777 == 0o700
    assert m.location(root, m.GATE).stat().st_uid == os.getuid()
    assert m.location(root, historical).read_bytes() == history


@pytest.mark.parametrize("field,value", [("head", "c" * 40), ("clean", False), ("tree", "bad")])
def test_checkout_wrong_dirty_or_unreadable_refuses_before_write(case, field, value):
    case[1][field] = value
    with pytest.raises(m.Refusal, match="checkout"):
        build(case)


@pytest.mark.parametrize("field,value", [("MainPID", "0"), ("NRestarts", "1"), ("ActiveState", "failed"),
                                        ("SubState", "dead"), ("ExecMainStartTimestamp", "Thu 2026-10-08 18:00:00 EDT"),
                                        ("ExecMainStartTimestamp", "Thu 2026-10-08 21:00:00 UTC"),
                                        ("ExecMainStartTimestamp", "Thu 2026-10-08 23:00:00 UTC")])
def test_inactive_restarted_unproven_timestamp_refuses(case, field, value):
    case[1]["states"]["oms"][field] = value
    with pytest.raises(m.Refusal):
        build(case)


@pytest.mark.parametrize("key,value", [(m.GAP, "false"), (m.GAP, None),
                                     (m.PREFIX + "ATR_REPRICE_HANDOFF_ENABLED", "true"),
                                     (m.PREFIX + "LINE_CHART_RESTORATION_ENABLED", "false")])
def test_required_live_switch_mismatch_is_not_adopted(case, key, value):
    case[1]["environments"]["schwab-1m-v2"][key] = value
    with pytest.raises(m.Refusal):
        build(case)


def test_historical_evidence_drift_not_rehashed_to_green(case):
    root, _, historical = case
    m.location(root, historical).write_text("changed")
    with pytest.raises(m.Refusal, match="historical evidence drift"):
        build(case)


def test_adapter_drift_not_rehashed_to_green(case):
    m.location(case[0], m.REPO + "/ops/health/preopen_alert.sh").write_text("changed")
    with pytest.raises(m.Refusal, match="adapter drift"):
        build(case)


def test_daily_artifact_drift_not_rehashed_to_green(case):
    m.location(case[0], m.DAILY + "/daily.py").write_text("changed")
    with pytest.raises(m.Refusal, match="daily artifact drift"):
        build(case)


def test_upgrade_ack_never_adopts_another_restart(case):
    case[1]['upgrade_state'] = {**case[1]['upgrade_state'], 'MainPID': '999999'}
    with pytest.raises(m.Refusal, match="untouched orb-schwab"):
        build(case)


def test_snapshot_record_mismatch_no_writes(case):
    path = m.location(case[0], RECORD)
    data = json.loads(path.read_bytes())
    data["snapshot_captured_at_utc"] = "2026-10-08T21:00:00Z"
    path.write_bytes(m.canonical(data))
    with pytest.raises(m.Refusal, match="record/snapshot"):
        build(case)


def test_unintended_restart_not_hidden_by_group_repin(case):
    path = m.location(case[0], RECORD)
    data = json.loads(path.read_bytes())
    data["service_actions"]["market-data"] = "restarted"
    path.write_bytes(m.canonical(data))
    with pytest.raises(m.Refusal, match="restart group"):
        build(case)


def test_symlink_target_refused(case):
    path = m.location(case[0], m.DAILY + "/binding.json")
    raw = path.read_bytes()
    path.unlink()
    target = case[0] / "foreign"
    target.write_bytes(raw)
    path.symlink_to(target)
    with pytest.raises(m.Refusal, match="symlink"):
        build(case)


def test_existing_daily_verify_runtime_accepts_complete_repin(case, monkeypatch):
    root, _, _ = case
    m.apply(root, build(case), NOW)
    policy = types.ModuleType("release_policy")
    policy.__file__ = str(m.location(root, m.DAILY + "/release_policy.py"))
    exec(compile(Path(policy.__file__).read_bytes(), policy.__file__, "exec"), policy.__dict__)
    assert policy.APP == APP and policy.TREE == TREE
    monkeypatch.setitem(sys.modules, "release_policy", policy)
    daily = types.ModuleType("isolated_daily")
    exec(compile(m.location(root, m.DAILY + "/daily.py").read_bytes(), "daily.py", "exec"), daily.__dict__)
    daily.ROOT = m.location(root, m.DAILY)
    daily.GATE = m.location(root, m.GATE)
    daily.REPO = m.location(root, m.REPO)
    original_stat = Path.stat
    def root_owned(path, *args, **kwargs):
        metadata = original_stat(path, *args, **kwargs)
        values = list(metadata)
        if path != daily.GATE:
            values[4] = 0
        return os.stat_result(values)
    # Model root ownership in a private tree on the non-root development host.
    with monkeypatch.context() as mock:
        mock.setattr(Path, "stat", root_owned)
        mock.setattr(daily.os, "geteuid", lambda: 0)
        # Absolute evidence paths are production paths, relocated only in this fixture.
        pin = json.loads((daily.ROOT / "runtime.json").read_bytes())
        pin["evidence_inputs"] = {str(m.location(root, path)): value for path, value in pin["evidence_inputs"].items()}
        (daily.ROOT / "runtime.json").write_bytes(m.canonical(pin))
        assert daily.verify_runtime()["approved_sha"] == APP
    assert daily.paper_shape.__code__.co_code
    assert m.location(root, m.DAILY + "/daily.py").read_bytes() == (HERE / "fixtures/daily.py").read_bytes()


def test_old_gate_non_target_identity_and_dynamic_paper_lines_unchanged(case):
    root, _, _ = case
    old = m.location(root, m.GATE).read_text()
    new = build(case)[m.GATE].decode()
    for prefix in ("EXPECTED_MARKET_DATA_", "EXPECTED_PAPER_"):
        assert [line for line in old.splitlines() if line.startswith(prefix)] == [line for line in new.splitlines() if line.startswith(prefix)]


@pytest.mark.parametrize("defect", ["missing", "wrong_expected", "wrong_owner", "duplicate"])
def test_gap_catalog_shape_cannot_be_certified_from_process_alone(case, defect):
    path = m.location(case[0], "/home/trader/restart_evidence/expected_flags.json")
    data = json.loads(path.read_bytes())
    row = next(row for row in data["flags"] if row["name"] == m.GAP[len("MAI_TAI_"):].lower())
    if defect == "missing":
        data["flags"].remove(row)
    elif defect == "wrong_expected":
        row["expected"] = False
    elif defect == "wrong_owner":
        row["owning_service"] = "oms"
    else:
        data["flags"].append(row.copy())
    path.write_bytes(m.canonical(data))
    with pytest.raises(m.Refusal, match="catalog row"):
        build(case)


def test_world_writable_daily_artifact_refuses(case):
    m.location(case[0], m.DAILY + "/daily.py").chmod(0o666)
    with pytest.raises(m.Refusal, match="owner/writability"):
        build(case)


def test_unchanged_pid_not_relabelled_as_restart(case):
    before = json.loads(m.location(case[0], SNAPSHOT).read_bytes())
    case[1]["states"]["oms"]["MainPID"] = str(before["services"]["oms"]["pid"])
    with pytest.raises(m.Refusal, match="identity not proven"):
        build(case)


def test_atomic_interruption_never_refreshes_runtime_to_incomplete_bytes(case, monkeypatch):
    root, _, _ = case
    runtime = m.location(root, m.DAILY + "/runtime.json").read_bytes()
    real = m.atomic
    writes = []
    def fail(path, *args):
        if "repin-backups" not in str(path):
            writes.append(path)
            if len(writes) == 2:
                raise OSError("simulated write failure")
        return real(path, *args)
    monkeypatch.setattr(m, "atomic", fail)
    with pytest.raises(OSError, match="simulated"):
        m.apply(root, build(case), NOW)
    assert m.location(root, m.DAILY + "/runtime.json").read_bytes() == runtime
    assert len(list(m.location(root, m.DAILY + "/repin-backups").glob("*/*"))) == len(build(case))


@pytest.mark.parametrize("dry", [True, False])
def test_parent_runner_cli_contract_and_receipt(case, dry):
    root, observations, _ = case
    now = datetime.now(timezone.utc)
    captured = (now - timedelta(minutes=10)).isoformat()
    snapshot_path = m.location(root, SNAPSHOT)
    snapshot = json.loads(snapshot_path.read_bytes())
    snapshot["captured_at_utc"] = captured
    snapshot_path.write_bytes(m.canonical(snapshot))
    record_path = m.location(root, RECORD)
    record = json.loads(record_path.read_bytes())
    record["snapshot_captured_at_utc"] = captured
    record_path.write_bytes(m.canonical(record))
    for row in observations["states"].values():
        row["ExecMainStartTimestamp"] = (now - timedelta(minutes=5)).strftime("%a %Y-%m-%d %H:%M:%S UTC")
    observation_path = root / "observations.json"
    observation_path.write_bytes(m.canonical(observations))
    before = m.location(root, m.GATE).read_bytes()
    command = [sys.executable, str(HERE / "repin_preopen.py"), "--root", str(root),
               "--approved-sha", APP, "--snapshot", SNAPSHOT, "--install-record", RECORD,
               '--line-enabled', 'true', '--retirement', RETIREMENT,
               "--receipt", "/home/trader/attempt/preopen-repin.json", "--observations", str(observation_path)]
    result = subprocess.run(command + (["--dry-run"] if dry else []), capture_output=True, text=True, check=True)
    receipt = json.loads(result.stdout)
    assert receipt["verdict"] == ("PREVIEW_CHECKS_NOT_RUN" if dry else "REPINNED_CHECKS_NOT_RUN")
    assert json.loads(m.location(root, "/home/trader/attempt/preopen-repin.json").read_bytes()) == receipt
    assert (m.location(root, m.GATE).read_bytes() == before) is dry
    if not dry:
        repeated = subprocess.run(command, capture_output=True, text=True)
        assert repeated.returncode == 2 and "receipt already exists" in repeated.stdout


def test_isolated_observations_cannot_be_used_as_production_proof(tmp_path):
    result = subprocess.run([sys.executable, str(HERE / "repin_preopen.py"), "--root", "/",
                             "--approved-sha", APP, "--snapshot", SNAPSHOT, "--install-record", RECORD,
                             '--line-enabled', 'true', '--retirement', RETIREMENT,
                             "--observations", str(tmp_path / "fake.json"), "--dry-run"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "requires root" in result.stdout or "observations pair" in result.stdout


@pytest.mark.parametrize('name', sorted(m.REQUIRED_ARTIFACTS))
def test_omitting_runtime_dependency_never_rehashes_to_green(case, name):
    path = m.location(case[0], m.DAILY + '/runtime.json')
    value = json.loads(path.read_bytes())
    del value['artifacts'][name]
    path.write_bytes(m.canonical(value))
    with pytest.raises(m.Refusal, match='dependency artifact omitted'):
        build(case)


@pytest.mark.parametrize('name', sorted(m.REQUIRED_INPUTS))
def test_omitting_runtime_evidence_dependency_never_rehashes_to_green(case, name):
    path = m.location(case[0], m.DAILY + '/runtime.json')
    value = json.loads(path.read_bytes())
    del value['evidence_inputs'][name]
    path.write_bytes(m.canonical(value))
    with pytest.raises(m.Refusal, match='dependency evidence omitted'):
        build(case)


def test_control_repin_preserves_untouched_orb_schwab_ack_identity(case):
    changes = build(case)
    gate = changes[m.GATE].decode()
    for name, label in [('control', 'CONTROL_')]:
        assert 'EXPECTED_' + label + 'PID=' + case[1]['states'][name]['MainPID'] in gate
    assert 'upgrade_ack.py; then' in gate
    assert 'upgrade_ack.py' in gate
    wrapper = changes[m.DAILY + '/restart_report.py'].decode()
    assert "get('active_for_current_install', True)" in wrapper
    assert json.loads(changes[m.DAILY + '/upgrade-ack.json'])['active_for_current_install'] is True


def test_old_collector_or_retired_orb_population_not_adopted(case):
    path = m.location(case[0], SNAPSHOT)
    value = json.loads(path.read_bytes())
    value['services']['orb'] = {'pid': 1322003}
    path.write_bytes(m.canonical(value))
    with pytest.raises(m.Refusal, match='retired ORB adopted'):
        build(case)


def test_four_service_repin_does_not_fabricate_or_require_orb_retirement(case):
    root, obs, _ = case
    result = m.plan(root, APP, SNAPSHOT, RECORD, obs, NOW)
    current = json.loads(result[m.DAILY + '/binding.json'])['current_install']
    assert current['retirement'] is None
    assert RETIREMENT not in current['hashes']
    assert current['restarted'] == sorted(m.RESTARTED)
