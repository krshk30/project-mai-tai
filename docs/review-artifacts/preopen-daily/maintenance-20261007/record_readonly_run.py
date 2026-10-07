"""Journal actual morning gate evidence; never waive a result or act on services."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from install import sha


def main():
    root = Path("/home/trader/preopen-daily")
    latest = json.loads((root / "latest.json").read_bytes())
    run = Path(latest["run"])
    assert run.parent == root / "runs"
    complete = json.loads((run / "complete.json").read_bytes())
    raw = (run / "raw.txt").read_text()
    assert complete["gate_rc"] == 2 and complete["verdict"] == "UNKNOWN"
    assert "Final call: PASS; checked=157/157 mismatches=0 unknown=0" in raw
    assert "PASS: checkout SHA=5b8b4f642bbc3c312be436d0e92adbc22d9e9f95" in raw
    assert "PASS: checkout tree is clean" in raw
    assert "PASS: orb-schwab exact Redis-upgrade restart ACKNOWLEDGED; actual NRestarts=1" in raw
    assert "FAIL:" not in raw and "contains a traceback before any timestamp; its time scope is unknown" in raw
    report = Path(complete["report_path"])
    assert sha(report) == complete["report_sha256"]
    states = {}
    for name in ("oms", "schwab-1m-v2", "strategy", "control", "orb-schwab", "market-data"):
        result = subprocess.run(["systemctl", "show", "project-mai-tai-" + name + ".service",
            "-p", "MainPID", "-p", "NRestarts", "-p", "ExecMainStartTimestamp", "-p", "ActiveState"],
            capture_output=True, text=True, check=True, timeout=10)
        states[name] = dict(line.split("=", 1) for line in result.stdout.splitlines())
    receipt = dict(at_utc=datetime.now(timezone.utc).isoformat(), run=str(run), complete=complete,
        raw_sha256=sha(run / "raw.txt"), report_path=str(report), report_sha256=sha(report),
        gate_sha256=sha(Path("/home/trader/preopen.sh")), actual_states=states,
        verdict="UNKNOWN_UNTIMESTAMPED_ORB_LOG_SCOPE_NOT_WAIVED", catalog="157/157PASS", mechanics_tests="129PASS",
        source_actions=[], trading_service_actions=[], rollback_or_recovery=False)
    path = run / "morning-mechanics-receipt.json"
    with path.open("x") as stream:
        json.dump(receipt, stream, indent=2, sort_keys=True)
        stream.write("\n")
    with Path("/home/trader/fleet_health/deployments-20261007.md").open("a") as stream:
        stream.write("\n" + receipt["at_utc"] + " codex PREOPEN read-only rerun 06:39ET: checkout/clean-tree PASS, "
            + "known ORB automatic restart ACKNOWLEDGED actual NRestarts1; flags157/157PASS, retryzero2/2. "
            + "Dated report now exists; verdict UNKNOWN because orb-schwab.log has traceback before any timestamp. "
            + "NO scope waiver/PASS; not a permissions failure. receipt=" + str(path) + " sha256=" + sha(path)
            + " report=" + str(report) + " report_sha256=" + sha(report)
            + " preopen_sha256=" + receipt["gate_sha256"] + " mechanics129PASS; no trading service action.\n")
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
