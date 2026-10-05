"""Run the existing 16 strict/coordinator controls, prior32, and new B-review16."""
import os
from pathlib import Path
import subprocess
import sys

root = Path.cwd()
for label, script in (
    ("strict8", "rpg1/check_readback_mutations.py"),
    ("coordinator8", "rpg1/check_handoff_mutations.py"),
    ("original9", "rpgstuck1/check_mutations.py"),
    ("runtime9", "rpg1/check_runtime_mutations.py"),
    ("sequence3", "rpgstuck1/check_sequence_mutations.py"),
    ("startup11", "rpgstuck1/check_startup_mutations.py"),
    ("breview16", "rpgstuck1/check_b_review_mutations.py"),
):
    path = Path(f"/tmp/rpgstuck-b-{label}-mutations.txt")
    with path.open("w") as output:
        run = subprocess.run([sys.executable, f"docs/review-artifacts/{script}"], cwd=root,
            env={**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"}, stdout=output, stderr=subprocess.STDOUT)
    print(f"{label}: rc={run.returncode} {path}", flush=True)
    if run.returncode:
        raise SystemExit(run.returncode)
