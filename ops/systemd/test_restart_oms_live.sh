#!/usr/bin/env bash
# Controlled-pair harness for the attended OMS restart wrapper. Runs entirely in a temp directory.
set -u

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT

SUBJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$ROOT/ops/systemd" "$ROOT/ops/preflight"
cp "$SUBJECT_DIR/restart_oms_live.sh" "$ROOT/ops/systemd/"

# Keep the subject wrapper exact while replacing unrelated dashboard/systemd helpers with a trace.
cat > "$ROOT/ops/systemd/live_helpers.sh" <<'SH'
confirm_step() {
  local reply
  read -r reply || true
  case "$reply" in y|Y|yes|YES) return 0 ;; *) exit 1 ;; esac
}
print_header() { :; }
print_dashboard_checks() { :; }
print_log_hint() { :; }
stop_unit() { echo "stop $1" >> "$TRACE"; }
restart_unit() { echo "restart $1" >> "$TRACE"; }
start_unit() { echo "start $1" >> "$TRACE"; }
SH

cat > "$ROOT/ops/preflight/preflight_oms_restart.sh" <<'SH'
#!/usr/bin/env bash
echo "preflight rc=${PREFLIGHT_RC:-0}" >> "$TRACE"
exit "${PREFLIGHT_RC:-0}"
SH
chmod +x "$ROOT/ops/preflight/preflight_oms_restart.sh"

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); echo "  PASS $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL $1"; }

run_case() {
  local preflight_rc="$1"
  local trace="$2"
  : > "$trace"
  set +e
  printf 'y\ny\n' | env \
    TRACE="$trace" PREFLIGHT_RC="$preflight_rc" \
    bash "$ROOT/ops/systemd/restart_oms_live.sh" --hold-strategy >/dev/null 2>&1
  local rc=$?
  set -e
  return "$rc"
}

echo "CASE 1 - a preflight refusal must stop before the OMS restart"
TRACE1="$ROOT/refused.trace"
if run_case 1 "$TRACE1"; then
  bad "wrapper returned GO after the preflight refused"
else
  ok "wrapper propagated the preflight refusal"
fi
grep -q '^preflight rc=1$' "$TRACE1" && ok "the tracked preflight ran" || bad "preflight did not run"
if grep -q '^restart project-mai-tai-oms.service$' "$TRACE1"; then
  bad "OMS restart was reached after a preflight refusal"
else
  ok "OMS restart was fenced"
fi

echo "CASE 2 - a preflight GO permits exactly one OMS restart"
TRACE2="$ROOT/go.trace"
if run_case 0 "$TRACE2"; then
  ok "wrapper completed after preflight GO"
else
  bad "wrapper refused a preflight GO"
fi
grep -q '^preflight rc=0$' "$TRACE2" && ok "the GO preflight ran" || bad "GO preflight did not run"
RESTARTS=$(grep -c '^restart project-mai-tai-oms.service$' "$TRACE2" || true)
[[ "$RESTARTS" -eq 1 ]] && ok "one OMS restart ran" || bad "expected one OMS restart, got $RESTARTS"

echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]
