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
[[ "$(stat -c '%a' "$2")" == "644" ]]
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
export MKTEMP_CALLS="$TMP/mktemp-calls"
mktemp() {
  printf '%s\n' "$1" >> "$MKTEMP_CALLS"
  command mktemp "$@"
}
export -f mktemp
mkdir -p "$TMP/logrotate.d"
MAI_TAI_LOGROTATE_TARGET="$TMP/logrotate.d/installed" \
MAI_TAI_LOGROTATE_BIN="$TMP/logrotate" \
MAI_TAI_SYSTEMCTL_BIN="$TMP/systemctl" \
  bash "$ROOT/ops/logrotate/install.sh" "$ROOT"

cmp "$ROOT/ops/logrotate/project-mai-tai" "$TMP/logrotate.d/installed"
[[ "$(stat -c '%a' "$TMP/logrotate.d/installed")" == "644" ]]
if grep -Fq "$TMP/logrotate.d/.project-mai-tai.logrotate." "$MKTEMP_CALLS"; then
  echo "installer staged a candidate inside the scanned logrotate.d directory"
  exit 1
fi
grep -Fqx "$TMP/.project-mai-tai.logrotate.XXXXXX" "$MKTEMP_CALLS"
grep -q '^--debug ' "$CALLS"
grep -qx 'enable --now logrotate.timer' "$CALLS"
grep -qx 'is-enabled --quiet logrotate.timer' "$CALLS"
grep -qx 'is-active --quiet logrotate.timer' "$CALLS"

# Reproduce the first live reconciliation failure: transport may leave the
# source group-writable.  The installer must validate the normalized 0644
# staged artifact, not the unsafe source metadata.
mkdir -p "$TMP/unsafe-source/ops/logrotate"
cp "$ROOT/ops/logrotate/project-mai-tai" \
  "$TMP/unsafe-source/ops/logrotate/project-mai-tai"
chmod 0664 "$TMP/unsafe-source/ops/logrotate/project-mai-tai"
if [[ "$(stat -c '%a' "$TMP/unsafe-source/ops/logrotate/project-mai-tai")" != "664" ]]; then
  echo "COULD_NOT_TELL: this filesystem cannot represent the unsafe 0664 source control"
  exit 3
fi
MAI_TAI_LOGROTATE_TARGET="$TMP/logrotate.d/from-unsafe-source" \
MAI_TAI_LOGROTATE_BIN="$TMP/logrotate" \
MAI_TAI_SYSTEMCTL_BIN="$TMP/systemctl" \
  bash "$ROOT/ops/logrotate/install.sh" "$TMP/unsafe-source"
cmp "$ROOT/ops/logrotate/project-mai-tai" "$TMP/logrotate.d/from-unsafe-source"
[[ "$(stat -c '%a' "$TMP/logrotate.d/from-unsafe-source")" == "644" ]]

# A scheduled reconciliation runs daily. An already-current policy must still
# stage and validate normalized bytes and verify the timer, but it must not
# replace the target again.
installed_inode=$(stat -c '%i' "$TMP/logrotate.d/installed")
: > "$MKTEMP_CALLS"
MAI_TAI_LOGROTATE_TARGET="$TMP/logrotate.d/installed" \
MAI_TAI_LOGROTATE_BIN="$TMP/logrotate" \
MAI_TAI_SYSTEMCTL_BIN="$TMP/systemctl" \
  bash "$ROOT/ops/logrotate/install.sh" "$ROOT"
grep -Fqx "$TMP/.project-mai-tai.logrotate.XXXXXX" "$MKTEMP_CALLS"
[[ "$(wc -l < "$MKTEMP_CALLS")" -eq 1 ]]
[[ "$(stat -c '%i' "$TMP/logrotate.d/installed")" == "$installed_inode" ]]
cmp "$ROOT/ops/logrotate/project-mai-tai" "$TMP/logrotate.d/installed"

# Correct bytes with unsafe metadata are not current: logrotate can ignore that
# target just as it ignored the transported source in the first live run.
drifted_inode=$(stat -c '%i' "$TMP/logrotate.d/installed")
chmod 0664 "$TMP/logrotate.d/installed"
[[ "$(stat -c '%a' "$TMP/logrotate.d/installed")" == "664" ]]
MAI_TAI_LOGROTATE_TARGET="$TMP/logrotate.d/installed" \
MAI_TAI_LOGROTATE_BIN="$TMP/logrotate" \
MAI_TAI_SYSTEMCTL_BIN="$TMP/systemctl" \
  bash "$ROOT/ops/logrotate/install.sh" "$ROOT"
[[ "$(stat -c '%a' "$TMP/logrotate.d/installed")" == "644" ]]
[[ "$(stat -c '%i' "$TMP/logrotate.d/installed")" != "$drifted_inode" ]]
cmp "$ROOT/ops/logrotate/project-mai-tai" "$TMP/logrotate.d/installed"

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
