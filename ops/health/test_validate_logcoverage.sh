#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# TEST `logcoverage` FROM validate_0813_deploy.sh — the coverage banner is a WATCH, and a watch is
# not trusted until a deliberate break turns it red. Run:  bash ops/health/test_validate_logcoverage.sh
#
# ⛔⭐ THE CONTROL THAT MATTERS IS CASE 3. Anyone "fixing" the intraday false alarm can silence the
# far-edge check entirely and every other case still passes. Case 3 is a known-bad tape: a CLOSED ET
# day whose log ends short really has lost its evening, and the ⛔⛔ banner MUST still fire.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${HERE}/validate_0813_deploy.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Single source of truth: lift the helpers out of the real script rather than copying them, so this
# test cannot silently drift from the thing it is testing.
# ⛔ Pull each function BY NAME, not by a line range: a range breaks the moment someone reorders the
# definitions, and it breaks SILENTLY into an empty lib — which would let every case below pass
# vacuously. That is the same false-clean this script exists to prevent, so the names are asserted.
extract_fn() {  # handles both `f() { ...; }` one-liners and multi-line bodies closed by a bare }
  awk -v n="$1" '
    !inf && index($0, n"() {") == 1 { print; if ($0 ~ /\}[[:space:]]*$/) next; inf=1; next }
    inf { print; if ($0 ~ /^\}$/) inf=0 }' "$SRC"
}
: > "${TMP}/lib.sh"
echo 'LOG_SILENT_WARN_SECS=${LOG_SILENT_WARN_SECS:-1800}' >> "${TMP}/lib.sh"
for fn in verdict verdict_zero verdict_pos _ts_epoch logfiles _readlog logcat logcount logtail logcoverage; do
  extract_fn "$fn" >> "${TMP}/lib.sh"
  grep -q "^${fn}() {" "${TMP}/lib.sh" || { echo "EXTRACTION FAILED — ${fn}() not found in $SRC"; exit 1; }
done
sudo() { "$@"; }                       # the real one needs root; here the fixtures are ours
logreadable() { [ -r "$1" ]; }
# shellcheck disable=SC1090
. "${TMP}/lib.sh"

PASS=0; FAIL=0
mklog() { rm -f "${TMP}"/t.log*; printf '%s\n' "$@" > "${TMP}/t.log"; echo "${TMP}/t.log"; }
# write a ROTATED sibling next to the live file, named exactly as logrotate names them
mkrot()   { local d="$1"; shift; printf '%s\n' "$@" >  "${TMP}/t.log-${d}"; }
mkrotgz() { local d="$1"; shift; printf '%s\n' "$@" | gzip > "${TMP}/t.log-${d}.gz"; }
# ⛔ The window is built in PLAIN UTC here, not via the script's `TZ="America/New_York"` prefix
# form. That extension is a GNU-on-Linux thing: on Git Bash it SILENTLY returns the input unshifted
# (verified). `logcoverage` only ever compares UTC_FROM/UTC_TO as opaque strings, so a plain UTC
# window exercises it identically — and keeping the harness off that call means these cases test the
# function, not the host's date(1). The real script now asserts the shift happened; see WINDOW SANITY.
win() { UTC_FROM="$1 04:00:00"; UTC_TO=$(date -u -d "$1 04:00:00 UTC +86399 seconds" '+%Y-%m-%d %H:%M:%S'); }
ago() { date -u -d "@$(( $(date +%s) - $1 ))" '+%Y-%m-%d %H:%M:%S'; }
# ⛔ `date -d "<time> -6 hours"` parses `-6` as a TIMEZONE OFFSET, not a relative shift. The literal
# `UTC` before the offset is load-bearing — without it these fixtures land a day out.
shift_utc() { date -u -d "$1 UTC $2" '+%Y-%m-%d %H:%M:%S'; }

