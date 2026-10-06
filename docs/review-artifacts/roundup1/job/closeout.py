"""One closeout: pinned gate/catalogs, root timer artifacts, timer-only activation."""
from datetime import datetime, time
import json
import os
from pathlib import Path
import re
from daily import exclusive
from gate_patch import build
from release_policy import APP, CHANGED, DAY, ET, NUMERIC_ARTIFACT, PHASES, canonical, digest, need

DAILY = ("daily.py", "release_policy.py", "retry_zero_readonly.py", "daily-run.sh", "daily-notify.sh")
UNITS = ("project-mai-tai-preopen.service", "project-mai-tai-preopen-failure.service", "project-mai-tai-preopen.timer")
CATALOGS = ("expected_flags_check.py", "expected_flags.json", "expected_numeric.json", "v2_restart_evidence.py", "preopen_restart_evidence.sh")
HELPERS = Path("/home/trader/restart_evidence")
ROOT = Path("/home/trader/preopen-daily")
UNIT_DIRECTORY = Path("/etc/systemd/system")
JOURNAL = Path("/home/trader/fleet_health/deployments-20261006.md")


def install_record(effects, owners=None):
    if owners is None:
        owners = getattr(effects, "runtime_owners", CHANGED)
    path = effects.attempt / ("install-record.json" if owners == CHANGED else "application-install-record.json")
    if path.exists():
        return path
    snapshot = json.loads((effects.attempt / "before-restart.json").read_bytes())
    need(set(owners).issubset(snapshot["services"]), "official snapshot lacks scoped owners")
    exclusive(path, canonical(dict(schema_version=1, snapshot_captured_at_utc=snapshot["captured_at_utc"],
        source_journal=str(effects.attempt / "runner-journal.jsonl"),
        service_actions={name: "restarted" if name in owners else "deliberately_untouched" for name in snapshot["services"]})))
    return path


PAPER_FLAGS = {("momentum_paper_enabled", "momentum-paper"),
               ("market_data_subscription_startup_enabled", "momentum-paper")}


def inactive_paper(before, after, now):
    from daily import system_time
    need(before == after, "paper identity/state changed during FLAGGATE")
    need(now >= datetime.combine(datetime.fromisoformat(DAY).date(), time(16), ET),
         "paper exception before dated after-close window")
    need(after["MainPID"] == 0 and after["ActiveState"] == "inactive" and after["SubState"] == "dead"
         and after["Result"] == "success" and after["NRestarts"] == 0
         and after["ExecMainCode"] == 1 and after["ExecMainStatus"] == 0, "paper not clean expected inactive")
    stopped = system_time(after["InactiveEnterTimestamp"]).astimezone(ET)
    need(stopped.date().isoformat() == DAY and stopped.hour == 9 and stopped.minute == 40
         and 0 <= stopped.second <= 30, "paper stop not dated scheduled09:40")


def flag_result(rc, text, catalog, *, paper_before=None, paper_after=None, now=None):
    final = re.findall(r"^Final call: (PASS|FAIL|UNKNOWN); checked=(\d+)/(\d+) mismatches=(\d+) unknown=(\d+)$", text, re.M)
    full = final == [("PASS", "153", "153", "0", "0")] and rc == 0
    paper_only = final == [("UNKNOWN", "151", "153", "0", "2")] and rc == 2
    need(full or paper_only, "FLAGGATE unexpected verdict/population")
    rows = [line for line in text.splitlines() if line.startswith(("PASS ", "REAL FAILURE ", "UNKNOWN "))]
    need(len(rows) == 153, "FLAGGATE population mismatch")
    found = [re.match(r"(?:PASS|UNKNOWN) flag=([^ ]+) service=([^ ]+) ", line) for line in rows]
    need(all(found), "FLAGGATE identities unreadable")
    actual = [(match[1], match[2]) for match in found]
    expected = {(row["name"], service) for row in catalog
                for service in (row["owning_service"], *row.get("also_check_services", []))}
    need(len(set(actual)) == len(actual) and set(actual) == expected, "FLAGGATE identities duplicate/missing/foreign")
    unknown = {identity for identity, row in zip(actual, rows) if row.startswith("UNKNOWN ")}
    if full:
        need(not unknown, "PASS verdict contradicts unknown rows")
    else:
        need(unknown == PAPER_FLAGS and all(row == "UNKNOWN flag=" + name + " service=momentum-paper reason=momentum-paper is not active"
             for (name, service), row in zip(actual, rows) if row.startswith("UNKNOWN ")), "unexpected paper UNKNOWN reason/identity")
        need(paper_before is not None and paper_after is not None and now is not None, "fresh paper inactivity proof absent")
        inactive_paper(paper_before, paper_after, now)
    return dict(original_rc=rc, original_verdict=final[0][0], checked=int(final[0][1]), total=153,
                unknown=int(final[0][4]), coverage="expected-dated-inactive-paper" if paper_only else "all-measured",
                raw_sha256=digest(text.encode()), paper_before=paper_before, paper_after=paper_after)


