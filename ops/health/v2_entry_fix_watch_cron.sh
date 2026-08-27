#!/bin/bash
# V2 ENTRY-FIX WATCH -- cron wrapper.
#
# Operator ask (2026-08-04): one watch covering the four open validations of the 08-03 fixes,
# replacing the ad-hoc jobs. The operator is away today, so this must reach the phone on its
# own and leave a readable status file behind for whoever picks it up.
#
# WHAT IT PUSHES (nothing else -- silence is not GREEN, read STATUS for GREEN):
#   1. FIRST cross of the day that produces real-money entries, gradeable or not.
#   2. Any NEW composition breach (>1 first slot, >1 reclaim slot, or 3+ on one cross), or a
#      COULD_NOT_TELL verdict caused by missing economic-slot evidence.
#   3. [OMS-V2-POLL-REENROLL] firing 3+ times -- the leak is live and self-healing masks it.
#   4. P0a: a marketable EH exit resting through a refresh then filling (VALIDATED -- closes the
#      deployed-not-validated state organically), or the KUST churn signature (NOT holding).
#   5. The checker itself failing. A watch that dies quietly reports zero findings, and zero
#      findings reads as health.
#
# ALERTS ARE DECIDED ON CONTENT, never on exit code, and stderr is never swallowed.
#
# Guards are enforced HERE in ET: CRON_TZ is IGNORED on this box, so a crontab hour range is a
# UTC range and cannot express an ET window. Guarding in ET is also DST-correct.
# Runs as ROOT from ROOT's crontab -- the env file and the service logs are root-readable only.
#
#   --selftest : bypass window/holiday/dedupe and FORCE the push path, to prove it lands.
set -u

SELFTEST=0
[ "${1:-}" = "--selftest" ] && SELFTEST=1

OUT=/home/trader/entry_fix_watch
CHECK="$OUT/check.py"
PY=/home/trader/project-mai-tai/.venv/bin/python
LOG="$OUT/watch.log"
STATUS="$OUT/STATUS.txt"          # always the latest full report -- this is where GREEN is read
SEEN="$OUT/seen"                  # breach signatures already pushed
NTFY_URL="https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
mkdir -p "$OUT"; touch "$SEEN"

STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
TODAY=$(TZ=America/New_York date +%F)
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))
ETDOW=$(TZ=America/New_York date '+%u')

if [ "$SELFTEST" -eq 0 ]; then
  [ "$ETDOW" -gt 5 ] && exit 0
  # 07:00 (420) .. 16:15 (975) ET -- the EH window opens at 07:00 and v2 now trades the open.
  { [ "$ETMIN" -lt 420 ] || [ "$ETMIN" -ge 975 ]; } && exit 0
  HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
  case "$HOLIDAYS_2026" in *"$TODAY"*) exit 0 ;; esac
fi

if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 5000000 ]; then
  mv -f "$LOG" "$LOG.1"
fi

push() {  # push <title-ascii> <priority> <body>
  curl -s -m 20 -H "Title: $1" -H "Priority: $2" -d "$3" "$NTFY_URL" >/dev/null \
    || echo "$STAMP  ERROR: ntfy push failed for [$1]" >> "$LOG"
}

REPORT=$("$PY" "$CHECK" --day "$TODAY" 2>"$OUT/stderr.last")
RC=$?
STDERR=$(cat "$OUT/stderr.last" 2>/dev/null)

{ echo "===== $STAMP ====="; echo "$REPORT"
  [ -n "$STDERR" ] && { echo "--- stderr ---"; echo "$STDERR"; }; } >> "$LOG"
{ echo "# V2 ENTRY-FIX WATCH -- last run $STAMP"
  echo "# PASS requires composition=OK with exercised>0; COULD_NOT_TELL and UNEXERCISED are not green."
  echo "# Silence from this watch is NOT green; read the sections below."; echo
  echo "$REPORT"
  [ -n "$STDERR" ] && { echo; echo "--- stderr ---"; echo "$STDERR"; }; } > "$STATUS"

# ---- the checker itself failed -> that is an alert, not a clean ----
if [ "$RC" -ne 0 ] || ! echo "$REPORT" | grep -q "VERDICT composition="; then
  if ! grep -qx "checkfail:$TODAY" "$SEEN"; then
    echo "checkfail:$TODAY" >> "$SEEN"
    push "V2 entry-fix watch BROKEN" "high" \
      "rc=$RC. The watch could not produce a verdict, so it is NOT reporting clean.
$(echo "$STDERR" | tail -5)"
  fi
  exit 0
fi

COMPO=$(echo "$REPORT" | grep -m1 "VERDICT composition=")
REEN=$(echo  "$REPORT" | grep -m1 "VERDICT reenroll=")
DROP=$(echo  "$REPORT" | grep -m1 "VERDICT dropped=")
TAPE=$(echo  "$REPORT" | grep -m1 "VERDICT tape ")
ENTERED=$(echo "$COMPO" | sed -n 's/.*entered=\([0-9]*\).*/\1/p')

