"""Generate source hashes and actual named mutation failures for parent review."""
import hashlib
import json
from pathlib import Path
import subprocess


root = Path(__file__).resolve().parents[3]
artifact = Path(__file__).parent
head = "95cfbae86c737b522051f6ff57c5b9e285cf55ef"
baseline = "994f08aee2c35b3809b3c3384e0d8628807dcfb7"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


assert not git("diff", head, "--", "src", "tests", "ops").strip()
paths = git("diff", "--name-only", baseline, head, "--", "src", "tests", "ops").decode().splitlines()
sources = []
for path in paths:
    blob = git("show", f"{head}:{path}")
    assert (root / path).read_bytes() == blob
    sources.append({"path": str(root / path), "sha256": hashlib.sha256(blob).hexdigest()})
mutations = []
for name in [*[f"S{i}" for i in range(1, 8)], *[f"F{i}" for i in range(1, 12)], "T1"]:
    folder = artifact / "rebased-mutations" / name
    receipt = json.loads((folder / "result.json").read_text())
    assert receipt["failed_names"] and receipt["returncode"] == (1 if name == "T1" else 0)
    mutations.append({"control": name, "actual_failed_names": receipt["failed_names"],
                      "head": receipt["head"], "raw_sha256": receipt["output_sha256"],
                      "command": receipt["command"]})
report = {"baseline": baseline, "frozen_candidate": head,
          "frozen_tree": git("rev-parse", f"{head}^{{tree}}").decode().strip(),
          "runtime_equals_d9ac": not git("diff", "d9ac712a", head, "--", "src", "tests", "ops").strip(),
          "sources": sources, "mutations": mutations}
(artifact / "CURRENT_SOURCES_AND_MUTATIONS.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({"source_paths": len(sources), "mutations_red": len(mutations),
                  "runtime_equals_d9ac": report["runtime_equals_d9ac"]}))
