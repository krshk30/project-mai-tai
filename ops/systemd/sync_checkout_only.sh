#!/usr/bin/env bash
# Fast-forward the production checkout without installing, migrating, or restarting.
#
# This path is intentionally narrow. It exists for a reviewed main delta that changes
# documentation/tests plus comments in Python loaded by a running service. A generic
# "git pull without restart" would recreate disk-vs-process ambiguity for behavioral
# code, so the delta is inspected before the checkout moves.
#
# A reviewed per-file declaration, not a computed import graph, is authoritative for
# behavior-bearing files that are deliberately unloaded. Runtime non-reachability is
# not decidable in general (#772 proved that boundary). The live cron/systemd scan is
# only a refuter: a hit vetoes a declaration; silence never grants one.
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
BRANCH="${2:-main}"
EXPECTED_SHA="${3:-}"
DECLARATIONS_FILE="${4:-$REPO_DIR/ops/systemd/sync_only_unloaded_files.tsv}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
CRONTAB_BIN="${CRONTAB_BIN:-crontab}"
SUDO_BIN="${SUDO_BIN:-sudo}"
SYSTEMD_UNIT_DIRS="${SYSTEMD_UNIT_DIRS:-/etc/systemd/system:/run/systemd/system:/usr/local/lib/systemd/system:/usr/lib/systemd/system:/lib/systemd/system}"

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
[[ -r "$DECLARATIONS_FILE" ]] \
  || could_not_tell "reviewed per-file declaration is unreadable: $DECLARATIONS_FILE"

LIVE_REFS="$(mktemp)" || could_not_tell "cannot create live-reference snapshot"
LIVE_REFS_ERR="$(mktemp)" || {
  rm -f "$LIVE_REFS"
  could_not_tell "cannot create live-reference error log"
}
UNIT_FILES="$(mktemp)" || {
  rm -f "$LIVE_REFS" "$LIVE_REFS_ERR"
  could_not_tell "cannot create systemd-unit inventory"
}
cleanup() {
  rm -f "$LIVE_REFS" "$LIVE_REFS_ERR" "$UNIT_FILES"
}
trap cleanup EXIT

snapshot_crontab() {
  local label="$1"
  shift
  : > "$LIVE_REFS_ERR"
  if "$@" >> "$LIVE_REFS" 2> "$LIVE_REFS_ERR"; then
    return 0
  fi
  if grep -qi 'no crontab' "$LIVE_REFS_ERR"; then
    printf '# %s: no crontab\n' "$label" >> "$LIVE_REFS"
    return 0
  fi
  could_not_tell "cannot read $label crontab"
}

snapshot_live_references() {
  local raw_dirs dir unit_count=0
  printf '# current-user crontab\n' > "$LIVE_REFS"
  snapshot_crontab "current-user" "$CRONTAB_BIN" -l
  printf '# root crontab\n' >> "$LIVE_REFS"
  snapshot_crontab "root" "$SUDO_BIN" -n "$CRONTAB_BIN" -u root -l

  : > "$UNIT_FILES"
  IFS=':' read -r -a raw_dirs <<< "$SYSTEMD_UNIT_DIRS"
  for dir in "${raw_dirs[@]}"; do
    [[ -n "$dir" ]] || continue
    [[ -d "$dir" ]] || continue
    find "$dir" -type f -print0 >> "$UNIT_FILES" \
      || could_not_tell "cannot enumerate systemd unit files under $dir"
  done
  while IFS= read -r -d '' unit_file; do
    [[ -r "$unit_file" ]] || could_not_tell "systemd unit file is unreadable: $unit_file"
    printf '\n# systemd unit: %s\n' "$unit_file" >> "$LIVE_REFS"
    cat -- "$unit_file" >> "$LIVE_REFS" \
      || could_not_tell "cannot read systemd unit file: $unit_file"
    unit_count=$((unit_count + 1))
  done < "$UNIT_FILES"
  [[ "$unit_count" -gt 0 ]] || could_not_tell "no readable systemd unit files were found"
}

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
snapshot_live_references

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
DELTA_CENSUS="$(python3 - "$CURRENT_SHA" "$EXPECTED_SHA" "$DECLARATIONS_FILE" "$LIVE_REFS" <<'PY'
import ast
from pathlib import PurePosixPath
from pathlib import Path
import subprocess
import sys

