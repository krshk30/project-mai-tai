"""Report-only repair: preserve unclassified errors for the official parser."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil

from install import atomic, sha


def main():
    assert os.geteuid() == 0
    root = Path("/home/trader/preopen-daily")
    with (root / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = root / "restart_report.py"
        runtime = root / "runtime.json"
        old = json.loads(runtime.read_bytes())
        assert sha(report) == old["artifacts"]["restart_report.py"] == "a758634167a3d1c68509ba7c308fa56884ff4cb4d3be711b37867d62366feb9b"
        backup = root / datetime.now(timezone.utc).strftime("report-repair-%Y%m%dT%H%M%S%fZ")
        backup.mkdir(mode=0o700)
        helper = root / "upgrade_ack.py"
        assert sha(helper) == old["artifacts"]["upgrade_ack.py"] == "c131995693310e3408ec775c50a044fe66b42721efb295200f2bf082793b4c04"
        for path in (report, runtime, helper):
            shutil.copy2(path, backup / path.name)
        atomic(report, Path(__file__).with_name("restart_report.py").read_bytes(), 0o600)
        atomic(helper, Path(__file__).with_name("upgrade_ack.py").read_bytes(), 0o600)
        old["artifacts"]["restart_report.py"] = sha(report)
        old["artifacts"]["upgrade_ack.py"] = sha(helper)
        atomic(runtime, (json.dumps(old, indent=2, sort_keys=True) + "\n").encode(), 0o600)
        receipt = dict(backup=str(backup), report_sha256=sha(report), helper_sha256=sha(helper), runtime_sha256=sha(runtime), service_actions=[])
        (backup / "installed.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        with Path("/home/trader/fleet_health/deployments-20261007.md").open("a") as stream:
            stream.write("\n" + datetime.now(timezone.utc).isoformat() + " codex REPORT-MECHANICS: collector returned UNKNOWN before writing output: "
                + "orb-schwab untimestamped traceback scope unreadable. Publish dated UNKNOWN with exact original stderr, no waiver; "
                + "NO exception allowance added. receipt=" + str(backup / "installed.json") + " sha256=" + sha(backup / "installed.json") + "\n")
        print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