check() { # check <name> <output> <want-present|want-absent> <needle> ...
  local name="$1" out="$2"; shift 2
  local ok=1 mode needle
  while [ $# -gt 0 ]; do
    mode="$1"; needle="$2"; shift 2
    case "$mode" in
      present) grep -qF -- "$needle" <<<"$out" || { ok=0; echo "    want PRESENT but missing: $needle"; };;
      absent)  grep -qF -- "$needle" <<<"$out" && { ok=0; echo "    want ABSENT but found:  $needle"; };;
    esac
  done
  if [ "$ok" = 1 ]; then echo "  ✓ $name"; PASS=$((PASS+1))
  else echo "  ✗ $name"; echo "$out" | sed 's/^/      | /'; FAIL=$((FAIL+1)); fi
}

ROT="THE ET DAY IS OVER"
PARTIAL="the ET day is still running"
SILENT="HAS BEEN SILENT FOR"
STARTLATE="THE LOG STARTS AFTER THE WINDOW OPENS"
NOTS="NO TIMESTAMPED LINE"

echo "TESTING logcoverage"

# 1. THE REGRESSION: live intraday run against an ACTIVE log. Log latency is not rotation loss.
win "$(TZ=America/New_York date +%F)"
f=$(mklog "${UTC_FROM} start" "$(ago 2) still going")
check "live+active: no rotation banner, says partial" "$(logcoverage "$f")" \
  absent "$ROT" present "$PARTIAL" absent "$SILENT"

# 2. Live run, but the writer went quiet — a different failure, and the one that fakes a clean.
f=$(mklog "${UTC_FROM} start" "$(ago 2700) last gasp")
check "live+silent: partial AND a silence warning" "$(logcoverage "$f")" \
  absent "$ROT" present "$PARTIAL" present "$SILENT"

# 3. ⭐ CONTROL — a CLOSED ET day whose log ends short really did lose its evening.
YDAY=$(date -u -d '2 days ago' +%F)
win "$YDAY"
f=$(mklog "${UTC_FROM} open" "$(shift_utc "$UTC_TO" "-6 hours") cut short")
check "closed day + short log: ROTATION BANNER FIRES" "$(logcoverage "$f")" \
  present "$ROT" absent "$PARTIAL" absent "$SILENT"

# 4. A closed day whose log covers the whole window must stay quiet.
f=$(mklog "${UTC_FROM} open" "${UTC_TO} closed")
check "closed day + full log: quiet" "$(logcoverage "$f")" \
  absent "$ROT" absent "$STARTLATE" absent "$PARTIAL"

# 5. The near edge still works: rotation ate the START of the day.
f=$(mklog "$(shift_utc "$UTC_FROM" "+6 hours") late start" "${UTC_TO} closed")
check "closed day + late start: START banner fires" "$(logcoverage "$f")" present "$STARTLATE"

# 6. A traceback at the tail must not skip the check — the old cut -c1-19 returned junk here.
win "$YDAY"
f=$(mklog "${UTC_FROM} open" "$(shift_utc "$UTC_TO" "-6 hours") cut short" \
          "Traceback (most recent call last):" "  File \"x.py\", line 1" "ValueError: boom")
check "traceback tail: still finds the timestamp, banner fires" "$(logcoverage "$f")" \
  present "$ROT" absent "$NOTS"

# 7. No timestamp anywhere ⇒ UNKNOWN, never a clean pass.
f=$(mklog "Traceback (most recent call last):" "ValueError: boom")
check "no timestamps at all: UNKNOWN, not clean" "$(logcoverage "$f")" \
  present "$NOTS" absent "$ROT" absent "$PARTIAL"


