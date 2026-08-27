#!/usr/bin/env bash
set -euo pipefail

REPO_DIR=${1:-/home/trader/project-mai-tai}
SOURCE_DIR="$REPO_DIR/ops/bootstrap/unattended-upgrades"
ETC_DIR=${MAI_TAI_ETC_DIR:-/etc}
LIBEXEC_DIR=${MAI_TAI_LIBEXEC_DIR:-/usr/local/libexec}
SYSTEMCTL_BIN=${MAI_TAI_SYSTEMCTL_BIN:-systemctl}
SYSTEMD_ANALYZE_BIN=${MAI_TAI_SYSTEMD_ANALYZE_BIN:-systemd-analyze}
APT_CONFIG_BIN=${MAI_TAI_APT_CONFIG_BIN:-apt-config}

if [[ ${MAI_TAI_NO_SUDO:-0} == 1 || $EUID == 0 ]]; then
  SUDO=()
  OWNER_ARGS=()
else
  SUDO=(sudo)
  OWNER_ARGS=(-o root -g root)
fi

required=(
  52-project-mai-tai-unattended-upgrades
  apt-daily.timer.override.conf
  apt-daily-upgrade.timer.override.conf
  apt-daily-upgrade.service.notify.conf
  project-mai-tai-unattended-upgrade-notify
)
for file in "${required[@]}"; do
  [[ -f "$SOURCE_DIR/$file" ]] || {
    echo "missing unattended-upgrade policy artifact: $SOURCE_DIR/$file" >&2
    exit 2
  }
done

bash -n "$SOURCE_DIR/project-mai-tai-unattended-upgrade-notify"
"$SYSTEMD_ANALYZE_BIN" calendar '*-*-* 01:30:00 America/New_York' >/dev/null
"$SYSTEMD_ANALYZE_BIN" calendar '*-*-* 02:30:00 America/New_York' >/dev/null

apt_dump=$("$APT_CONFIG_BIN" \
  -c "$SOURCE_DIR/52-project-mai-tai-unattended-upgrades" dump)
for pattern in '^postgresql$' '^postgresql-.*$' '^libpq[0-9]+$' '^openssl$' '^libssl[0-9].*$'; do
  grep -Fq "$pattern" <<<"$apt_dump" || {
    echo "apt did not parse required unattended-upgrade exclusion: $pattern" >&2
    exit 1
  }
done

install_atomic() {
  local source=$1
  local target=$2
  local mode=$3
  local target_dir target_name tmp metadata expected_mode
  target_dir=$(dirname "$target")
  target_name=$(basename "$target")
  expected_mode=${mode#0}
  "${SUDO[@]}" install -d "${OWNER_ARGS[@]}" -m 0755 "$target_dir"
  tmp=$("${SUDO[@]}" mktemp "$target_dir/.${target_name}.XXXXXX")
  "${SUDO[@]}" install "${OWNER_ARGS[@]}" -m "$mode" "$source" "$tmp"
  if [[ -f "$target" ]] && cmp -s "$source" "$target"; then
    metadata=$(stat -c '%a:%u:%g' "$target")
    if [[ ${MAI_TAI_NO_SUDO:-0} == 1 || "$metadata" == "$expected_mode:0:0" ]]; then
      "${SUDO[@]}" rm -f "$tmp"
      return
    fi
  fi
  "${SUDO[@]}" mv -f "$tmp" "$target"
  cmp -s "$source" "$target" || {
    echo "installed unattended-upgrade artifact does not match source: $target" >&2
    exit 1
  }
}

install_atomic \
  "$SOURCE_DIR/52-project-mai-tai-unattended-upgrades" \
  "$ETC_DIR/apt/apt.conf.d/52-project-mai-tai-unattended-upgrades" 0644
install_atomic \
  "$SOURCE_DIR/apt-daily.timer.override.conf" \
  "$ETC_DIR/systemd/system/apt-daily.timer.d/override.conf" 0644
install_atomic \
  "$SOURCE_DIR/apt-daily-upgrade.timer.override.conf" \
  "$ETC_DIR/systemd/system/apt-daily-upgrade.timer.d/override.conf" 0644
install_atomic \
  "$SOURCE_DIR/apt-daily-upgrade.service.notify.conf" \
  "$ETC_DIR/systemd/system/apt-daily-upgrade.service.d/project-mai-tai-notify.conf" 0644
install_atomic \
  "$SOURCE_DIR/project-mai-tai-unattended-upgrade-notify" \
  "$LIBEXEC_DIR/project-mai-tai-unattended-upgrade-notify" 0755

"${SUDO[@]}" "$SYSTEMCTL_BIN" daemon-reload
"${SUDO[@]}" "$SYSTEMCTL_BIN" enable --now apt-daily.timer apt-daily-upgrade.timer

if [[ ${MAI_TAI_SKIP_RUNTIME_VERIFY:-0} != 1 ]]; then
  "${SUDO[@]}" "$SYSTEMCTL_BIN" is-enabled --quiet apt-daily.timer apt-daily-upgrade.timer
  "${SUDO[@]}" "$SYSTEMCTL_BIN" is-active --quiet apt-daily.timer apt-daily-upgrade.timer
  for timer in apt-daily.timer apt-daily-upgrade.timer; do
    properties=$("${SUDO[@]}" "$SYSTEMCTL_BIN" show "$timer" \
      -p RandomizedDelayUSec -p Persistent)
    grep -Fqx 'RandomizedDelayUSec=0' <<<"$properties"
    grep -Fqx 'Persistent=no' <<<"$properties"
  done
fi

echo "unattended-upgrade policy installed: protected runtimes, ntfy change/failure reporting, explicit ET timers"
