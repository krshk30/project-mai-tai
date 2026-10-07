"""Root checks-only timer and pure daily/paper admission. Never starts a trading unit."""
from datetime import datetime, time, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo
from release_policy import ADAPTER, APP, canonical, digest, moment, need

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
ROOT = Path("/home/trader/preopen-daily")
GATE = Path("/home/trader/preopen.sh")
REPO = Path("/home/trader/project-mai-tai")


def system_time(value):
    match = re.fullmatch(r"[A-Za-z]{3} (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC", value)
    need(match is not None, "unreadable/non-UTC systemd timestamp")
    return datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def paper_shape(now, paper, guard):
    local = now.astimezone(ET)
    need(local.time() < time(7), "paper gate outside pre07:00 bound")
    need(paper["ActiveState"] == "active" and paper["SubState"] == "running"
         and int(paper["MainPID"]) > 0 and paper["NRestarts"] == "0", "paper not active/NRestarts0")
    need(guard["ActiveState"] == "active" and int(guard["MainPID"]) > 0, "daily guard inactive")
    started, guarded = system_time(paper["ExecMainStartTimestamp"]), system_time(guard["ExecMainStartTimestamp"])
    floor = datetime.combine(local.date(), time(3, 40), ET).astimezone(UTC)
    need(floor <= started <= now and guarded <= started, "paper start before floor, out of order or future")
    return dict(paper=paper, guard=guard, floor_utc=floor.isoformat(), paper_after_floor=True,
                paper_after_guard=True, date_et=local.date().isoformat())


def calendar(now, holidays):
    local = now.astimezone(ET)
    need(local.year in {2026, 2027} and any(day.year == local.year for day in holidays), "calendar year unknown")
    need(local.time() < time(7), "preopen invocation after07:00")
    return local.weekday() < 5 and local.date() not in holidays


def report_stamp(raw, now):
    found = re.findall(r"^Generated: (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (?:EDT|EST) \((\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC\)$", raw, re.M)
    need(len(found) == 1, "missing/ambiguous report generation")
    local, absolute = found[0]
    stamp = datetime.strptime(absolute, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    need(stamp.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S") == local
         and stamp.astimezone(ET).date() == now.astimezone(ET).date(), "report date/zone mismatch")
    return stamp


def outcome(rc, raw, before, after, mtime):
    if rc not in (0, 1, 2):
        return 2
    try:
        stamp = report_stamp(raw, after)
        need(before.replace(microsecond=0) <= stamp <= after and before.timestamp() <= mtime <= after.timestamp(),
             "report not generated during invocation")
        finals = re.findall(r"^Final call: (PASS|FAIL|UNKNOWN|EXPECTED BY DESIGN);", raw, re.M)
        need(len(finals) == 1, "report verdict ambiguous")
        if rc == 0:
            need(finals[0] in {"PASS", "EXPECTED BY DESIGN", "ACCEPTED_OPEN_LINESRC1"}, "zero rc disagrees with report")
        return rc
    except Exception:
        return 2


def exclusive(path, raw, mode=0o600):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode), "wb") as file:
        file.write(raw)
        file.flush()
        os.fsync(file.fileno())