# ---- 1. first cross of the day that actually entered ----
if [ "${ENTERED:-0}" -gt 0 ] && ! grep -qx "firstcross:$TODAY" "$SEEN"; then
  echo "firstcross:$TODAY" >> "$SEEN"
  FIRST=$(echo "$REPORT" | grep -m1 -E "^  \[(OK|BREACH|COULD_NOT_TELL|UNEXERCISED)\] " | sed 's/^  //')
  push "V2 first live cross $TODAY" "default" \
    "$FIRST
Legal = at most 1 first slot AND at most 1 reclaim slot. Order style is not the slot.
$COMPO"
fi

# ---- 2. new composition breaches ----
if echo "$COMPO" | grep -q "composition=BREACH"; then
  SIG=$(echo "$COMPO" | sed -n 's/.*breaches=\(.*\)$/\1/p')
  if ! grep -qx "breach:$TODAY:$SIG" "$SEEN"; then
    echo "breach:$TODAY:$SIG" >> "$SEEN"
    push "V2 ENTRY CAP BREACHED" "urgent" \
      "The 08-03 composition cap did not hold on real money.
$SIG
Rollback: gh pr revert 644 -> VPS git pull --ff-only -> restart v2, then stop strategy / restart oms / start strategy."
  fi
fi

# ---- missing economic-slot evidence -> the detector cannot grade composition ----
if echo "$COMPO" | grep -q "composition=COULD_NOT_TELL"; then
  if ! grep -qx "composition-ctt:$TODAY" "$SEEN"; then
    echo "composition-ctt:$TODAY" >> "$SEEN"
    push "V2 entry composition COULD NOT TELL" "high" \
      "$COMPO
At least one counted fill lacks usable cw_entry_slot=first|reclaim evidence. Do not read this
as clean or breached; repair attribution before grading the #644 cap."
  fi
fi

# ---- 3. reenrol firing repeatedly ----
if echo "$REEN" | grep -q "reenroll=REPEATED"; then
  if ! grep -qx "reenroll:$TODAY" "$SEEN"; then
    echo "reenroll:$TODAY" >> "$SEEN"
    push "V2 poll-reenrol REPEATING" "high" \
      "$REEN
One fire proves the mechanism. Repeated fires mean the managed-row leak is LIVE and the
self-healing is masking it -- that is the trigger for a root-cause pass, not a pass mark."
  fi
elif echo "$REEN" | grep -q "reenroll=PROVEN"; then
  if ! grep -qx "reenrolproven:$TODAY" "$SEEN"; then
    echo "reenrolproven:$TODAY" >> "$SEEN"
    push "V2 poll-reenrol FIRED (good)" "default" \
      "$REEN
Fix 2's real path just executed for the first time -- previously unexercised."
  fi
fi

# ---- 4. P0a: the hold engaging (closes it) or churning (it is not holding) ----
P0A=$(echo "$REPORT" | grep -m1 "VERDICT p0a=")
if echo "$P0A" | grep -q "p0a=VALIDATED"; then
  if ! grep -qx "p0avalid:$TODAY" "$SEEN"; then
    echo "p0avalid:$TODAY" >> "$SEEN"
    EV=$(echo "$REPORT" | grep -m1 "P0a VALIDATED" | sed 's/^  //')
    push "P0a VALIDATED (organic)" "high" \
      "A marketable EH software-ladder exit rested through a refresh and FILLED.
$EV
$P0A
This is the condition we could not trigger since 07-31. P0a closes; item 11 loses its
'also validates P0a' dependency."
  fi
elif echo "$P0A" | grep -q "p0a=FAILURE"; then
  if ! grep -qx "p0afail:$TODAY" "$SEEN"; then
    echo "p0afail:$TODAY" >> "$SEEN"
    EV=$(echo "$REPORT" | grep -m1 "\[CHURN\]" | sed 's/^  //')
    push "P0a NOT HOLDING - KUST signature" "urgent" \
      "A managed EH exit was cancelled and replaced at an equal-or-higher limit -- nothing had
moved against us. This is the churn P0a exists to stop.
$EV
$P0A
Kill switch is an APPEND (the flag is absent from the env file, running on code default true):
MAI_TAI_OMS_HOLD_MARKETABLE_MANAGED_EXIT=false + stop strategy / restart oms / start strategy."
  fi
fi

if [ "$SELFTEST" -eq 1 ]; then
  push "V2 entry-fix watch SELFTEST" "default" \
    "Selftest push at $STAMP. Live verdicts right now:
$COMPO
$DROP
$REEN
$TAPE"
  echo "SELFTEST: forced a push. Verdicts:"; echo "$COMPO"; echo "$DROP"; echo "$REEN"; echo "$TAPE"
fi

exit 0
