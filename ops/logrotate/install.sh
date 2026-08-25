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

target_dir=$(dirname "$TARGET")
target_parent=$(dirname "$target_dir")

# Normalize ownership and mode before validation.  Root logrotate deliberately
# ignores configs owned by non-root users or writable by group/others, while
# still exiting zero.  Validate the exact staged artifact that can be installed,
# never the scp/repository copy whose metadata depends on transport and umask.
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

# Exit zero alone is insufficient: logrotate can ignore an unsafe config and
# still exit zero, so require proof that it parsed our exact log pattern.
if ! debug_output=$("${PRIV[@]}" "$LOGROTATE_BIN" --debug "$tmp" 2>&1); then
  printf '%s\n' "$debug_output" >&2
  exit 1
fi
if ! grep -Fq 'rotating pattern: /var/log/project-mai-tai/*.log' <<<"$debug_output"; then
  printf '%s\n' "$debug_output" >&2
  echo "logrotate did not parse the project-mai-tai log pattern"
  exit 1
fi

if "${PRIV[@]}" test -f "$TARGET" && "${PRIV[@]}" cmp -s "$tmp" "$TARGET"; then
  echo "Logrotate policy already matches the normalized source; no replacement needed."
else
  "${PRIV[@]}" mv -f "$tmp" "$TARGET"
  trap - EXIT
  echo "Installed updated project-mai-tai logrotate policy."
fi

"${PRIV[@]}" "$SYSTEMCTL_BIN" enable --now logrotate.timer >/dev/null
"${PRIV[@]}" "$SYSTEMCTL_BIN" is-enabled --quiet logrotate.timer
"${PRIV[@]}" "$SYSTEMCTL_BIN" is-active --quiet logrotate.timer

if ! "${PRIV[@]}" cmp -s "$SOURCE" "$TARGET"; then
  echo "installed logrotate policy does not match $SOURCE"
  exit 1
fi

echo "Installed 30-day project-mai-tai log retention; logrotate.timer is enabled and active."