def verify_runtime(*, check_gate=True):
    need(os.geteuid() == 0, "daily service requires root")
    pin = ROOT / "runtime.json"
    need(not pin.is_symlink() and pin.stat().st_uid == 0 and pin.stat().st_mode & 0o022 == 0, "runtime pin not immutable to non-root")
    manifest = json.loads(pin.read_bytes())
    need(manifest["approved_sha"] == APP, "daily application pin differs")
    need({"daily.py", "release_policy.py", "binding.py", "binding.json", "retry_zero_readonly.py"}.issubset(manifest["artifacts"]),
         "daily retry proof artifact omitted")
    for name, expected in manifest["artifacts"].items():
        need(Path(name).name == name, "runtime path escape")
        path = ROOT / name
        need(not path.is_symlink() and path.stat().st_uid == 0 and path.stat().st_mode & 0o022 == 0
             and digest(path.read_bytes()) == expected, "daily artifact drift: " + name)
    if check_gate:
        need(not GATE.is_symlink() and GATE.stat().st_uid == manifest["gate_uid"]
             and GATE.stat().st_mode & 0o777 == 0o700 and digest(GATE.read_bytes()) == manifest["gate_sha256"], "preopen pin drift")
    adapter = REPO / "ops/health/preopen_alert.sh"
    need(digest(adapter.read_bytes()) == manifest["adapter_sha256"] == ADAPTER, "adapter drift")
    for path, expected in manifest["evidence_inputs"].items():
        file = Path(path)
        need(not file.is_symlink() and digest(file.read_bytes()) == expected, "isolated/calendar/snapshot evidence input drift")
    return manifest


def state(name):
    result = subprocess.run(["systemctl", "show", f"project-mai-tai-{name}.service", "-p", "MainPID",
                             "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts", "-p", "ExecMainStartTimestamp"],
                            check=True, capture_output=True, text=True, timeout=10)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def run_checks(now, holidays, invoke, read_report):
    need(calendar(now, holidays), "closed calendar day")
    rc, output, after = invoke()
    try:
        raw, mtime = read_report(now.astimezone(ET).strftime("%Y%m%d"))
    except Exception:
        raw, mtime = "", 0
    result = outcome(rc, raw, now, after, mtime)
    return result, dict(gate_rc=rc, verdict=("ACCEPTED_OPEN_LINESRC1" if result == 0 and "Final call: ACCEPTED_OPEN_LINESRC1;" in raw
                                 else {0: "PASS", 1: "FAIL", 2: "UNKNOWN"}[result]),
                        started_at_utc=now.isoformat(), completed_at_utc=after.isoformat(),
                        report_sha256=digest(raw.encode()) if raw else None), output


