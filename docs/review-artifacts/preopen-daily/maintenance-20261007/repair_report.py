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
        assert sha(report) == old["artifacts"]["restart_report.py"] == "08f4f41f7a59e54968a9c2ffc798e1043cca53e085ce56a0000751e63f4c05ad"
        backup = root / datetime.now(timezone.utc).strftime("report-repair-%Y%m%dT%H%M%S%fZ")
        backup.mkdir(mode=0o700)
        helper = root / "upgrade_ack.py"
        assert sha(helper) == old["artifacts"]["upgrade_ack.py"] == "01f43d7cfa21270ce774919b848dc73762e485484a3435de4fbf7e48b63e7fe6"
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
            stream.write("\n" + datetime.now(timezone.utc).isoformat() + " codex REPORT-MECHANICS: first corrected gate read checkout clean/flags157PASS; "
                + "classifier refused pre07 LINESRC logs and suppressed report. Repair passes unclassified lines intact to official collector; "
                + "NO exception allowance added. receipt=" + str(backup / "installed.json") + " sha256=" + sha(backup / "installed.json") + "\n")
        print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
