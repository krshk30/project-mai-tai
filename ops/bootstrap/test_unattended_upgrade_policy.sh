#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$SYSTEMCTL_CALLS"
EOF
chmod +x "$TMP/systemctl"
export SYSTEMCTL_CALLS="$TMP/systemctl.calls"

MAI_TAI_NO_SUDO=1 \
MAI_TAI_ETC_DIR="$TMP/etc" \
MAI_TAI_LIBEXEC_DIR="$TMP/libexec" \
MAI_TAI_SYSTEMCTL_BIN="$TMP/systemctl" \
MAI_TAI_SKIP_RUNTIME_VERIFY=1 \
  bash "$ROOT/ops/bootstrap/11_install_unattended_upgrade_policy.sh" "$ROOT"

grep -Fq '"^libpq[0-9]+$";' "$TMP/etc/apt/apt.conf.d/52-project-mai-tai-unattended-upgrades"
grep -Fq '"^libssl[0-9].*$";' "$TMP/etc/apt/apt.conf.d/52-project-mai-tai-unattended-upgrades"
grep -Fq '01:30:00 America/New_York' "$TMP/etc/systemd/system/apt-daily.timer.d/override.conf"
grep -Fq '02:30:00 America/New_York' "$TMP/etc/systemd/system/apt-daily-upgrade.timer.d/override.conf"
grep -Fq 'Persistent=false' "$TMP/etc/systemd/system/apt-daily.timer.d/override.conf"
grep -Fq 'Persistent=false' "$TMP/etc/systemd/system/apt-daily-upgrade.timer.d/override.conf"
grep -Fxq 'daemon-reload' "$SYSTEMCTL_CALLS"
grep -Fxq 'enable --now apt-daily.timer apt-daily-upgrade.timer' "$SYSTEMCTL_CALLS"

# Idempotent control: identical, safe artifacts are not replaced.
policy_inode=$(stat -c '%i' "$TMP/etc/apt/apt.conf.d/52-project-mai-tai-unattended-upgrades")
notifier_inode=$(stat -c '%i' "$TMP/libexec/project-mai-tai-unattended-upgrade-notify")
MAI_TAI_NO_SUDO=1 \
MAI_TAI_ETC_DIR="$TMP/etc" \
MAI_TAI_LIBEXEC_DIR="$TMP/libexec" \
MAI_TAI_SYSTEMCTL_BIN="$TMP/systemctl" \
MAI_TAI_SKIP_RUNTIME_VERIFY=1 \
  bash "$ROOT/ops/bootstrap/11_install_unattended_upgrade_policy.sh" "$ROOT"
[[ "$(stat -c '%i' "$TMP/etc/apt/apt.conf.d/52-project-mai-tai-unattended-upgrades")" == "$policy_inode" ]]
[[ "$(stat -c '%i' "$TMP/libexec/project-mai-tai-unattended-upgrade-notify")" == "$notifier_inode" ]]

NOTIFY="$ROOT/ops/bootstrap/unattended-upgrades/project-mai-tai-unattended-upgrade-notify"
cat > "$TMP/dpkg-query" <<'EOF'
#!/usr/bin/env bash
cat "$PACKAGE_STATE"
EOF
cat > "$TMP/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CURL_CALLS"
[[ "${CURL_MUST_FAIL:-0}" != 1 ]]
EOF
chmod +x "$TMP/dpkg-query" "$TMP/curl"
export PACKAGE_STATE="$TMP/packages"
export CURL_CALLS="$TMP/curl.calls"
export MAI_TAI_UNATTENDED_STATE_DIR="$TMP/state"
export MAI_TAI_UNATTENDED_LOG_FILE="$TMP/notify.log"
export MAI_TAI_UNATTENDED_UPGRADE_LOG="$TMP/unattended-upgrades.log"
export MAI_TAI_DPKG_QUERY_BIN="$TMP/dpkg-query"
export MAI_TAI_CURL_BIN="$TMP/curl"
export MAI_TAI_NTFY_URL="https://ntfy.invalid/test-topic"

printf 'libpq5\t1.0\nopenssl\t1.0\n' > "$PACKAGE_STATE"
: > "$MAI_TAI_UNATTENDED_UPGRADE_LOG"
bash "$NOTIFY" begin
printf 'libpq5\t1.1\nopenssl\t1.0\n' > "$PACKAGE_STATE"
bash "$NOTIFY" finish success exited 0
grep -Fq 'INFO mai-tai unattended upgrade changed packages' "$CURL_CALLS"
grep -Fq -- '- libpq5' "$CURL_CALLS"
grep -Fq -- '+ libpq5' "$CURL_CALLS"

# Quiet control: a successful run with no package delta must not page.
: > "$CURL_CALLS"
rm -rf "$MAI_TAI_UNATTENDED_STATE_DIR"
bash "$NOTIFY" begin
bash "$NOTIFY" finish success exited 0
[[ ! -s "$CURL_CALLS" ]]
grep -Fq 'NO_CHANGE' "$MAI_TAI_UNATTENDED_LOG_FILE"

# The vendor apt.systemd.daily wrapper can swallow an unattended-upgrade
# failure. A new ERROR in its own log must still make a nominally-successful
# systemd service page and fail.
rm -rf "$MAI_TAI_UNATTENDED_STATE_DIR"
bash "$NOTIFY" begin
printf '2026-08-27 06:30:01 ERROR unattended-upgrade failed\n' >> "$MAI_TAI_UNATTENDED_UPGRADE_LOG"
if bash "$NOTIFY" finish success exited 0; then
  echo "notifier trusted vendor-wrapper success over an ERROR log delta" >&2
  exit 1
fi
grep -Fq 'RED mai-tai unattended upgrade failed' "$CURL_CALLS"

# Failure polarity: the service failure pages even when package bytes did not move.
rm -rf "$MAI_TAI_UNATTENDED_STATE_DIR"
bash "$NOTIFY" begin
if bash "$NOTIFY" finish exit-code exited 1; then
  echo "notifier accepted a failed unattended-upgrade service" >&2
  exit 1
fi
grep -Fq 'RED mai-tai unattended upgrade failed' "$CURL_CALLS"

# Delivery failure stays loud; it cannot turn a lost page into success.
rm -rf "$MAI_TAI_UNATTENDED_STATE_DIR"
bash "$NOTIFY" begin
export CURL_MUST_FAIL=1
if bash "$NOTIFY" finish exit-code exited 1; then
  echo "notifier swallowed ntfy delivery failure" >&2
  exit 1
fi

echo "unattended-upgrade policy: PASS"
