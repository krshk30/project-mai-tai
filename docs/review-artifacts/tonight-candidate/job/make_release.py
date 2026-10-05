"""Generate release JSON from committed blobs; stdout only, never stage a job."""
import argparse
import hashlib
import json
import subprocess

APP = "7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf"
PREFIX = "docs/review-artifacts/tonight-candidate/"
FILES = {name: PREFIX + "job/" + name for name in (
    "run.sh", "actions.py", "census_readonly.py", "proof.py", "redis_checkpoint.py")}
FILES["strict_flat_readonly.py"] = PREFIX + "strict_flat_readonly.py"
FILES["INSTALL_PLAN_2026-10-05.md"] = PREFIX + "INSTALL_PLAN_2026-10-05.md"
SOURCES = ("ops/health/expected_flags_check.py", "ops/health/expected_flags.json",
           "ops/health/expected_numeric.json", "ops/health/v2_restart_evidence.py",
           "ops/health/preopen_restart_evidence.sh", "ops/preflight/preflight_v2_restart.sh",
           "ops/preflight/preflight_oms_restart.sh", "src/project_mai_tai/deploy_preflight.py",
           "sql/migrations/versions/20261005_0022_managed_entry_binding.py")


def blob(ref, path):
    return subprocess.check_output(["git", "show", ref + ":" + path])


def digest(value):
    return hashlib.sha256(value).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    args = parser.parse_args()
    if len(args.plan) != 40 or any(c not in "0123456789abcdef" for c in args.plan):
        raise ValueError("exact committed plan required")
    application = {path: digest(blob(APP, path)) for path in SOURCES}
    artifacts = {name: digest(blob(args.plan, path)) for name, path in FILES.items()}
    result = {"schema_version": 1, "date_et": "2026-10-05", "plan_commit": args.plan,
              "approved_sha": APP, "box_sha": "e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89",
              "helper_sha256": "ce3ab15bf95a5910e9b84b147d1460c9c494bc359fa6facfa80ebf9884e55ab8",
              "artifacts": artifacts, "artifact_sources": FILES, "application_blobs": application,
              "migration_sha256": application[SOURCES[-1]],
              "scope": "v2-strategy-oms-0022-all-four-on-no-recovery"}
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
