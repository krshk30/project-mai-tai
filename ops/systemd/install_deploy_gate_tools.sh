#!/usr/bin/env bash
set -euo pipefail

# Installs the reviewed deploy gate outside the production checkout. This script intentionally
# uses GNU sha256sum; the production target is Linux. It fails closed on macOS.

SOURCE_DIR="${1:-}"
EXPECTED_SHA="${2:-}"
DEST_ROOT="${3:-/home/trader/mai-tai-deploy-gate}"
MANIFEST_REL="ops/systemd/deploy_gate_tools.sha256"
PAYLOAD_PATHS=(
  "ops/preflight/preflight_oms_restart.sh"
  "ops/systemd/deploy_oms_strategy_authorized.sh"
  "ops/systemd/deploy_service.sh"
  "src/project_mai_tai/deploy_preflight.py"
)
EXECUTABLE_PATHS=(
  "ops/preflight/preflight_oms_restart.sh"
  "ops/systemd/deploy_oms_strategy_authorized.sh"
  "ops/systemd/deploy_service.sh"
)

fail() {
  echo "DEPLOY-GATE BOOTSTRAP REFUSED: $1" >&2
  exit 1
}

[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "expected SHA must be a full commit"
[[ -n "$SOURCE_DIR" && -d "$SOURCE_DIR" ]] || fail "source checkout is missing"
SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd -P)"
mkdir -p "$DEST_ROOT/releases"
DEST_ROOT="$(cd "$DEST_ROOT" && pwd -P)"
case "$DEST_ROOT/" in
  "$SOURCE_DIR/"*) fail "tools directory must be outside the source repository" ;;
esac

ACTUAL_SHA="$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || true)"
[[ "$ACTUAL_SHA" == "$EXPECTED_SHA" ]] \
  || fail "source checkout is $ACTUAL_SHA, expected $EXPECTED_SHA"
[[ -z "$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=all)" ]] \
  || fail "source checkout is not clean"

MANIFEST="$SOURCE_DIR/$MANIFEST_REL"
[[ -f "$MANIFEST" ]] || fail "checksum manifest is missing"
EXPECTED_PATH_LIST="$(printf '%s\n' "${PAYLOAD_PATHS[@]}")"
ACTUAL_PATH_LIST="$(awk '{print $2}' "$MANIFEST")"
[[ "$ACTUAL_PATH_LIST" == "$EXPECTED_PATH_LIST" ]] \
  || fail "checksum manifest does not name the exact deploy-gate payload"

for path in "${EXECUTABLE_PATHS[@]}"; do
  mode="$(git -C "$SOURCE_DIR" ls-files -s -- "$path" | awk '{print $1}')"
  [[ "$mode" == "100755" ]] || fail "$path is not committed executable (mode=$mode)"
done

(cd "$SOURCE_DIR" && sha256sum -c "$MANIFEST_REL" >/dev/null) \
  || fail "source checksum verification failed"

RELEASE_DIR="$DEST_ROOT/releases/$EXPECTED_SHA"
if [[ -d "$RELEASE_DIR" ]]; then
  [[ -f "$RELEASE_DIR/DEPLOY_GATE_SOURCE_SHA" ]] \
    || fail "existing release has no source marker"
  [[ "$(cat "$RELEASE_DIR/DEPLOY_GATE_SOURCE_SHA")" == "$EXPECTED_SHA" ]] \
    || fail "existing release source marker does not match"
  cmp -s "$MANIFEST" "$RELEASE_DIR/$MANIFEST_REL" \
    || fail "existing release manifest differs from the approved source manifest"
  (cd "$RELEASE_DIR" && sha256sum -c "$MANIFEST" >/dev/null) \
    || fail "existing release checksum verification failed"
  echo "DEPLOY-GATE TOOLS ALREADY INSTALLED tools_dir=$RELEASE_DIR sha=$EXPECTED_SHA"
  exit 0
fi

STAGING="$(mktemp -d "$DEST_ROOT/releases/.${EXPECTED_SHA}.tmp.XXXXXX")"
cleanup() {
  [[ -n "${STAGING:-}" && -d "$STAGING" ]] && rm -rf -- "$STAGING"
}
trap cleanup EXIT

for path in "${PAYLOAD_PATHS[@]}"; do
  mkdir -p "$STAGING/$(dirname "$path")"
  cp -p "$SOURCE_DIR/$path" "$STAGING/$path"
done
mkdir -p "$STAGING/$(dirname "$MANIFEST_REL")"
cp -p "$MANIFEST" "$STAGING/$MANIFEST_REL"
printf '%s\n' "$EXPECTED_SHA" > "$STAGING/DEPLOY_GATE_SOURCE_SHA"

(cd "$STAGING" && sha256sum -c "$MANIFEST" >/dev/null) \
  || fail "installed checksum verification failed"
for path in "${EXECUTABLE_PATHS[@]}"; do
  [[ -x "$STAGING/$path" ]] || fail "installed $path lost its executable bit"
done

mv "$STAGING" "$RELEASE_DIR"
STAGING=""
trap - EXIT
echo "DEPLOY-GATE TOOLS INSTALLED tools_dir=$RELEASE_DIR sha=$EXPECTED_SHA"