# ── ROTATED SIBLINGS — the whole point of dropping the 20:00 ET deadline ─────────────────────────
# ⛔⭐⭐ Before this, the script read ONLY the live file and told you the day was gone after 20:00 ET.
# It was not gone: logrotate keeps it. A retained day being reported as zero is a false clean.
echo "TESTING rotated-sibling reading"
win "$YDAY"
IN="${UTC_FROM}"                       # a timestamp inside the window
f=$(mklog "$(shift_utc "$UTC_TO" "-1 hours") live-side MARKER")
mkrot    "20260813" "${IN} rotated-plain MARKER"
mkrotgz  "20260812" "$(shift_utc "$UTC_FROM" "+1 hours") rotated-gz MARKER"
n=$(logcount "$f" 'MARKER')
if [ "$n" = "3" ]; then echo "  ✓ counts across live + plain + .gz siblings (n=3)"; PASS=$((PASS+1))
else echo "  ✗ rotated siblings not summed — got n=${n}, want 3"; FAIL=$((FAIL+1)); fi
# a marker OUTSIDE the window must still be excluded even though its file is read
mkrot "20260811" "2001-01-01 00:00:00 ancient MARKER"
n=$(logcount "$f" 'MARKER')
if [ "$n" = "3" ]; then echo "  ✓ out-of-window lines in a sibling are still filtered out"; PASS=$((PASS+1))
else echo "  ✗ window filter leaked across siblings — got n=${n}, want 3"; FAIL=$((FAIL+1)); fi
blind_chk() { if [ "${LOG_BLIND:-x}" = "$2" ]; then echo "  ✓ $1 (LOG_BLIND=$2)"; PASS=$((PASS+1));
              else echo "  ✗ $1 — LOG_BLIND=${LOG_BLIND:-unset}, want $2"; FAIL=$((FAIL+1)); fi; }
# ⭐ THE WIN: the live file alone starts long after the window opens — which used to force blind=1
# and VOID every past-day verdict. With the sibling read, the window is covered end to end.
f=$(mklog "$(shift_utc "$UTC_TO" "-1 hours") live side" "${UTC_TO} through the close")
mkrot "20260813" "${IN} rotated covers the open"
logcoverage "$f" >/dev/null
blind_chk "siblings cover the open ⇒ a past day is readable, not blind" 0
# ⛔ AND THE CONSERVATIVE SIDE: if the union still ends before the window closes we stay blind. With
# every sibling already read there is no way to tell "the writer was quiet" from "the day aged out",
# and this script must fail toward VOID, never toward a zero.
f=$(mklog "$(shift_utc "$UTC_TO" "-1 hours") live only")
mkrot "20260813" "${IN} rotated covers the open"
logcoverage "$f" >/dev/null
blind_chk "union still ends short ⇒ stays blind (fails toward VOID)" 1

# ── LOG_BLIND — logcoverage must publish the blindness it prints ─────────────────────────────────
blind_is() { # blind_is <name> <want 0|1>
  if [ "${LOG_BLIND:-unset}" = "$2" ]; then echo "  ✓ $1 (LOG_BLIND=$2)"; PASS=$((PASS+1))
  else echo "  ✗ $1 — LOG_BLIND=${LOG_BLIND:-unset}, want $2"; FAIL=$((FAIL+1)); fi
}
echo "TESTING LOG_BLIND"
win "$(TZ=America/New_York date +%F)"
logcoverage "$(mklog "${UTC_FROM} start" "$(ago 2) still going")" >/dev/null
blind_is "live+active is NOT blind — the day merely has not happened yet" 0
win "$YDAY"
logcoverage "$(mklog "${UTC_FROM} open" "${UTC_TO} closed")" >/dev/null
blind_is "closed day, full coverage" 0
logcoverage "$(mklog "${UTC_FROM} open" "$(shift_utc "$UTC_TO" "-6 hours") short")" >/dev/null
blind_is "closed day, log ends short" 1
logcoverage "$(mklog "$(shift_utc "$UTC_FROM" "+6 hours") late" "${UTC_TO} closed")" >/dev/null
blind_is "rotation ate the start of the day" 1
logcoverage "$(mklog "no timestamp here")" >/dev/null
blind_is "no timestamped line at all" 1