def run():
    verify_runtime()
    with (ROOT / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        now = datetime.now(UTC)
        destination = ROOT / "runs" / now.strftime("%Y%m%dT%H%M%S%fZ")
        destination.mkdir(mode=0o700)
        exclusive(destination / "started.json", canonical(dict(started_at_utc=now.isoformat(), gate_sha256=digest(GATE.read_bytes()))))
        # The failure unit can find a timeout/crash even before a completion receipt exists.
        temporary = ROOT / "latest.tmp"
        exclusive(temporary, canonical(dict(run=str(destination))))
        os.replace(temporary, ROOT / "latest.json")
        try:
            sys.path.insert(0, str(REPO / "src"))
            from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS
            if not calendar(now, US_MARKET_HOLIDAYS):
                exclusive(destination / "complete.json", canonical(dict(verdict="SKIP_CLOSED", gate_invoked=False)))
                return 0
            from retry_zero_readonly import validate
            since = datetime.now(UTC)
            result = subprocess.run([sys.executable, str(ROOT / "retry_zero_readonly.py")],
                                    capture_output=True, timeout=20)
            need(len(result.stdout) + len(result.stderr) <= 16384, "retry proof output exceeds bound")
            exclusive(destination / "retry-zero.stdout.json", result.stdout)
            exclusive(destination / "retry-zero.stderr.txt", result.stderr)
            need(result.returncode == 0 and not result.stderr.strip(), "daily explicit zero proof blocked/unreadable")
            receipt = validate(json.loads(result.stdout), since, datetime.now(UTC))
            exclusive(destination / "retry-zero-coverage.json", canonical(receipt))
            def invoke():
                try:
                    result = subprocess.run(["bash", str(GATE)], capture_output=True, timeout=240)
                    return result.returncode, result.stdout + result.stderr, datetime.now(UTC)
                except subprocess.TimeoutExpired as exc:
                    return 124, (exc.stdout or b"") + (exc.stderr or b""), datetime.now(UTC)
            def report(day):
                path = Path("/home/trader/known_defect_regression_watch") / ("v2-restart-evidence-" + day + ".md")
                need(path.stat().st_size <= 3_000_000, "report exceeds bound")
                return path.read_text(), path.stat().st_mtime
            rc, receipt, output = run_checks(now, US_MARKET_HOLIDAYS, invoke, report)
            exclusive(destination / "raw.txt", output)
            receipt.update(report_path=str(Path("/home/trader/known_defect_regression_watch") /
                                          ("v2-restart-evidence-" + now.astimezone(ET).strftime("%Y%m%d") + ".md")),
                           retry_zero_process_proof="explicit2/2",
                           catalog_total=verify_runtime()["catalog_counts"]["total"])
            exclusive(destination / "complete.json", canonical(receipt))
            return rc
        except Exception as exc:
            exclusive(destination / "error.json", canonical(dict(verdict="UNKNOWN", error_type=type(exc).__name__)))
            return 2


def notify():
    # Gate hash failure must still notify; the adapter and isolated notifier remain pinned.
    need(os.geteuid() == 0 and digest((REPO / "ops/health/preopen_alert.sh").read_bytes()) == ADAPTER,
         "notification adapter not the reviewed literal source")
    # No direct notification in run(): only OnFailure invokes this adapter once.
    try:
        info = json.loads((ROOT / "latest.json").read_bytes()) if (ROOT / "latest.json").exists() else {}
    except Exception:
        info = {}
    run_path = Path(info.get("run", str(ROOT)))
    if run_path != ROOT and run_path.parent != ROOT / "runs":
        run_path = ROOT
    unit = subprocess.run(["systemctl", "show", "project-mai-tai-preopen.service", "-p", "Result", "-p", "ExecMainStatus", "-p", "ExecMainStartTimestamp"],
                          capture_output=True, text=True, timeout=10, check=True).stdout
    unit_fields = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)
    fresh_run = False
    try:
        if run_path != ROOT and (run_path / "started.json").exists():
            stamp = moment(json.loads((run_path / "started.json").read_bytes())["started_at_utc"])
            fresh_run = system_time(unit_fields["ExecMainStartTimestamp"]) <= stamp <= datetime.now(UTC)
    except Exception:
        fresh_run = False
    if not fresh_run:
        run_path = ROOT / "runs" / datetime.now(UTC).strftime("notify-unknown-%Y%m%dT%H%M%S%fZ")
        run_path.mkdir(mode=0o700)
    complete = run_path / "complete.json"
    try:
        result = json.loads(complete.read_bytes()) if complete.exists() else {"verdict": "UNKNOWN timeout/crash/missing completion", "gate_rc": None}
    except Exception:
        result = {"verdict": "UNKNOWN malformed completion", "gate_rc": None}
    attempt = run_path / "notification-started.json"
    exclusive(attempt, canonical(dict(result=result, unit=unit)))
    rc = subprocess.run([str(REPO / "ops/health/preopen_alert.sh"), "ERROR", json.dumps(dict(result=result, unit=unit)),
                         str(run_path)], timeout=35).returncode
    exclusive(run_path / "notification-complete.json", canonical(dict(adapter_rc=rc, delivery_confirmed=rc == 0)))
    return rc


def main():
    try:
        if sys.argv[1:] == ["paper"]:
            verify_runtime()
            print(json.dumps(paper_shape(datetime.now(UTC), state("momentum-paper"), state("option-a-daily-guard"))))
            return 0
        if len(sys.argv) == 3 and sys.argv[1] == "report-date":
            report_stamp(Path(sys.argv[2]).read_text(), datetime.now(UTC))
            return 0
        if sys.argv[1:] == ["run"]:
            return run()
        if sys.argv[1:] == ["notify"]:
            return notify()
        return 2
    except Exception as exc:
        print("UNKNOWN daily monitor: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
