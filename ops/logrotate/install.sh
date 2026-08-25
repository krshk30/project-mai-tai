#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
SOURCE="$REPO_DIR/ops/logrotate/project-mai-tai"
TARGET="${MAI_TAI_LOGROTATE_TARGET:-/etc/logrotate.d/project-mai-tai}"
LOGROTATE_BIN="${MAI_TAI_LOGROTATE_BIN:-/usr/sbin/logrotate}"
SYSTEMCTL_BIN="${MAI_TAI_SYSTEMCTL_BIN:-systemctl}"
PRIV=()

if [[ ! -f "$SOURCE" ]]; then
  echo "missing versioned logrotate policy: $SOURCE"
  exit 1
fi

if [[ -z "${MAI_TAI_LOGROTATE_TARGET:-}" && "$EUID" -ne 0 ]]; then
  PRIV=(sudo)
fi

# Validate the candidate before it can replace the working policy.  Exit zero
# alone is insufficient: logrotate can ignore a non-regular or unsafe config
# and still exit zero, so require proof that it parsed our exact log pattern.
if ! debug_output=$("${PRIV[@]}" "$LOGROTATE_BIN" --debug "$SOURCE" 2>&1); then
  printf '%s\n' "$debug_output" >&2
  exit 1
fi
if ! grep -Fq 'rotating pattern: /var/log/project-mai-tai/*.log' <<<"$debug_output"; then
  printf '%s\n' "$debug_output" >&2
  echo "logrotate did not parse the project-mai-tai log pattern"
  exit 1
fi

target_dir=$(dirname "$TARGET")
# Keep the candidate on the target filesystem for an atomic rename, but outside
# logrotate.d itself.  logrotate reads dotfiles in that directory, so staging
# there briefly creates two configs for the same log pattern.
target_parent=$(dirname "$target_dir")
tmp=$("${PRIV[@]}" mktemp "$target_parent/.project-mai-tai.logrotate.XXXXXX")
cleanup() { "${PRIV[@]}" rm -f "$tmp"; }
trap cleanup EXIT

if [[ -n "${MAI_TAI_LOGROTATE_TARGET:-}" ]]; then
  # Test/non-production seam: never requires root ownership or sudo.
  install -m 0644 "$SOURCE" "$tmp"
else
  "${PRIV[@]}" install -o root -g root -m 0644 "$SOURCE" "$tmp"
fi
"${PRIV[@]}" cmp -s "$SOURCE" "$tmp"
"${PRIV[@]}" mv -f "$tmp" "$TARGET"
trap - EXIT

"${PRIV[@]}" "$SYSTEMCTL_BIN" enable --now logrotate.timer >/dev/null
"${PRIV[@]}" "$SYSTEMCTL_BIN" is-enabled --quiet logrotate.timer
"${PRIV[@]}" "$SYSTEMCTL_BIN" is-active --quiet logrotate.timer

if ! cmp -s "$SOURCE" "$TARGET"; then
  echo "installed logrotate policy does not match $SOURCE"
  exit 1
fi

echo "Installed 30-day project-mai-tai log retention; logrotate.timer is enabled and active."
