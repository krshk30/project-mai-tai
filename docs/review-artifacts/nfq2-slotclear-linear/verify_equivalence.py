"""Record exact linear-history and byte-equivalence checks; no runtime writes."""
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[3]
freeze = "5d77d48f213203d23ee2ef6d8ac354655ecc0e83"
base = "9951b473f8203590f48c667770c07e1f9c3096b6"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


head = git("rev-parse", "HEAD")
assert not git("rev-list", "--merges", f"{base}..{head}")
subprocess.run(["git", "merge-base", "--is-ancestor", base, head], cwd=root, check=True)
assert not git("diff", freeze, "--", "src", "tests", "ops")
trees = {name: {"proof": git("rev-parse", f"{freeze}:{name}"),
                "linear": git("rev-parse", f"{head}:{name}")} for name in ("src", "tests", "ops")}
assert all(values["proof"] == values["linear"] for values in trees.values())
record = {"utc": datetime.now(UTC).isoformat(), "head": head,
          "tree": git("rev-parse", f"{head}^{{tree}}"), "base": base,
          "proof_head": freeze, "subtrees": trees, "byte_identical": True,
          "merges_above_base": [], "source_path": str(root / "src"),
          "nfq_marker_mapping": {"0239726f": "81670de1", "16c435b0": "a8519b5d", "0318ef84": "6a6f5005"},
          "scope_diff": git("diff", "--stat", base, head, "--", "src", "tests", "ops")}
(Path(__file__).parent / "EQUIVALENCE.json").write_text(json.dumps(record, indent=2) + "\n")
print(json.dumps(record, indent=2))