def install(effects):
    from attended import GATE, PY, REPO
    effects.gates(len(PHASES))
    snapshot, record = effects.attempt / "before-restart.json", install_record(effects)
    candidate = build((effects.attempt / "preopen.before").read_text(), effects.last, str(snapshot), str(record),
                      restart_strategy="strategy" in getattr(effects, "runtime_owners", CHANGED))
    gate_file = effects.attempt / "preopen.candidate.sh"
    exclusive(gate_file, candidate.encode(), 0o700)
    effects.command(["bash", "-n", gate_file])
    from difflib import unified_diff
    diff = "".join(unified_diff((effects.attempt / "preopen.before").read_text().splitlines(True),
                                candidate.splitlines(True), fromfile="preopen.before", tofile="preopen.candidate"))
    exclusive(effects.attempt / "preopen.diff", diff.encode())
    target = HELPERS
    # Bind and back up every existing isolated gate file before replacing any.
    for name in CATALOGS:
        file = target / name
        need(file.is_file() and not file.is_symlink(), "isolated gate path missing/unexpected")
        need(file.read_bytes() == (effects.attempt / (name + ".before")).read_bytes(),
             "isolated gate changed since pre-write backup")
        source = REPO / "ops/health" / name
        need(digest(source.read_bytes()) == effects.release["application_blobs"]["ops/health/" + name], "closeout source drift")
    for name in CATALOGS:
        file = target / name
        raw = (REPO / "ops/health" / name).read_bytes()
        if name == "expected_numeric.json":
            from retry_zero_readonly import catalog
            raw = (effects.job / NUMERIC_ARTIFACT).read_bytes()
            catalog(raw, (REPO / "ops/health" / name).read_bytes())
            exclusive(effects.attempt / "numeric-catalog.diff", "".join(unified_diff(
                (REPO / "ops/health" / name).read_text().splitlines(True), raw.decode().splitlines(True),
                fromfile="golden-source-numeric", tofile="reviewed-isolated-numeric")).encode())
        effects.replace(file, raw, 0o644, file.stat().st_uid, file.stat().st_gid)
    paper_before = effects.fleet()["momentum-paper"]
    need(paper_before == effects.before["momentum-paper"], "untouched paper changed before checker")
    result = effects.command([PY, target / "expected_flags_check.py", "--catalog", target / "expected_flags.json",
                              "--numeric-catalog", target / "expected_numeric.json"], check=False)
    need(not result.stderr.strip(), "FLAGGATE stderr unreadable")
    catalog = json.loads((target / "expected_flags.json").read_bytes())["flags"] + json.loads((target / "expected_numeric.json").read_bytes())["settings"]
    coverage = flag_result(result.returncode, result.stdout.decode(), catalog, paper_before=paper_before,
                           paper_after=effects.fleet()["momentum-paper"], now=effects.now())
    exclusive(effects.attempt / "flaggate-coverage.json", canonical(coverage))
    effects.gates(len(PHASES))
    need(GATE.read_bytes() == (effects.attempt / "preopen.before").read_bytes(), "gate changed during install")
    root = ROOT
    need(not root.exists(), "daily target appeared during install")
    root.mkdir(mode=0o700)
    (root / "runs").mkdir(mode=0o700)
    installed = {}
    for name in DAILY:
        output = {"daily-run.sh": "run.sh", "daily-notify.sh": "notify.sh"}.get(name, name)
        raw = (effects.job / name).read_bytes()
        exclusive(root / output, raw, 0o700 if name.endswith(".sh") else 0o600)
        installed[output] = digest(raw)
    info = GATE.stat()
    pin = dict(approved_sha=APP, gate_uid=info.st_uid, gate_sha256=digest(candidate.encode()), artifacts=installed,
               release_sha256=digest((effects.job / "release.json").read_bytes()),
               adapter_sha256=effects.release["application_blobs"]["ops/health/preopen_alert.sh"],
               evidence_inputs={**{str(target / name): digest((target / name).read_bytes()) for name in CATALOGS},
                                str(snapshot): digest(snapshot.read_bytes()), str(record): digest(record.read_bytes()),
                                str(REPO / "src/project_mai_tai/strategy_core/time_utils.py"):
                                    effects.release["application_blobs"]["src/project_mai_tai/strategy_core/time_utils.py"],
                                **{str(UNIT_DIRECTORY / name): digest((effects.job / name).read_bytes()) for name in UNITS}})
    exclusive(root / "runtime.json", canonical(pin))
    effects.replace(GATE, candidate.encode(), 0o700, info.st_uid, info.st_gid)
    unit_directory = UNIT_DIRECTORY
    for name in UNITS:
        need(not (unit_directory / name).exists(), "preopen unit appeared during closeout")
        exclusive(unit_directory / name, (effects.job / name).read_bytes(), 0o644)
    effects.command(["systemd-analyze", "verify", *[unit_directory / name for name in UNITS]])
    effects.command(["systemd-analyze", "calendar", "Mon..Fri *-*-* 06:20:00 America/New_York"])
    effects.command(["systemctl", "daemon-reload"])
    effects.command(["systemctl", "enable", "--now", "project-mai-tai-preopen.timer"])
    effects.command(["systemctl", "is-enabled", "project-mai-tai-preopen.timer"])
    effects.command(["systemctl", "is-active", "project-mai-tai-preopen.timer"])
    output = effects.command(["systemctl", "show", "project-mai-tai-preopen.timer", "-p", "NextElapseUSecRealtime", "-p", "LastTriggerUSec"]).stdout.decode()
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    need(fields == dict(NextElapseUSecRealtime="Wed 2026-10-07 10:20:00 UTC", LastTriggerUSec=""), "timer NEXT/last trigger differs")
    state = effects.command(["systemctl", "show", "project-mai-tai-preopen.service", "-p", "ExecMainStartTimestamp", "-p", "ActiveState"]).stdout.decode()
    need(dict(line.split("=", 1) for line in state.splitlines() if "=" in line)
         == dict(ExecMainStartTimestamp="", ActiveState="inactive"), "real gate already invoked; no future-date run allowed")
    exclusive(effects.attempt / "daily-installed.json", canonical(dict(runtime=pin, units={name: digest((unit_directory / name).read_bytes()) for name in UNITS},
                                                                      timer_next_utc=fields["NextElapseUSecRealtime"], first_live_rehearsal="UNMEASURED")))
    journal = JOURNAL
    need(journal.is_file(), "deployment journal missing; no implicit creation")
    with journal.open("a") as file:
        file.write("\n" + effects.now().isoformat() + " codex six-item control-display retry-zero application=" + APP + " attempt=" + str(effects.attempt)
                   + " release=" + pin["release_sha256"] + " preopen=" + pin["gate_sha256"]
                   + " flags=" + str(coverage["checked"]) + "/153 raw_verdict=" + coverage["original_verdict"]
                   + " raw_rc=" + str(coverage["original_rc"]) + " unknown=" + str(coverage["unknown"])
                   + " coverage=" + coverage["coverage"] + " next=20261007T0620ET; future live delivery/rehearsal UNMEASURED\n")
        file.flush()
        os.fsync(file.fileno())