# ── THE VERDICT GATE — a blind zero must never read as a result ──────────────────────────────────
# ⛔⭐⭐ THIS IS THE POINT OF THE WHOLE CHANGE. Before it, a past-day control run answered
# "UNEXERCISED — no RTH broker rest happened at all" for 2026-08-13, a day that placed 215 of them.
echo "TESTING the verdict gate"
check "blind + UNEXERCISED -> VOID, and the word UNEXERCISED is NOT the verdict" \
  "$(verdict_zero 1 "UNEXERCISED" "no RTH broker rest happened at all")" \
  present "VERDICT: VOID" absent "VERDICT: UNEXERCISED" present "would have read:"
check "blind + a PASS resting on a zero -> VOID" \
  "$(verdict_zero 1 "PASS" "3 attached, 0 failed")" \
  present "VERDICT: VOID" absent "VERDICT: PASS"
check "blind + a FAIL resting on a zero -> VOID (it could be blindness, not badness)" \
  "$(verdict_zero 1 "FAIL" "12 Webull fills and ZERO attaches")" \
  present "VERDICT: VOID" absent "VERDICT: FAIL"
check "NOT blind -> verdict_zero passes straight through" \
  "$(verdict_zero 0 "UNEXERCISED" "no RTH broker rest happened at all")" \
  present "VERDICT: UNEXERCISED" absent "VOID"
# ⛔⭐⭐ A FAILED POPULATION CONTROL MUST VOID EVERY ZERO, even one from a perfectly readable log.
# The log being intact says nothing about whether the QUERY would have found the thing. This gate
# is the entire mechanism behind §0b; without a test it survived a mutation that disabled it.
check "control FAILED -> every zero is VOID even when the log is fine" \
  "$(CONTROL_VOID=1 verdict_zero 0 "UNEXERCISED" "no RTH broker rest happened at all")" \
  present "VERDICT: VOID" present "POPULATION CONTROL FAILED" absent "VERDICT: UNEXERCISED"
check "control PASSED + not blind -> normal verdict" \
  "$(CONTROL_VOID=0 verdict_zero 0 "PASS" "3 mirrors against 3 rests")" \
  present "VERDICT: PASS" absent "VOID"
check "blind + a FAIL resting on a NON-zero -> survives as a LOWER BOUND" \
  "$(verdict_pos 1 "FAIL" "2 positions HELD WITH NO BROKER-SIDE STOP")" \
  present "VERDICT: FAIL" present "LOWER BOUND" absent "VOID"
check "NOT blind -> verdict_pos passes straight through, unannotated" \
  "$(verdict_pos 0 "PASS" "the claim expired 3x")" \
  present "VERDICT: PASS" absent "LOWER BOUND" absent "VOID"

# ── WIRING — every log-derived verdict must go through a gate, none left bare ────────────────────
# ⛔ The helpers existing proves nothing if a call site still uses the plain `verdict`. This caught a
# real half-applied edit: 11 call sites rewritten while the two definitions never landed, which
# `bash -n` reports as perfectly valid.
echo "TESTING wiring"
# ⛔ The exclusions are the verdicts that legitimately rest on DB counts (which do not rotate) or on
# the controls themselves — a control cannot gate on its own outcome without becoming circular.
BARE=$(grep -nE '^  verdict "(PASS|UNEXERCISED)"' "$SRC" | grep -vE 'known-bad day|no Webull buy filled|never tried to close|ZERO reservation rejects|a rest, a fill and a bracket' || true)
if [ -z "$BARE" ]; then echo "  ✓ no log-derived verdict left on the bare helper"; PASS=$((PASS+1))
else echo "  ✗ bare verdict on log-derived data:"; echo "$BARE" | sed 's/^/      /'; FAIL=$((FAIL+1)); fi
for fn in verdict_zero verdict_pos; do
  if grep -q "^${fn}() {" "$SRC" && grep -q "  ${fn} \"" "$SRC"; then
    echo "  ✓ ${fn} is both defined and called"; PASS=$((PASS+1))
  else echo "  ✗ ${fn} defined=$(grep -c "^${fn}() {" "$SRC") called=$(grep -c "  ${fn} \"" "$SRC")"; FAIL=$((FAIL+1)); fi
done

echo
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
