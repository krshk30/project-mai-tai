"""Deterministic local release from exact committed blobs. Never stages or approves."""
import argparse
import json
from pathlib import Path
import re
import subprocess
from release_policy import (APP, BOX, DAY, ENV_UPDATES, NUMERIC_ARTIFACT, NUMERIC_SHA,
                            RETRY_ENABLED, SCOPE, TREE, digest, need)

PREFIX = "docs/review-artifacts/roundup1/"
NAMES = ("attended.py", "release_policy.py", "armed_readonly.py", "raw_gate_admission.py", "strict_flat_readonly.py", "census_readonly.py",
         "redis_checkpoint.py", "post_proof.py", "closeout.py", "gate_patch.py", "preopen.baseline.sh", "daily.py",
         "daily-run.sh", "daily-notify.sh", "project-mai-tai-preopen.service", "project-mai-tai-preopen.timer",
         "project-mai-tai-preopen-failure.service", "make_release.py", "RELEASE_STATUS_2026-10-06.md",
         "retry_zero_readonly.py", NUMERIC_ARTIFACT, "RETRY_ZERO_RECEIPT_2026-10-06.md", "control_display_proof.py", "make_approval.py")
SOURCES = tuple("ops/health/" + name for name in ("expected_flags_check.py", "expected_flags.json", "expected_numeric.json",
                    "v2_restart_evidence.py", "preopen_restart_evidence.sh", "preopen_alert.sh")) + (
    "ops/preflight/preflight_v2_restart.sh", "ops/preflight/preflight_oms_restart.sh",
    "src/project_mai_tai/deploy_preflight.py", "src/project_mai_tai/settings.py", "src/project_mai_tai/strategy_core/time_utils.py",
    "src/project_mai_tai/services/schwab_1m_v2_bot.py", "src/project_mai_tai/services/orb_schwab_app.py",
    "src/project_mai_tai/oms/service.py", "src/project_mai_tai/oms/store.py", "src/project_mai_tai/oms/atr_reprice_handoff.py",
    "src/project_mai_tai/services/control_plane.py", "ops/systemd/project-mai-tai-control.service",
    "src/project_mai_tai/services/schwab_token_refresher.py", "src/project_mai_tai/broker_adapters/schwab_token_manager.py")


def blob(ref, path):
    return subprocess.check_output(["git", "show", ref + ":" + path], timeout=10)


def generate(plan):
    need(re.fullmatch(r"[0-9a-f]{40}", plan) is not None, "exact full plan commit required")
    actual = subprocess.check_output(["git", "show", "-s", "--format=%T", APP], text=True, timeout=10).strip()
    need(actual == TREE, "candidate whole tree differs")
    paths = {name: PREFIX + "job/" + name for name in NAMES}
    paths["INSTALL_PLAN_2026-10-06.md"] = PREFIX + "INSTALL_PLAN_2026-10-06.md"
    paths["PREOPEN_PLAN_2026-10-06.md"] = "docs/review-artifacts/preopen-daily/PLAN_2026-10-06.md"
    from retry_zero_readonly import catalog
    catalog(blob(plan, paths[NUMERIC_ARTIFACT]), blob(APP, "ops/health/expected_numeric.json"))
    return dict(schema_version=2, approved_sha=APP, tree=TREE, box_sha=BOX, plan_commit=plan, scope=SCOPE, date_et=DAY,
                artifacts={name: digest(blob(plan, path)) for name, path in paths.items()}, artifact_sources=paths,
                application_blobs={path: digest(blob(APP, path)) for path in SOURCES},
                deployment_status="TESTED_STANDING_AUTHORITY_FRESH_RUNTIME_ADMISSION_REQUIRED",
                blocking_acceptance=[],
                runtime_required=["fresh strict flat/exact dated residual admission; bot holdings APUS78/MOBX504/historical132/66 block",
                                  "explicit fresh armed field before/after unchanged v2 gate; read failure blocks",
                                  "row47 optional reset requires fresh old-PID/invocation explicit SIGTERM and CancelledError; ambiguity blocks",
                                  "paper151/153 UNKNOWN2 only exact named rows plus unchanged dated09:40 clean-stop proof",
                                  "explicit stable new v2/OMS retry-enabled=true max-retries=0; repeat before daily checks",
                                  "one control restart with sole token owner, old PID retired, new logs, exact LIVE/SCHWAB JAGX1 page/API"],
                excluded=["RETRYOFF1 superseded enabled-OFF/removal request: withdrawn, not accepted",
                          "#1099 unpinned recovery/MIRROR-counter build", "extra/repeated control restart", "T43", "MIRRORHOLD Step0",
                          "Pause", "CLEARWAIT", "new WEBULL lanes", "Install2 unmerged operator-holdings classifier",
                          "migration", "other application restart"],
                env_updates=ENV_UPDATES, retained_env={RETRY_ENABLED: "true"},
                display=dict(merged_sha=APP, source_in_checkout=True, activation="AUTHORIZED_INSTALL1_UNMEASURED",
                             control_restart=True, restart_count=1, page="/bot/orb", symbol="JAGX", trades=1),
                numeric_catalog=dict(artifact=NUMERIC_ARTIFACT, sha256=NUMERIC_SHA, source_catalog_unchanged=True),
                flaggate=dict(boolean=143, numeric=10, total=153,
                              required="actual153/153 PASS or narrowly proven paper151/153 UNKNOWN2; never relabel UNKNOWN as PASS"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--package", type=Path, help="generate a NEW local directory from committed blobs; never stage remotely")
    args = parser.parse_args()
    manifest = generate(args.plan)
    raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    need(not (args.output and args.package), "choose output or local package")
    if args.output or args.package:
        destination = args.output or args.package
        forbidden = ("/home/trader", "/etc", "/run", "/var", "/private/etc", "/System/Volumes/Data/home/trader")
        need(not any(str(path).startswith(forbidden) for path in (destination.absolute(), destination.resolve())),
             "builder never stages production paths")
    if args.package:
        from daily import exclusive
        args.package.mkdir(mode=0o700)
        for name, path in manifest["artifact_sources"].items():
            exclusive(args.package / name, blob(args.plan, path), 0o700 if name.endswith(".sh") else 0o600)
        exclusive(args.package / "release.json", raw)
        print("LOCAL_PACKAGE=" + str(args.package.resolve()) + " RELEASE_SHA256=" + digest(raw))
        return
    if args.output:
        from daily import exclusive
        exclusive(args.output, raw)
    else:
        print(raw.decode(), end="")


if __name__ == "__main__":
    main()
