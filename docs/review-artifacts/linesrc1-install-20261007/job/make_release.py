"""[codex] LOCAL assembly from exact committed bytes and parent baseline metadata."""
import argparse
import json
from pathlib import Path
import re
import subprocess

import release_policy as p
from runner import exclusive, FIELDS

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
REL = "docs/review-artifacts/linesrc1-install-20261007/job/"
ARTIFACTS = ("runner.py", "release_policy.py", "strict_flat_readonly.py", "armed_readonly.py",
             "redis_checkpoint.py", "ticket_inventory.py", "census_readonly.py", "log_ranges.py",
             "flag_admission.py", "continuity_readonly.py", "make_release.py", "run.sh", "README.md",
             "project-mai-tai-linesrc1-20261007.service", "project-mai-tai-linesrc1-20261007.timer")


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=True, timeout=20).stdout


def assemble(plan, baseline, target):
    p.need(re.fullmatch(r"[0-9a-f]{40}", plan), "full committed plan SHA required")
    p.need(set(baseline) == {"fleet_before", "environment_sha256", "authorization_provenance"},
           "parent baseline fields differ; no environment secrets required")
    p.need(re.fullmatch(r"[0-9a-f]{64}", baseline["environment_sha256"]), "actual env SHA256 required")
    p.need(isinstance(baseline["authorization_provenance"], str) and baseline["authorization_provenance"].strip(),
           "same-user standing GO provenance required")
    p.need(set(baseline["fleet_before"]) == set(p.SERVICES)
           and all(set(row) == set(FIELDS) for row in baseline["fleet_before"].values()),
           "parent actual full fleet fields incomplete")
    p.need(git("rev-parse", p.APP + "^{tree}").decode().strip() == p.TREE, "APP tree binding differs")
    git("merge-base", "--is-ancestor", p.APP, plan)
    paths = git("diff", "--name-only", p.APP, plan).decode().splitlines()
    p.need(paths and all(path.startswith(REL) for path in paths), "plan changes outside isolated job")
    raw = {name: git("show", plan + ":" + REL + name) for name in ARTIFACTS}
    p.need(all(value == (HERE / name).read_bytes() for name, value in raw.items()),
           "local files differ from committed reviewed artifact bytes")
    fixture = HERE / "fixtures"
    for path in ("preopen.sh", "preopen-daily/runtime.json", "restart_evidence/expected_numeric.json"):
        p.need((fixture / path).read_bytes() == git("show", plan + ":" + REL + "fixtures/" + path),
               "assembly baseline fixture differs from committed plan: " + path)
    gate = (fixture / "preopen.sh").read_bytes()
    runtime_raw = (fixture / "preopen-daily/runtime.json").read_bytes()
    runtime = json.loads(runtime_raw)
    p.need(p.digest(gate) == p.GATE_SHA and runtime["approved_sha"] == p.BOX, "recorded current baseline differs")
    flags = git("show", p.APP + ":ops/health/expected_flags.json")
    numeric = (fixture / "restart_evidence/expected_numeric.json").read_bytes()
    counts = p.catalog_counts(flags, numeric)
    hashes = {"/home/trader/preopen.sh": p.GATE_SHA,
              "/home/trader/preopen-daily/runtime.json": p.digest(runtime_raw),
              **{"/home/trader/preopen-daily/" + name: sha for name, sha in runtime["artifacts"].items()},
              **runtime["evidence_inputs"],
              "/home/trader/project-mai-tai/ops/health/preopen_alert.sh": runtime["adapter_sha256"]}
    p.need(hashes["/home/trader/restart_evidence/expected_flags.json"] == p.digest(flags)
           and hashes["/home/trader/restart_evidence/expected_numeric.json"] == p.digest(numeric),
           "installed/source catalogs differ")
    target.mkdir(mode=0o700)
    for name, value in raw.items():
        exclusive(target / name, value, 0o700 if name == "run.sh" else 0o600)
    release = dict(application=p.APP, tree=p.TREE, box=p.BOX, date_et=p.DAY, scope=p.SCOPE,
        plan_commit=plan, artifacts={name: p.digest(value) for name, value in raw.items()},
        application_blobs={name: p.digest(git("show", p.APP + ":" + name)) for name in p.SOURCES},
        baseline_hashes=hashes, catalog_hashes=dict(flags=p.digest(flags), numeric=p.digest(numeric)),
        catalog_counts=counts, **baseline)
    release_raw = p.canonical(release)
    sha = p.digest(release_raw)
    exclusive(target / "release.json", release_raw)
    exclusive(target / "release.sha256", (sha + "\n").encode())
    exclusive(target / "approval.pending.json", p.canonical(dict(authority="operator-standing-mechanics-authority",
        decision="APPROVED", application=p.APP, plan_commit=plan, date_et=p.DAY, scope=p.SCOPE, release_sha256=sha)))
    return dict(package=str(target), release_sha256=sha, catalog_counts=counts,
                production_run=False, after16_fleet_baseline_required=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--package", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(assemble(args.plan, json.loads(args.baseline.read_bytes()), args.package), sort_keys=True))


if __name__ == "__main__":
    main()
