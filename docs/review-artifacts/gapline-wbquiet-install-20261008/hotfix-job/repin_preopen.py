"""Re-pin measured Oct 8 identities; no service action, gate run or PASS claim."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import tempfile
import uuid

DAILY = "/home/trader/preopen-daily"
GATE = "/home/trader/preopen.sh"
REPO = "/home/trader/project-mai-tai"
RESTARTED = {"oms", "strategy", "schwab-1m-v2", "control"}
DEFAULT_SERVICES = {"control", "market-capture", "market-data", "oms", "reconciler",
                    "schwab-1m-v2", "strategy", "tv-alerts"}
PREFIX = "MAI_TAI_STRATEGY_SCHWAB_1M_V2_"
OVERRIDES = {PREFIX + "LINE_CHART_RESTORATION_ENABLED": "true",
             PREFIX + "ATR_REPRICE_HANDOFF_ENABLED": "false",
             PREFIX + "FALSE_FLIP_ENABLED": "true"}
GAP = PREFIX + "GAP_LINE_CARRY_ENABLED"
REFRESHABLE = {REPO + "/ops/health/v2_restart_evidence.py",
               REPO + "/src/project_mai_tai/strategy_core/time_utils.py",
               "/home/trader/restart_evidence/expected_flags.json",
               "/home/trader/restart_evidence/expected_numeric.json"}
FIELDS = ("MainPID", "NRestarts", "ActiveState", "SubState", "ExecMainStartTimestamp")
REQUIRED_ARTIFACTS = {"binding.json", "binding.py", "catalog_policy.py", "daily.py", "linesrc_disposition.py",
                      "notify.sh", "release_policy.py", "restart_report.py", "retry_zero_readonly.py",
                      "run.sh", "upgrade-ack.json", "upgrade_ack.py"}
REQUIRED_INPUTS = REFRESHABLE | {
    "/home/trader/restart_evidence/expected_flags_check.py",
    "/home/trader/restart_evidence/preopen_restart_evidence.sh",
    "/home/trader/restart_evidence/v2_restart_evidence.py",
    "/etc/systemd/system/project-mai-tai-preopen.service",
    "/etc/systemd/system/project-mai-tai-preopen.timer",
    "/etc/systemd/system/project-mai-tai-preopen-failure.service",
}


class Refusal(RuntimeError):
    pass


def need(ok, message):
    if not ok:
        raise Refusal(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value) is not None


def moment(value):
    value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    need(value.tzinfo is not None, "un-zoned evidence time")
    return value.astimezone(timezone.utc)


def system_time(value):
    need(re.fullmatch(r"[A-Za-z]{3} \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC", value) is not None,
         "non-UTC/unreadable service start")
    return datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def location(root, absolute):
    path = PurePosixPath(absolute)
    need(path.is_absolute() and str(path) == absolute and ".." not in path.parts, "path escape")
    result = root / str(path).lstrip("/")
    for parent in (result, *result.parents):
        if parent == root:
            break
        need(not parent.is_symlink(), "symlink path refused: " + absolute)
    return result


def replace_assignment(text, key, value):
    pattern = rf"^{re.escape(key)}=.*$"
    need(len(re.findall(pattern, text, re.M)) == 1, "missing/duplicate assignment: " + key)
    return re.sub(pattern, lambda _: key + "=" + shlex.quote(str(value)), text, flags=re.M)


def rebind_upgrade(source, old, new):
    pattern = r'^APP = "([a-f0-9]{40})"$'
    matches = re.findall(pattern, source, re.M)
    need(matches == [old], "upgrade APP binding unexpected")
    return re.sub(pattern, 'APP = "' + new + '"', source, flags=re.M).encode()


def gate_candidate(text, app, snapshot, record, states, environments, catalog, line_enabled=True):
    need('EXPECTED_DATE="$(TZ=America/New_York date +%F)"' in text
         and 'v2-restart-evidence-${EXPECTED_DATE//-/}.md' in text
         and "/home/trader/preopen-daily/daily.py paper" in text,
         "dynamic date/report/paper admission missing")
    result = replace_assignment(text, "EXPECTED_SHA", app)
    result = replace_assignment(result, "SNAPSHOT", snapshot)
    result = replace_assignment(result, "INSTALL_RECORD", record)
    for name, label in (("oms", "OMS_"), ("strategy", "STRATEGY_"), ("schwab-1m-v2", ""),
                        ("control", "CONTROL_")):
        result = replace_assignment(result, "EXPECTED_" + label + "PID", states[name]["MainPID"])
        result = replace_assignment(result, "EXPECTED_" + label + "START", states[name]["ExecMainStartTimestamp"])
    # Legacy ORB is untouched. Keep its existing independent identity check;
    # current Settings inventory does not authorize retiring the running unit.
    acknowledgement = ('if "$REPO/.venv/bin/python" /home/trader/preopen-daily/upgrade_ack.py; then\n'
        '  pass "orb-schwab exact Redis-upgrade restart ACKNOWLEDGED; actual NRestarts=1"\n'
        'else\n  fail "orb-schwab upgrade acknowledgement mismatch"\nfi\n')
    ordinary = 'check_identity orb-schwab "$ORB_SCHWAB_UNIT" "$EXPECTED_ORB_SCHWAB_PID" "$EXPECTED_ORB_SCHWAB_START"\n'
    need(result.count(acknowledgement) == 1 or result.count(ordinary) == 1, 'orb-schwab admission template ambiguous')
    # orb-schwab is untouched across both authorized installs. Its exact
    # acknowledged upgrade identity must stay pinned; never adopt a new PID.
    overrides = {**OVERRIDES, PREFIX + 'LINE_CHART_RESTORATION_ENABLED': str(line_enabled).lower()}
    original = re.findall(r"^  --expect-flag '([^']+)' \\\n", text, re.M)
    need(original and len(original) == len(set(original)), "expect-flag population ambiguous")
    flags = {}
    for expression in original:
        owner, pair = expression.split(":", 1)
        key, expected = pair.split("=", 1)
        if owner not in RESTARTED:
            continue
        expected = overrides.get(key, expected)
        need(environments[owner].get(key) == expected, "process flag differs: " + owner + ":" + key)
        flags[(owner, key)] = expected
    for owner in ("oms", "schwab-1m-v2"):
        for key, expected in overrides.items():
            need(environments[owner].get(key) == expected, "required OFF process key missing: " + owner + ":" + key)
            flags[(owner, key)] = expected
    need(environments["schwab-1m-v2"].get(GAP) == "true", "gap carry not explicitly ON")
    flags[("schwab-1m-v2", GAP)] = "true"
    for row in catalog:
        key = "MAI_TAI_" + row["name"].upper()
        expected = str(row["expected"]).lower()
        for owner in {row["owning_service"], *row.get("also_check_services", [])} & RESTARTED:
            if key in environments[owner]:
                need(environments[owner][key] == expected, "catalog/process mismatch: " + owner + ":" + key)
                flags[(owner, key)] = expected
    result = re.sub(r"^  --restarted [a-z0-9-]+ \\\n", "", result, flags=re.M)
    result = re.sub(r"^  --expect-flag '[^']+' \\\n", "", result, flags=re.M)
    anchor = '  --install-record "$INSTALL_RECORD" \\\n'
    need(result.count(anchor) == 1, "report install-record argument ambiguous")
    arguments = "".join("  --restarted " + owner + " \\\n" for owner in sorted(RESTARTED))
    for (owner, key), value in sorted(flags.items()):
        expression = owner + ":" + key + "=" + value
        need(re.fullmatch(r"[A-Za-z0-9_:=.*\-]+", expression), "unsafe flag expression")
        # Preserve the template's quoting so the next install can parse this pin.
        arguments += "  --expect-flag '" + expression + "' \\\n"
    result = result.replace(anchor, anchor + arguments)
    return result.encode()


def validate_evidence(before, record, states, now):
    need(before.get("schema_version") in {2, 3} and isinstance(before.get("services"), dict), "snapshot shape")
    need(DEFAULT_SERVICES <= set(before["services"]) and 'orb' not in before['services']
         and before.get("alembic_version"), "snapshot fleet/schema incomplete or retired ORB adopted")
    exposure = before.get("live_exposure", {})
    need({"accounts_found", "accounts_expected", "open_managed_rows", "nonzero_account_position_rows"} <= set(exposure),
         "snapshot exposure incomplete")
    need(record.get("schema_version") == 1
         and record.get("snapshot_captured_at_utc") == before.get("captured_at_utc"), "record/snapshot mismatch")
    need(set(record.get("service_actions", {})) == set(before["services"]), "record fleet incomplete")
    need(record["service_actions"] == {name: "restarted" if name in RESTARTED else "deliberately_untouched"
                                       for name in before["services"]}, "record restart group differs")
    need(RESTARTED <= set(before["services"]), "snapshot missing owning services")
    captured = moment(before["captured_at_utc"])
    need(captured <= now, "snapshot in future")
    for owner in RESTARTED:
        state = states[owner]
        need(state["MainPID"].isdigit() and int(state["MainPID"]) > 0
             and state["NRestarts"] == "0" and state["ActiveState"] == "active"
             and state["SubState"] == "running", "new service not active/NRestarts0: " + owner)
        need(int(state["MainPID"]) != int(before["services"][owner]["pid"])
             and captured <= system_time(state["ExecMainStartTimestamp"]) <= now,
             "new identity not proven: " + owner)


def plan(root, app, snapshot, record, observations, now, *, line_enabled=True, retirement=None):
    need(sha(app) and observations["head"] == app and observations["clean"] is True
         and sha(observations["tree"]), "checkout not exact clean application")
    def read(name):
        return location(root, name).read_bytes()
    before = json.loads(read(snapshot))
    actions = json.loads(read(record))
    validate_evidence(before, actions, observations["states"], now)
    inputs = (snapshot, record)
    if retirement is not None:
        from retire_orb import validate_receipt
        validate_receipt(json.loads(read(retirement)))
        inputs += (retirement,)
    journal = actions.get("source_journal")
    need(isinstance(journal, str) and read(journal).strip(), "install source journal absent/empty")
    old_runtime = json.loads(read(DAILY + "/runtime.json"))
    need(REQUIRED_ARTIFACTS <= set(old_runtime.get("artifacts", {})), "daily dependency artifact omitted")
    need(REQUIRED_INPUTS <= set(old_runtime.get("evidence_inputs", {})), "daily dependency evidence omitted")
    expected_uid = 0 if root == Path("/") else os.geteuid()
    for name in ("runtime.json", *old_runtime["artifacts"]):
        metadata = location(root, DAILY + "/" + name).stat()
        need(metadata.st_uid == expected_uid and stat.S_IMODE(metadata.st_mode) & 0o022 == 0,
             "daily artifact owner/writability: " + name)
    old_binding = json.loads(read(DAILY + "/binding.json"))
    old_app = old_binding.get("approved_sha")
    need(sha(old_app) and old_runtime["approved_sha"] == old_app, "old bindings disagree")
    need(read(REPO + "/ops/health/preopen_alert.sh")
         and digest(read(REPO + "/ops/health/preopen_alert.sh")) == old_runtime["adapter_sha256"], "adapter drift")
    need('ADAPTER = "' + old_runtime["adapter_sha256"] + '"' in read(DAILY + "/release_policy.py").decode(),
         "release policy adapter differs")
    for name, expected in old_runtime["artifacts"].items():
        need(Path(name).name == name and digest(read(DAILY + "/" + name)) == expected, "daily artifact drift: " + name)
    for path, expected in old_runtime["evidence_inputs"].items():
        if path not in REFRESHABLE:
            need(digest(read(path)) == expected, "historical evidence drift: " + path)
    ack = json.loads(read(DAILY + "/upgrade-ack.json"))
    need(ack["application"] == old_app and ack["decision"] == "ACKNOWLEDGED_REDIS_UPGRADE_RESTART",
         "historical upgrade acknowledgement binding differs")
    need(all(observations['upgrade_state'].get(key) == value for key, value in ack['state'].items()),
         'untouched orb-schwab upgrade identity changed')
    ack['historical_receipt'] = json.loads(read(DAILY + '/upgrade-ack.json'))
    ack["application"] = app
    ack['active_for_current_install'] = True
    ack['preserved_untouched_identity'] = dict(snapshot=snapshot, install_record=record,
        source_journal=journal, reason='orb-schwab not restarted; exact historical acknowledgement preserved')
    flags = json.loads(read("/home/trader/restart_evidence/expected_flags.json"))["flags"]
    overrides = {**OVERRIDES, PREFIX + 'LINE_CHART_RESTORATION_ENABLED': str(line_enabled).lower()}
    for key, expected in {**overrides, GAP: "true"}.items():
        rows = [row for row in flags if "MAI_TAI_" + row["name"].upper() == key]
        need(len(rows) == 1 and rows[0]["expected"] is (expected == "true")
             and rows[0]["owning_service"] == "schwab-1m-v2", "required catalog row missing/drift: " + key)
    result = {GATE: gate_candidate(read(GATE).decode(), app, snapshot, record,
                                   observations["states"], observations["environments"], flags, line_enabled)}
    binding = dict(approved_sha=app, tree=observations["tree"], historical_binding=old_binding,
                   historical_binding_sha256=digest(read(DAILY + "/binding.json")),
                   current_install=dict(snapshot=snapshot, install_record=record, source_journal=journal,
                                        retirement=retirement,
                                        hashes={path: digest(read(path)) for path in (*inputs, journal)},
                                        restarted=sorted(RESTARTED)))
    result[DAILY + "/binding.json"] = canonical(binding)
    # APP and TREE already derive from binding.json; do not change historical installer policy constants.
    policy = read(DAILY + "/release_policy.py").decode()
    need('APP = BINDING.get("approved_sha",' in policy and 'TREE = BINDING.get("tree",' in policy,
         "release APP/TREE are not binding-derived")
    result[DAILY + "/upgrade-ack.json"] = canonical(ack)
    result[DAILY + "/upgrade_ack.py"] = rebind_upgrade(read(DAILY + "/upgrade_ack.py").decode(), old_app, app)
    wrapper = read(DAILY + '/restart_report.py').decode()
    old_condition = "    if Path('/home/trader/preopen-daily/upgrade-ack.json').is_file():"
    new_condition = ("    if (Path('/home/trader/preopen-daily/upgrade-ack.json').is_file()\n"
        "            and json.loads(Path('/home/trader/preopen-daily/upgrade-ack.json').read_bytes()).get('active_for_current_install', True)):")
    need(wrapper.count(old_condition) == 1 or wrapper.count(new_condition) == 1, 'upgrade wrapper template ambiguous')
    result[DAILY + '/restart_report.py'] = wrapper.replace(old_condition, new_condition).encode()
    gate_stat = location(root, GATE).stat()
    need(stat.S_IMODE(gate_stat.st_mode) == 0o700 and gate_stat.st_uid == old_runtime["gate_uid"], "gate owner/mode drift")
    runtime = json.loads(read(DAILY + "/runtime.json"))
    runtime["approved_sha"] = app
    runtime["gate_sha256"] = digest(result[GATE])
    for name in runtime["artifacts"]:
        path = DAILY + "/" + name
        runtime["artifacts"][name] = digest(result.get(path, read(path)))
    for path in REFRESHABLE & set(runtime["evidence_inputs"]):
        runtime["evidence_inputs"][path] = digest(read(path))
    for path in (*inputs, journal):
        runtime["evidence_inputs"][path] = digest(read(path))
    numerics = json.loads(read("/home/trader/restart_evidence/expected_numeric.json"))["settings"]
    def count(rows):
        return sum(1 + len(row.get("also_check_services", [])) for row in rows)
    runtime["catalog_counts"] = dict(boolean=count(flags), numeric=count(numerics), total=count(flags) + count(numerics))
    # The old release hash remains historical. This receipt separately binds the new inputs.
    runtime["historical_release_sha256"] = runtime["release_sha256"]
    runtime["current_install"] = binding["current_install"]
    result[DAILY + "/runtime.json"] = canonical(runtime)
    return result


def atomic(path, raw, uid, gid, mode):
    fd, temporary = tempfile.mkstemp(prefix=".repin-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply(root, candidates, now):
    originals = {name: (location(root, name).read_bytes(), location(root, name).stat()) for name in candidates}
    backup_parent = location(root, DAILY + "/repin-backups")
    backup = backup_parent / (now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex)
    backup.mkdir(parents=True, mode=0o700)
    rows = {}
    for name, (raw, metadata) in originals.items():
        target = backup / Path(name).name
        atomic(target, raw, metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode))
        rows[name] = dict(backup=str(target), before=digest(raw), after=digest(candidates[name]))
    # Publish runtime last: an interrupted multi-file update fails its hashes, never invents PASS.
    for name, raw in candidates.items():
        old, metadata = originals[name]
        need(location(root, name).read_bytes() == old, "concurrent edit: " + name)
        atomic(location(root, name), raw, metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode))
    receipt = dict(verdict="REPINNED_CHECKS_NOT_RUN", files=rows, measured_at_utc=now.isoformat())
    atomic(backup / "receipt.json", canonical(receipt), os.geteuid(), os.getegid(), 0o600)
    return receipt


def collect():
    def run(command):
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    def state(service):
        raw = run(["systemctl", "show", "project-mai-tai-" + service + ".service",
                   *["--property=" + name for name in FIELDS]])
        return dict(line.split("=", 1) for line in raw.splitlines())
    states, envs = {}, {}
    for owner in sorted(RESTARTED):
        current = state(owner)
        raw = Path("/proc/" + current["MainPID"] + "/environ").read_bytes()
        need(0 < len(raw) <= 262144, "process environment absent/overflow")
        pairs = [piece.decode().split("=", 1) for piece in raw.split(b"\0") if piece]
        selected = [(key, value) for key, value in pairs if key.startswith("MAI_TAI_")]
        need(len({key for key, _ in selected}) == len(selected), "duplicate process setting")
        envs[owner] = dict(selected)
        need(state(owner) == current, "identity changed during process read")
        states[owner] = current
    raw = run(["systemctl", "show", "project-mai-tai-orb-schwab.service", *["--property=" + name for name in (*FIELDS, "InvocationID")]])
    return dict(states=states, environments=envs, upgrade_state=dict(line.split("=", 1) for line in raw.splitlines()),
                head=run(["git", "-C", REPO, "rev-parse", "HEAD"]),
                tree=run(["git", "-C", REPO, "rev-parse", "HEAD^{tree}"]),
                clean=not run(["git", "-C", REPO, "status", "--porcelain"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/"))
    parser.add_argument("--approved-sha", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--install-record", required=True)
    parser.add_argument('--line-enabled', choices=('true', 'false'), required=True)
    parser.add_argument("--receipt", help="exclusive output path; the receipt never asserts the daily gate passed")
    parser.add_argument("--observations", type=Path, help="isolated fixture root only; never accepted for production")
    parser.add_argument("--dry-run", action="store_true", help="read-only preview; default writes only the declared repins")
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        need(root != Path("/") or os.geteuid() == 0, "production repin requires root")
        need((root == Path("/")) == (args.observations is None), "isolated root/observations pair required")
        observations = collect() if args.observations is None else json.loads(args.observations.read_bytes())
        now = datetime.now(timezone.utc)
        if not args.dry_run:
            need(args.receipt is not None, "write requires --receipt")
            need(not location(root, args.receipt).exists(), "receipt already exists")
        with location(root, DAILY + "/run.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            candidates = plan(root, args.approved_sha, args.snapshot, args.install_record, observations, now,
                              line_enabled=args.line_enabled == 'true', retirement=None)
            for name, raw in candidates.items():
                if name.endswith(".py"):
                    ast.parse(raw)
            subprocess.run(["bash", "-n"], input=candidates[GATE], check=True, capture_output=True, timeout=5)
            receipt = (dict(verdict="PREVIEW_CHECKS_NOT_RUN", hashes={name: digest(raw) for name, raw in candidates.items()})
                       if args.dry_run else apply(root, candidates, now))
            if args.receipt:
                destination = location(root, args.receipt)
                with destination.open("xb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(canonical(receipt))
                    stream.flush()
                    os.fsync(stream.fileno())
            print(json.dumps(receipt, sort_keys=True))
            return 0
    except Exception as exc:
        print("REFUSE preopen repin: " + str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