old, new, declarations_file, live_refs_file = sys.argv[1:]


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


paths = [
    item.decode("utf-8")
    for item in git("diff", "--name-only", "-z", old, new).split(b"\0")
    if item
]
docs = tests = python_equal = controls = declared_unloaded = 0
allowed_controls = {
    ".github/workflows/deploy-service.yml",
    "ops/systemd/sync_checkout_only.sh",
    "ops/systemd/sync_only_unloaded_files.tsv",
}


def declarations() -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        provided = Path(declarations_file).read_bytes()
    except (OSError, UnicodeError) as exc:
        print(
            f"SYNC-ONLY: COULD_NOT_TELL — cannot read per-file declarations: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(3)
    try:
        expected = git("show", f"{new}:ops/systemd/sync_only_unloaded_files.tsv")
    except subprocess.CalledProcessError:
        print(
            "SYNC-ONLY: COULD_NOT_TELL — target has no per-file declaration artifact",
            file=sys.stderr,
        )
        raise SystemExit(3)
    if provided != expected:
        print(
            "SYNC-ONLY: COULD_NOT_TELL — provided declarations do not match the target commit",
            file=sys.stderr,
        )
        raise SystemExit(3)
    try:
        lines = provided.decode("utf-8").splitlines()
    except UnicodeError as exc:
        print(
            f"SYNC-ONLY: COULD_NOT_TELL — declarations are not UTF-8: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(3)
    for number, raw in enumerate(lines, 1):
        if not raw or raw.startswith("#"):
            continue
        try:
            path, reason = raw.split("\t", 1)
        except ValueError:
            print(
                f"SYNC-ONLY: COULD_NOT_TELL — malformed declaration line {number}",
                file=sys.stderr,
            )
            raise SystemExit(3)
        normalized = PurePosixPath(path).as_posix()
        if (
            normalized != path
            or path.startswith("/")
            or ".." in PurePosixPath(path).parts
            or not path.endswith(".py")
            or not reason.strip()
        ):
            print(
                f"SYNC-ONLY: COULD_NOT_TELL — unusable declaration line {number}",
                file=sys.stderr,
            )
            raise SystemExit(3)
        if path in found:
            print(
                f"SYNC-ONLY: COULD_NOT_TELL — duplicate declaration for {path}",
                file=sys.stderr,
            )
            raise SystemExit(3)
        try:
            git("cat-file", "-e", f"{new}:{path}")
        except subprocess.CalledProcessError:
            print(
                f"SYNC-ONLY: COULD_NOT_TELL — declared file is absent from target: {path}",
                file=sys.stderr,
            )
            raise SystemExit(3)
        found[path] = reason.strip()
    return found


declared = declarations()
try:
    live_refs = open(live_refs_file, encoding="utf-8", errors="replace").read()
except OSError as exc:
    print(f"SYNC-ONLY: COULD_NOT_TELL — cannot read live references: {exc}", file=sys.stderr)
    raise SystemExit(3)


def refuter_hit(path: str) -> str | None:
    pure = PurePosixPath(path)
    module = path[:-3].replace("/", ".")
    stem = pure.stem
    tokens = (path, pure.name, module, stem, stem.replace("_", "-"))
    return next((token for token in tokens if token and token in live_refs), None)


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
    if path in declared:
        hit = refuter_hit(path)
        if hit is not None:
            print(
                f"SYNC-ONLY: REFUSED — live cron/systemd reference {hit!r} vetoes {path}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        declared_unloaded += 1
        print(
            f"SYNC-ONLY: ALLOW declared-unloaded file={path} "
            f"authority=reviewed-declaration refuter=quiet reason={declared[path]}",
            file=sys.stderr,
        )
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
    f"python_ast_equal={python_equal} control_files={controls} "
    f"declared_unloaded={declared_unloaded} runtime_ast_changed=0"
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
