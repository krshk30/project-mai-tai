"""Sequential child processes isolate all semantic controls from the full head run."""
import json
import subprocess
import sys
from pathlib import Path

cases = ["ghost_kept", "attempt_ignored", "stale_admitted", "account_coverage_removed",
    "intent_protection_barrier_removed", "broker_holding_ignored", "filled_phase_admitted",
    "refresh_allowed", "causal_buy_segment_omitted", "strict_causal_segment_omitted"]
results = []
for name in cases:
    completed = subprocess.run([sys.executable, str(Path(__file__).with_name("mutations.py")), name],
                               capture_output=True, text=True, timeout=30)
    results.append({"mutation": name, "rc": completed.returncode,
        "result": "RED" if completed.returncode == 1 and " failed," in completed.stdout else "NOT_RED",
        "summary": completed.stdout.splitlines()[-1] if completed.stdout else completed.stderr[-1000:]})
print(json.dumps(results, indent=2))
raise SystemExit(0 if all(r["result"] == "RED" for r in results) else 1)
