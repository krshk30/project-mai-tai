"""Run retained SLOT/NFQ and new joint controls serially with bounded children."""
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[3]
receipt = root / "docs/review-artifacts/slotclear1/run_receipt.py"
out = Path(__file__).parent / "controls"
commands = [(f"S{i}", "docs/review-artifacts/slotclear1/check_mutations.py", f"S{i}", 0) for i in range(1, 8)]
commands += [(f"F{i}", "docs/review-artifacts/slotclear1/check_sell_mutations.py", f"F{i}", 0) for i in range(1, 12)]
commands += [("T1", "docs/review-artifacts/slotclear1/check_ticket_veto_mutation.py", None, 1)]
commands += [(f"N{i}", "docs/review-artifacts/nfq2/check_mutations.py", f"N{i}", 0) for i in range(1, 18)]
commands += [(f"C{i}", "docs/review-artifacts/nfq2-slotclear-integration/check_joint_mutation.py", f"C{i}", 0) for i in range(1, 4)]
results = []
for name, script, argument, expected in commands:
    folder = out / name
    assert not folder.exists(), folder
    command = [sys.executable, str(receipt), str(root), str(folder), sys.executable, script]
    if argument:
        command.append(argument)
    child = subprocess.run(command, cwd=root, env=dict(os.environ, PYTHONPATH="src:.",
                           PROOF_TIMEOUT_SECONDS="60"), capture_output=True, text=True, timeout=70)
    record = json.loads((folder / "result.json").read_text())
    red = child.returncode == expected and bool(record["failed_names"]) and not record["timed_out"]
    results.append({"name": name, "red": red, "failed_names": record["failed_names"]})
    print(json.dumps(results[-1]), flush=True)
(out / "SUMMARY.json").write_text(json.dumps(results, indent=2) + "\n")
raise SystemExit(0 if all(row["red"] for row in results) else 1)
