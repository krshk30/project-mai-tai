"""Stage committed joint-install artifacts; never fabricate approval or start apps."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

PREFIX = "docs/review-artifacts/rpg1"
JOB = "project-mai-tai-rpg1-coldstart1-install-20261003"
REMOTE_ROOT = "/home/trader/after-hours/2026-10-03/rpg1-coldstart1-job"
PLAN = "JOINT_INSTALL_PLAN_2026-10-03.md"
BOX = "250ab18458f4d806aa8bcdf98d787fff5bb57df4"
RPG_HEAD = "7a9957bfef8a7e0be624bbeb9d04e9d6fd1b3bbf"
RPG_MERGE = "c015c0059526b57b679f019f3a8ce2867a919b34"
COLD_HEAD = "09f1a9ab514ee0b5184edc3b1d50b01eba5e24a4"
FILES = (
    "policy.py", "review_gate.py", "proofs.py", "install.py", "run_install.sh",
    JOB + ".service", JOB + ".timer", "test_joint_install.py", "README.md",
)
START_PIDS = {"market-data": "2907", "oms": "2910", "strategy": "2911",
              "schwab-1m-v2": "2912", "orb": "2913", "momentum-paper": "2914",
              "orb-schwab": "2915", "control": "2916", "market-capture": "2917",
              "reconciler": "2918", "redis-server": "926", "postgresql@16-main": "1037"}
START_HASHES = {
    "/home/trader/preopen.sh": "79d500d82c73271d68c1ff75ab64f9533713e1116d42944baa524d6762faa239",
    "/home/trader/restart_evidence/expected_flags.json": "d15b588540000b18253a8d6cc4d01d9e389a83799d02d219dbdab317da1a1eff",
    "/home/trader/restart_evidence/expected_numeric.json": "934fd2cc177cd7dc900ebc1b85543ae08e3857364cd0a60f99f2d5a5cb2c2084",
    "/home/trader/after-hours/2026-10-05/guard-start-job/start-guard-20261005.sh": "f9ae601b2477813fdc02f31286b8c61c473b3e71386908145ba181931e9b1a81",
    "/etc/project-mai-tai/project-mai-tai.env": "68dfb7e39692da9022657f462a6682b25e25b5777b68a466f4633696ec6ca1a7",
}


def command(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.check_output(args, input=data, timeout=180)


def git(*args: str) -> str:
    return command("git", *args).decode().strip()


def blob(revision: str, path: str) -> bytes:
    return command("git", "show", f"{revision}:{path}")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(ok: object, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def validate_start_host(host: dict) -> None:
    require(set(host["services"]) == set(START_PIDS), "starting service census changed")
    for name, pid in START_PIDS.items():
        row = host["services"][name]
        start = {"redis-server": "Sat 2026-10-03 21:41:33 UTC",
                 "postgresql@16-main": "Sat 2026-10-03 21:41:36 UTC"}.get(
                     name, "Sat 2026-10-03 22:03:17 UTC")
        require(row["MainPID"] == pid and row["ExecMainStartTimestamp"] == start
                and row["NRestarts"] == "0", f"starting identity drift: {name}; review before repin")
    for path, expected in START_HASHES.items():
        require(host["files"][path]["sha256"] == expected, f"starting file drift: {path}")


def merged_review(pr: int, head: str, base: str) -> tuple[str, dict]:
    row = json.loads(command(
        "gh", "pr", "view", str(pr), "--json",
        "state,headRefOid,baseRefOid,mergeCommit,statusCheckRollup",
    ))
    require(row["state"] == "MERGED" and row["headRefOid"] == head,
            f"PR {pr} not merged at the exact reviewed head")
    require(row["baseRefOid"] == base, f"PR {pr} review base changed")
    checks = row["statusCheckRollup"]
    pin = sorted((x for x in checks if x.get("name") == "independent-review-pin"),
                 key=lambda x: x.get("completedAt", ""))
    require(pin and pin[-1].get("conclusion") == "SUCCESS", f"PR {pr} pin not green")
    validate = [x for x in checks if x.get("name") == "validate"]
    require(len(validate) == 2 and all(x.get("conclusion") == "SUCCESS" for x in validate),
            f"PR {pr} needs both exact-head Validate successes")
    path = f"records/{head}/pr-{pr}--{base}--claude-1.json"
    record_commit = git("log", "-1", "--diff-filter=A", "--format=%H", "origin/review-pins", "--", path)
    require(re.fullmatch(r"[0-9a-f]{40}", record_commit), "missing ledger introducing commit")
    raw = blob(record_commit, path)
    require(raw == blob("origin/review-pins", path), "review record changed after its introducing commit")
    record = json.loads(raw)
    require(record["pr_number"] == pr and record["range_head"] == head
            and record["range_base"] == base and record["reviewer"] == "claude-1",
            f"PR {pr} ledger mismatch")
    merge = row["mergeCommit"]["oid"]
    require(git("rev-parse", f"{merge}^{{tree}}") == git("rev-parse", f"{head}^{{tree}}"),
            f"PR {pr} merge tree differs from pinned tree")
    return merge, {"pr": pr, "head": head, "base": base, "record_commit": record_commit,
                   "pin_path": path, "pin_sha256": sha(raw),
                   "ci_urls": [x["detailsUrl"].split("/job/", 1)[0] for x in validate]}


def assemble(approved: str, directory: Path) -> dict:
    require(re.fullmatch(r"[0-9a-f]{40}", approved), "full application SHA required")
    require(not git("status", "--porcelain"), "staging requires a clean committed plan")
    plan_commit = git("rev-parse", "HEAD")
    command("git", "fetch", "origin", "main", "review-pins")
    command("git", "merge-base", "--is-ancestor", approved, "origin/main")
    drift = git("diff", "--name-only", approved, "origin/main").splitlines()
    require(all(x.startswith("docs/") for x in drift), "main non-docs ahead of approved SHA")
    rpg, rpg_review = merged_review(1085, RPG_HEAD, BOX)
    require(rpg == RPG_MERGE, "RPG merge changed")
    cold, cold_review = merged_review(1088, COLD_HEAD, rpg)
    require(cold == approved, "approved SHA must be the COLDSTART merge")
    release = json.loads(blob(plan_commit, f"{PREFIX}/joint_install_job/release.json"))
    require(release["ready"] is False, "committed template must not be pre-approved")
    flags = json.loads(blob(approved, "ops/health/expected_flags.json"))["flags"]
    numeric = json.loads(blob(approved, "ops/health/expected_numeric.json"))["settings"]
    boolean_count = sum(1 + len(x.get("also_check_services", [])) for x in flags)
    numeric_count = sum(1 + len(x.get("also_check_services", [])) for x in numeric)
    require(release["denominators"] == {"boolean": boolean_count, "numeric": numeric_count,
                                         "combined": boolean_count + numeric_count}
            == {"boolean": 132, "numeric": 8, "combined": 140}, "catalog denominator drift")
    for name in ("market_data_subscription_startup_enabled",
                 "strategy_schwab_1m_v2_atr_reprice_handoff_enabled"):
        matches = [x for x in flags if x["name"] == name]
        require(len(matches) == 1 and matches[0]["expected"] is True, "missing enabled flag " + name)
    for name in FILES:
        (directory / name).write_bytes(blob(plan_commit, f"{PREFIX}/joint_install_job/{name}"))
    plan_data = blob(plan_commit, f"{PREFIX}/{PLAN}")
    (directory / PLAN).write_bytes(plan_data)
    source = {path: sha(blob(approved, path)) for path in
              git("ls-tree", "-r", "--name-only", approved, "src/").splitlines()}
    release.update({"ready": True,
                    "reviews": [rpg_review, cold_review],
                    "artifacts": {name: sha((directory / name).read_bytes()) for name in FILES}})
    release["application"] = {
        "box_sha": BOX, "approved_sha": approved,
        "tree": git("rev-parse", f"{approved}^{{tree}}"),
        "rpg_merge_sha": rpg, "coldstart_merge_sha": cold,
        "commits": git("rev-list", "--reverse", f"{BOX}..{approved}").splitlines(),
        "changed_paths": git("diff", "--name-only", BOX, approved).splitlines(),
        "source_sha256": source,
    }
    release["plan"] = {"commit": plan_commit, "path": f"{PREFIX}/{PLAN}", "sha256": sha(plan_data)}
    return release


STAGE = r'''set -euo pipefail
umask 077
SOURCE=$1
JOB=$2
REPO=/home/trader/project-mai-tai
FLEET=(project-mai-tai-{market-data,oms,strategy,schwab-1m-v2,orb,orb-schwab,momentum-paper,control,market-capture,reconciler,tv-alerts}.service redis-server.service postgresql@16-main.service)
BEFORE=$(mktemp)
AFTER=$(mktemp)
systemctl show "${FLEET[@]}" -p Id -p MainPID -p ExecMainStartTimestamp -p InvocationID -p NRestarts -p ActiveState -p SubState > "$BEFORE"
test ! -e "$JOB"
test ! -e /home/trader/after-hours/2026-10-03/rpg1-coldstart1-run
test ! -e /etc/systemd/system/project-mai-tai-rpg1-coldstart1-install-20261003.service
test ! -e /etc/systemd/system/project-mai-tai-rpg1-coldstart1-install-20261003.timer
test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = 250ab18458f4d806aa8bcdf98d787fff5bb57df4
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
"$REPO/.venv/bin/python" -B - "$SOURCE" <<'PY'
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]); release=json.loads((root/'release.json').read_text())
sys.path.insert(0,str(root))
from policy import now,window
from review_gate import validate_release
window(now(),prepare=True)
validate_release(release)
for name,want in release['artifacts'].items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==want,name
assert hashlib.sha256((root/'JOINT_INSTALL_PLAN_2026-10-03.md').read_bytes()).hexdigest()==release['plan']['sha256']
assert not (root/'approval.json').exists()
PY
# Populate Git objects only. The validated refs are full SHAs, never moving branches.
readarray -t COMMITS < <("$REPO/.venv/bin/python" -B -c 'import json,sys; r=json.load(open(sys.argv[1])); print("\n".join(sorted({r["application"]["approved_sha"],r["plan"]["commit"],*(x["record_commit"] for x in r["reviews"])})))' "$SOURCE/release.json")
test "${#COMMITS[@]}" -ge 3
sudo -u trader git -C "$REPO" fetch --no-tags origin "${COMMITS[@]}"
systemd-analyze verify "$SOURCE/"*.service "$SOURCE/"*.timer
install -d -o root -g root -m 0700 "$JOB"
for path in "$SOURCE/"*; do
    test -f "$path"
    install -o root -g root -m 0600 "$path" "$JOB/$(basename "$path")"
done
for path in "$JOB/"*.service "$JOB/"*.timer; do
    install -o root -g root -m 0644 "$path" "/etc/systemd/system/$(basename "$path")"
done
test ! -e "$JOB/approval.json"
systemctl daemon-reload
systemctl enable --now project-mai-tai-rpg1-coldstart1-install-20261003.timer
systemctl cat project-mai-tai-rpg1-coldstart1-install-20261003.service project-mai-tai-rpg1-coldstart1-install-20261003.timer
systemctl list-timers --all project-mai-tai-rpg1-coldstart1-install-20261003.timer --no-pager
sha256sum "$JOB/"*
systemctl show "${FLEET[@]}" -p Id -p MainPID -p ExecMainStartTimestamp -p InvocationID -p NRestarts -p ActiveState -p SubState > "$AFTER"
diff -u "$BEFORE" "$AFTER"
install -o root -g root -m 0600 "$AFTER" "$JOB/staged-service-identities.txt"
test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = 250ab18458f4d806aa8bcdf98d787fff5bb57df4
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
test ! -e "$JOB/approval.json"
test ! -e /home/trader/after-hours/2026-10-03/rpg1-coldstart1-run
printf 'STAGED_ONLY approval absent; no application action\n'
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-sha", required=True)
    parser.add_argument("--host", default="mai-tai-vps")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="joint-review-") as temp:
        local = Path(temp)
        release = assemble(args.approved_sha, local)
        remote = command("ssh", args.host, "mktemp -d /tmp/joint-review.XXXXXXXX").decode().strip()
        require(re.fullmatch(r"/tmp/joint-review\.[A-Za-z0-9]+", remote), "unsafe remote staging path")
        command("scp", *map(str, local.iterdir()), f"{args.host}:{remote}/")
        capture = command("ssh", args.host, "sudo", "/home/trader/project-mai-tai/.venv/bin/python", "-B",
                          remote + "/proofs.py", "capture")
        release["host"] = json.loads(capture)
        validate_start_host(release["host"])
        manifest = json.dumps(release, sort_keys=True, indent=2).encode() + b"\n"
        (local / "release.json").write_bytes(manifest)
        command("scp", str(local / "release.json"), f"{args.host}:{remote}/release.json")
        output = command("ssh", args.host, "sudo", "bash", "-s", "--",
                         shlex.quote(remote), shlex.quote(REMOTE_ROOT), data=STAGE.encode())
        print(output.decode(), end="")
        print("PLAN_COMMIT", release["plan"]["commit"])
        print("APPROVED_SHA", args.approved_sha)
        print("RELEASE_SHA256", sha(manifest))
        print(json.dumps(release["artifacts"], sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
