"""Local, immutable candidate package from parent's actual merge/combination proof."""
import argparse
import json
from pathlib import Path
import subprocess
from binding import validate_binding
from catalog_policy import numeric_candidate, validate_catalogs
from daily import exclusive
from release_policy import BOX, DAY, ENV_UPDATES, NUMERIC_ARTIFACT, SCOPE, canonical, digest, need

PREFIX = "docs/review-artifacts/install2-20261006/job/"
NAMES = ("attended.py", "release_policy.py", "binding.py", "catalog_policy.py", "cumulative.py", "derive_install1.py",
         "linesrc_disposition.py", "restart_report.py", "post_proof.py", "closeout.py", "gate_patch.py",
         "daily.py", "retry_zero_readonly.py", "control_display_proof.py", "armed_readonly.py",
         "raw_gate_admission.py", "strict_flat_readonly.py", "census_readonly.py", "ticket_inventory.py",
         "redis_checkpoint.py", "preopen.baseline.sh", "daily-run.sh", "daily-notify.sh",
         "project-mai-tai-preopen.service", "project-mai-tai-preopen-failure.service",
         "project-mai-tai-preopen.timer", "make_release.py", "make_approval.py", "README.md")
SOURCES = ("ops/health/expected_flags_check.py", "ops/health/expected_flags.json",
           "ops/health/expected_numeric.json", "ops/health/v2_restart_evidence.py",
           "ops/health/preopen_restart_evidence.sh", "ops/health/preopen_alert.sh",
           "src/project_mai_tai/strategy_core/time_utils.py",
           "src/project_mai_tai/settings.py", "src/project_mai_tai/deploy_preflight.py",
           "src/project_mai_tai/market_data/schwab_v2_rest_client.py",
           "ops/preflight/preflight_v2_restart.sh", "ops/preflight/preflight_oms_restart.sh")


def git(*args):
    return subprocess.check_output(["git", *args], timeout=30)


def blob(ref, path):
    return git("show", ref + ":" + path)


def assemble(plan, binding, proof_raw):
    import re
    need(re.fullmatch(r"[0-9a-f]{40}", plan), "exact committed runner plan required")
    validate_binding(binding)
    app, tree = binding["approved_sha"], binding["tree"]
    need(git("rev-parse", app + "^{tree}").decode().strip() == tree, "actual candidate TREE drift")
    git("merge-base", "--is-ancestor", BOX, app)
    previous = BOX
    for row in binding["merged_prs"]:
        git("merge-base", "--is-ancestor", previous, row["merge_sha"])
        git("merge-base", "--is-ancestor", row["merge_sha"], app)
        previous = row["merge_sha"]
    changed = git("diff", "--name-only", BOX, app).decode().splitlines()
    need(set(changed) == set(binding["reviewed_changed_paths"]), "candidate changed paths outside exact parent allowlist")
    need(digest(proof_raw) == binding["source_combination_proof_sha256"], "source combination receipt hash differs")
    proof = json.loads(proof_raw)
    need(proof.get("verdict") == "VERIFIED" and proof.get("application") == app
         and proof.get("tree") == tree and proof.get("merged_prs") == binding["merged_prs"]
         and proof.get("reviewed_changed_paths") == binding["reviewed_changed_paths"],
         "parent combination receipt not bound to candidate")
    need(proof.get("known_reject_noid_client_abort") == "ACCEPTED"
         and proof.get("mi_nxl_helper_disposition") == "UNCHANGED"
         and proof.get("full_same_environment_pair") == "PASS"
         and proof.get("all_on") == "PASS" and proof.get("mutations") == "PASS"
         and proof.get("ci") == "PASS", "parent source combination acceptance incomplete")
    package = {name: blob(plan, PREFIX + name) for name in NAMES}
    package["binding.json"] = canonical(binding)
    package["source-combination-proof.json"] = proof_raw
    numeric = numeric_candidate(blob(app, "ops/health/expected_numeric.json"))
    package[NUMERIC_ARTIFACT] = numeric
    counts = validate_catalogs(blob(app, "ops/health/expected_flags.json"), numeric, binding)
    sources = set(SOURCES) | {path for path in changed if not path.startswith(("docs/", "tests/"))}
    manifest = dict(schema_version=3, approved_sha=app, tree=tree, box_sha=BOX, plan_commit=plan,
                    date_et=DAY, scope=SCOPE, binding=binding, blocking_acceptance=[],
                    artifacts={name: digest(raw) for name, raw in package.items()},
                    application_blobs={path: digest(blob(app, path)) for path in sorted(sources)},
                    catalog_counts=counts, env_updates=ENV_UPDATES,
                    status="CANDIDATE_BOUND_FRESH_ATTENDED_ADMISSION_REQUIRED")
    return manifest, package


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--source-proof", required=True, type=Path)
    parser.add_argument("--package", required=True, type=Path)
    args = parser.parse_args()
    manifest, package = assemble(args.plan, json.loads(args.binding.read_bytes()), args.source_proof.read_bytes())
    destination = args.package.resolve()
    need(not any(str(destination).startswith(path) for path in
                 ("/home/trader", "/etc", "/private/etc", "/var", "/run")), "local builder cannot stage production")
    destination.mkdir(mode=0o700)
    for name, raw in package.items():
        exclusive(destination / name, raw, 0o700 if name.endswith(".sh") else 0o600)
    raw = canonical(manifest)
    exclusive(destination / "release.json", raw)
    print("LOCAL CANDIDATE release_sha256=" + digest(raw) + " app=" + manifest["approved_sha"])


if __name__ == "__main__":
    main()
