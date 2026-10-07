"""Run both retained BUY controls and new SELL controls, preserving every subprocess receipt."""
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
results = []
for prefix, maximum, harness in [("S", 7, "check_mutations.py"), ("F", 11, "check_sell_mutations.py")]:
    for number in range(1, maximum + 1):
        name = f"{prefix}{number}"
        folder = root / "sell-mutations" / name
        command = [sys.executable, str(root / "run_receipt.py"), str(Path.cwd()), str(folder),
                   sys.executable, str(root / harness), name]
        result = subprocess.run(command, stdout=subprocess.DEVNULL)
        results.append({"mutation": name, "returncode": result.returncode})
        print(name, result.returncode, flush=True)
(root / "sell-mutations" / "summary.json").write_text(json.dumps(
    {"end_utc": datetime.now(UTC).isoformat(), "results": results}, indent=2) + "\n")
raise SystemExit(int(any(row["returncode"] != 0 for row in results)))
