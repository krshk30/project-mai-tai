#!/usr/bin/env bash
# Fast-forward the production checkout without installing, migrating, or restarting.
#
# This path is intentionally narrow. It exists for a reviewed main delta that changes
# documentation/tests plus comments in Python loaded by a running service. A generic
# "git pull without restart" would recreate disk-vs-process ambiguity for behavioral
# code, so the delta is inspected before the checkout moves.
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
BRANCH="${2:-main}"
EXPECTED_SHA="${3:-}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"

readonly -a MANAGED_UNITS=(
  project-mai-tai-control.service
  project-mai-tai-reconciler.service
  project-mai-tai-strategy.service
  project-mai-tai-oms.service
  project-mai-tai-market-data.service
  project-mai-tai-schwab-1m-v2.service
)

could_not_tell() {
  echo "SYNC-ONLY: COULD_NOT_TELL — $*" >&2
  exit 3
}

refuse() {
  echo "SYNC-ONLY: REFUSED — $*" >&2
  exit 1
}

if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: sync_checkout_only.sh <repo_dir> <branch> <expected-40-char-sha>" >&2
  exit 2
fi
if [[ ! -d "$REPO_DIR/.git" ]]; then
  could_not_tell "missing git repository: $REPO_DIR"
fi

snapshot_units() {
  local unit pid started active sub restarts
  for unit in "${MANAGED_UNITS[@]}"; do
    pid="$($SYSTEMCTL_BIN show "$unit" --property MainPID --value)" \
      || could_not_tell "cannot read MainPID for $unit"
    started="$($SYSTEMCTL_BIN show "$unit" --property ExecMainStartTimestamp --value)" \
      || could_not_tell "cannot read start time for $unit"
    active="$($SYSTEMCTL_BIN show "$unit" --property ActiveState --value)" \
      || could_not_tell "cannot read ActiveState for $unit"
    sub="$($SYSTEMCTL_BIN show "$unit" --property SubState --value)" \
      || could_not_tell "cannot read SubState for $unit"
    restarts="$($SYSTEMCTL_BIN show "$unit" --property NRestarts --value)" \
      || could_not_tell "cannot read NRestarts for $unit"
    [[ "$pid" =~ ^[0-9]+$ && -n "$started" && -n "$active" && -n "$sub" && "$restarts" =~ ^[0-9]+$ ]] \
      || could_not_tell "unusable process identity for $unit"
    printf '%s|%s|%s|%s|%s|%s\n' "$unit" "$pid" "$started" "$active" "$sub" "$restarts"
  done
}

cd "$REPO_DIR"
[[ -z "$(git status --porcelain)" ]] || refuse "production checkout has local changes"

CURRENT_SHA="$(git rev-parse HEAD 2>/dev/null)" \
  || could_not_tell "cannot read current checkout SHA"
[[ "$CURRENT_SHA" =~ ^[0-9a-f]{40}$ ]] || could_not_tell "current checkout SHA is malformed"
CURRENT_BRANCH="$(git symbolic-ref --quiet --short HEAD 2>/dev/null)" \
  || could_not_tell "production checkout is detached"
[[ "$CURRENT_BRANCH" == "$BRANCH" ]] \
  || refuse "production checkout is on $CURRENT_BRANCH, expected $BRANCH"
[[ "$(git rev-parse "refs/heads/$BRANCH" 2>/dev/null)" == "$CURRENT_SHA" ]] \
  || could_not_tell "local $BRANCH does not identify the checked-out commit"

BEFORE_UNITS="$(snapshot_units)"

git fetch origin "$BRANCH" || could_not_tell "git fetch origin $BRANCH failed"
REMOTE_SHA="$(git rev-parse "refs/remotes/origin/$BRANCH" 2>/dev/null)" \
  || could_not_tell "cannot resolve fetched origin/$BRANCH"
if [[ "$REMOTE_SHA" != "$EXPECTED_SHA" ]]; then
  could_not_tell "origin/$BRANCH moved: expected $EXPECTED_SHA, fetched $REMOTE_SHA"
fi
git merge-base --is-ancestor "$CURRENT_SHA" "$EXPECTED_SHA" \
  || refuse "target is not a fast-forward descendant of current checkout"

# The verifier prints one parseable census line and exits non-zero on any path
# outside the allowlist or on any behavior-bearing Python AST change.
DELTA_CENSUS="$(python3 - "$CURRENT_SHA" "$EXPECTED_SHA" <<'PY'
import ast
import subprocess
import sys

old, new = sys.argv[1:]


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


paths = [
    item.decode("utf-8")
    for item in git("diff", "--name-only", "-z", old, new).split(b"\0")
    if item
]
docs = tests = python_equal = controls = 0
allowed_controls = {
    ".github/workflows/deploy-service.yml",
    "ops/systemd/sync_checkout_only.sh",
}
for path in paths:
    if path.startswith("docs/"):
        docs += 1
        continue
    if path.startswith("tests/"):
        tests += 1
        continue
    if path in allowed_controls:
        controls += 1
        continue
    if path.startswith("src/") and path.endswith(".py"):
        try:
            before = git("show", f"{old}:{path}").decode("utf-8")
            after = git("show", f"{new}:{path}").decode("utf-8")
            before_ast = ast.dump(ast.parse(before, filename=path), include_attributes=False)
            after_ast = ast.dump(ast.parse(after, filename=path), include_attributes=False)
        except Exception as exc:
            print(f"SYNC-ONLY: COULD_NOT_TELL — cannot compare Python AST for {path}: {exc}", file=sys.stderr)
            raise SystemExit(3)
        if before_ast != after_ast:
            print(f"SYNC-ONLY: REFUSED — behavior-bearing Python change: {path}", file=sys.stderr)
            raise SystemExit(1)
        python_equal += 1
        continue
    print(f"SYNC-ONLY: REFUSED — path is not sync-only-safe: {path}", file=sys.stderr)
    raise SystemExit(1)

print(
    f"changed_files={len(paths)} docs={docs} tests={tests} "
    f"python_ast_equal={python_equal} control_files={controls} runtime_ast_changed=0"
)
PY
)" || exit $?

git checkout "$BRANCH"
git merge --ff-only "$EXPECTED_SHA"
[[ "$(git rev-parse HEAD)" == "$EXPECTED_SHA" ]] \
  || could_not_tell "checkout did not land on the expected SHA"
[[ -z "$(git status --porcelain)" ]] || could_not_tell "checkout became dirty during sync"

AFTER_UNITS="$(snapshot_units)"
if [[ "$AFTER_UNITS" != "$BEFORE_UNITS" ]]; then
  echo "SYNC-ONLY: REFUSED — a managed process identity changed during checkout sync" >&2
  echo "before:" >&2
  printf '%s\n' "$BEFORE_UNITS" >&2
  echo "after:" >&2
  printf '%s\n' "$AFTER_UNITS" >&2
  exit 1
fi

echo "[SYNC-ONLY-OK] from=$CURRENT_SHA to=$EXPECTED_SHA $DELTA_CENSUS restarted_units=0 migrations=0 runtime_install=0"
