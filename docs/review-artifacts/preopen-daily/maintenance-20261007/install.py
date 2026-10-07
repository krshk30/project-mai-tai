"""Install tested preopen mechanics only, with backups. Never acts on an app service."""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from upgrade_ack import APP, EXPECTED, validate

ROOT = Path("/home/trader/preopen-daily")
REPO = Path("/home/trader/project-mai-tai")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def call(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=True)
    return result.stdout


def gate_text(text):
    replacements = {
        "EXPECTED_ORB_SCHWAB_PID=612486": "EXPECTED_ORB_SCHWAB_PID=765206",
        "EXPECTED_ORB_SCHWAB_START='Tue 2026-10-06 23:50:33 UTC'": "EXPECTED_ORB_SCHWAB_START='Wed 2026-10-07 06:31:14 UTC'",
        'check_identity orb-schwab "$ORB_SCHWAB_UNIT" "$EXPECTED_ORB_SCHWAB_PID" "$EXPECTED_ORB_SCHWAB_START"':
        'if "$REPO/.venv/bin/python" /home/trader/preopen-daily/upgrade_ack.py; then\n'
        '  pass "orb-schwab exact Redis-upgrade restart ACKNOWLEDGED; actual NRestarts=1"\n'
        'else\n  fail "orb-schwab upgrade acknowledgement mismatch"\nfi',
    }
    for before, after in replacements.items():
        if text.count(before) != 1:
            raise ValueError("gate source differs from measured original: " + before)
        text = text.replace(before, after)
    return text


def atomic(path, raw, mode, uid=0, gid=0):
    temp = path.with_name(path.name + ".morning-20261007.tmp")
    with temp.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, mode)
    os.chown(temp, uid, gid)
    os.replace(temp, path)


def main():
    assert os.geteuid() == 0
    with Path("/run/lock/project-mai-tai-deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (ROOT / "run.lock").open("a") as gate_lock:
            fcntl.flock(gate_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            execute()


def execute():
    here = Path(__file__).parent
    head = call(["git", "-c", "safe.directory=" + str(REPO), "-C", str(REPO), "rev-parse", "HEAD"]).strip()
    assert head == APP
    assert not call(["git", "-c", "safe.directory=" + str(REPO), "-C", str(REPO), "status", "--porcelain"]).strip()
    state = dict(line.split("=", 1) for line in call(["systemctl", "show", "project-mai-tai-orb-schwab.service",
        *sum((["-p", key] for key in EXPECTED), [])]).splitlines())
    ack = dict(application=APP, unit="project-mai-tai-orb-schwab.service", decision="ACKNOWLEDGED_REDIS_UPGRADE_RESTART",
        redis_restart_utc="2026-10-07T06:31:08+00:00", redis_package="7.0.15-1ubuntu0.24.04.5", state=EXPECTED,
        source="operator/reviewer morning acknowledgement 2026-10-07 06:21ET; journal Redis stop/start06:31:08UTC, ORB crash06:31:09/restart06:31:14; unattended-upgrades.log")
    validate(ack, state, head)
    packages = call(["dpkg-query", "-W", "-f=${Package} ${Version}\n", "redis-server", "redis-tools"])
    assert set(packages.splitlines()) == {"redis-server 5:7.0.15-1ubuntu0.24.04.5", "redis-tools 5:7.0.15-1ubuntu0.24.04.5"}
    gate = Path("/home/trader/preopen.sh")
    old_runtime = json.loads((ROOT / "runtime.json").read_bytes())
    assert sha(gate) == old_runtime["gate_sha256"]
    for name, expected in old_runtime["artifacts"].items():
        assert sha(ROOT / name) == expected
    new_gate = gate_text(gate.read_text())
    check = subprocess.run(["bash", "-n"], input=new_gate, text=True, capture_output=True)
    assert check.returncode == 0, check.stderr
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = ROOT / ("maintenance-" + stamp)
    backup.mkdir(mode=0o700)
    paths = [gate, ROOT / "runtime.json", ROOT / "restart_report.py", Path("/etc/gitconfig"),
        Path("/etc/apt/apt.conf.d/52-project-mai-tai-unattended-upgrades")]
    saved = {}
    for i, path in enumerate(paths):
        if path.exists():
            destination = backup / (str(i) + "-" + path.name)
            shutil.copy2(path, destination)
            saved[str(path)] = dict(path=str(destination), sha256=sha(destination))
        else:
            saved[str(path)] = {"before": "ABSENT"}
    redis_conf = Path("/etc/apt/apt.conf.d/53-project-mai-tai-redis-hold")
    assert not redis_conf.exists()
    # Trust one literal deployment checkout, not every directory owned by another user.
    call(["git", "config", "--system", "--add", "safe.directory", str(REPO)])
    clean = {"PATH": "/usr/bin:/bin", "HOME": "/root"}
    assert subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], env=clean,
        text=True, capture_output=True, check=True).stdout.strip() == APP
    atomic(redis_conf, (here / "redis-hold.conf").read_bytes(), 0o644)
    apt_dump = call(["apt-config", "dump"])
    assert '"^redis-server$"' in apt_dump and '"^redis-tools$"' in apt_dump
    atomic(ROOT / "upgrade_ack.py", (here / "upgrade_ack.py").read_bytes(), 0o600)
    atomic(ROOT / "upgrade-ack.json", (json.dumps(ack, indent=2, sort_keys=True) + "\n").encode(), 0o600)
    atomic(ROOT / "restart_report.py", (here / "restart_report.py").read_bytes(), 0o600)
    stat = gate.stat()
    atomic(gate, new_gate.encode(), 0o700, stat.st_uid, stat.st_gid)
    runtime = old_runtime.copy()
    runtime["artifacts"] = dict(old_runtime["artifacts"])
    for name in ("upgrade_ack.py", "upgrade-ack.json", "restart_report.py"):
        runtime["artifacts"][name] = sha(ROOT / name)
    runtime["gate_sha256"] = sha(gate)
    atomic(ROOT / "runtime.json", (json.dumps(runtime, indent=2, sort_keys=True) + "\n").encode(), 0o600)
    diff = subprocess.run(["diff", "-u", str(backup / "0-preopen.sh"), str(gate)], capture_output=True, text=True)
    (backup / "preopen.diff").write_text(diff.stdout)
    receipt = dict(at_utc=datetime.now(timezone.utc).isoformat(), backups=saved, acknowledgement=ack,
        hashes={str(p): sha(p) for p in (gate, ROOT / "runtime.json", ROOT / "restart_report.py",
            ROOT / "upgrade_ack.py", ROOT / "upgrade-ack.json", redis_conf, Path("/etc/gitconfig"))},
        service_actions=[], application=head, package_versions=packages, gate_diff=str(backup / "preopen.diff"),
        security="Redis updates deferred to an attended maintenance window; persistence remains OFF")
    target = backup / "installed.json"
    target.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    journal = Path("/home/trader/fleet_health/deployments-20261007.md")
    with journal.open("a") as stream:
        stream.write("\n" + receipt["at_utc"] + " codex PREOPEN-MECHANICS installed receipt=" + str(target)
            + " sha256=" + sha(target) + " Redis unattended-upgrade exclusion server/tools; "
            + "exact root Git trust; ORB automatic restart ACKNOWLEDGED actual NRestarts=1, not reset. "
            + "No service restart or application change; original06:20 failure receipt retained.\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"receipt": str(target), "sha256": sha(target), "hashes": receipt["hashes"]}, sort_keys=True))


if __name__ == "__main__":
    main()
