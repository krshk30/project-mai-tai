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
awk '/^LOG_SILENT_WARN_SECS=/,/^}$/' "$SRC" > "${TMP}/lib.sh"
grep -q 'logcoverage()' "${TMP}/lib.sh" || { echo "EXTRACTION FAILED — logcoverage not found in $SRC"; exit 1; }
sudo() { "$@"; }                       # the real one needs root; here the fixtures are ours
logreadable() { [ -r "$1" ]; }
# shellcheck disable=SC1090
. "${TMP}/lib.sh"

PASS=0; FAIL=0
mklog() { printf '%s\n' "$@" > "${TMP}/t.log"; echo "${TMP}/t.log"; }
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
f=$(mklog "$(ago 7200) start" "$(ago 2) still going")
check "live+active: no rotation banner, says partial" "$(logcoverage "$f")" \
  absent "$ROT" present "$PARTIAL" absent "$SILENT"

# 2. Live run, but the writer went quiet — a different failure, and the one that fakes a clean.
f=$(mklog "$(ago 7200) start" "$(ago 2700) last gasp")
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

echo
echo "PASS=${PASS} FAIL=${FAIL}"
[ "$FAIL" -eq 0 ]
