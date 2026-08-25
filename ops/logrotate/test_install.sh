#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/logrotate" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$CALLS"
[[ "$1" == "--debug" ]]
grep -q 'rotate 30' "$2"
grep -q '/var/log/project-mai-tai/\*.log' "$2"
echo 'rotating pattern: /var/log/project-mai-tai/*.log  after 1 days'
EOF

cat > "$TMP/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$CALLS"
case "$1" in enable|is-enabled|is-active) exit 0;; *) exit 9;; esac
EOF
chmod +x "$TMP/logrotate" "$TMP/systemctl"

export CALLS="$TMP/calls"
MAI_TAI_LOGROTATE_TARGET="$TMP/installed" \
MAI_TAI_LOGROTATE_BIN="$TMP/logrotate" \
MAI_TAI_SYSTEMCTL_BIN="$TMP/systemctl" \
  bash "$ROOT/ops/logrotate/install.sh" "$ROOT"

cmp "$ROOT/ops/logrotate/project-mai-tai" "$TMP/installed"
grep -q '^--debug ' "$CALLS"
grep -qx 'enable --now logrotate.timer' "$CALLS"
grep -qx 'is-enabled --quiet logrotate.timer' "$CALLS"
grep -qx 'is-active --quiet logrotate.timer' "$CALLS"

cat > "$TMP/logrotate-fail" <<'EOF'
#!/usr/bin/env bash
exit 7
EOF
chmod +x "$TMP/logrotate-fail"
printf 'known-good-policy\n' > "$TMP/preserved"
if MAI_TAI_LOGROTATE_TARGET="$TMP/rejected" \
   MAI_TAI_LOGROTATE_BIN="$TMP/logrotate-fail" \
   MAI_TAI_SYSTEMCTL_BIN="$TMP/systemctl" \
     bash "$ROOT/ops/logrotate/install.sh" "$ROOT"; then
  echo "installer accepted a failed logrotate validation"
  exit 1
fi
if ! MAI_TAI_LOGROTATE_TARGET="$TMP/preserved" \
     MAI_TAI_LOGROTATE_BIN="$TMP/logrotate-fail" \
     MAI_TAI_SYSTEMCTL_BIN="$TMP/systemctl" \
       bash "$ROOT/ops/logrotate/install.sh" "$ROOT"; then
  grep -qx 'known-good-policy' "$TMP/preserved"
else
  echo "installer accepted a failed candidate over an existing policy"
  exit 1
fi

cat > "$TMP/logrotate-ignore" <<'EOF'
#!/usr/bin/env bash
echo 'Ignoring candidate while returning success'
exit 0
EOF
chmod +x "$TMP/logrotate-ignore"
printf 'known-good-policy\n' > "$TMP/ignored-preserved"
if MAI_TAI_LOGROTATE_TARGET="$TMP/ignored-preserved" \
   MAI_TAI_LOGROTATE_BIN="$TMP/logrotate-ignore" \
   MAI_TAI_SYSTEMCTL_BIN="$TMP/systemctl" \
     bash "$ROOT/ops/logrotate/install.sh" "$ROOT"; then
  echo "installer accepted a config logrotate silently ignored"
  exit 1
fi
grep -qx 'known-good-policy' "$TMP/ignored-preserved"

echo "log retention installer: PASS"
