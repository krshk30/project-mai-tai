"""Freeze command, tree, UTC boundaries, output and failed node names."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

cwd, receipt, *command = sys.argv[1:]
root = Path(receipt).resolve()
root.mkdir(parents=True, exist_ok=True)
def git(*args):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


record = {"cwd": cwd, "command": command, "head": git("rev-parse", "HEAD"),
          "tree": git("rev-parse", "HEAD^{tree}"), "status_before": git("status", "--short"),
          "python": sys.version, "platform": platform.platform(),
          "pythonpath": os.environ.get("PYTHONPATH"),
          "start_utc": datetime.datetime.now(datetime.UTC).isoformat()}
with (root / "output.txt").open("w") as stream:
    result = subprocess.run(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
record.update(end_utc=datetime.datetime.now(datetime.UTC).isoformat(), returncode=result.returncode)
raw = (root / "output.txt").read_bytes()
record["output_sha256"] = hashlib.sha256(raw).hexdigest()
record["failed_names"] = sorted({line.split(" - ")[0].removeprefix("FAILED ")
                                  for line in raw.decode().splitlines() if line.startswith("FAILED ")})
(root / "result.json").write_text(json.dumps(record, indent=2) + "\n")
print(json.dumps(record, indent=2))
raise SystemExit(result.returncode)
