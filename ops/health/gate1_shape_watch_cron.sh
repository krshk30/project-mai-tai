#!/bin/bash
# GATE1 SHAPE WATCH — page the moment a qualifying #647 Gate 1 shape exists.
#
# ⭐ WHY. Operator ruling 2026-09-09: Gate 1 is NOT waived, so `oms_v2_rth_edge_bracket_enabled`
# stays off until an unreserved-share preview proves the shape. He took that branch knowing the
# cost - the pre-market bracket hole stays open meanwhile (~107 occurrences and counting).
#
# ⛔ THE QUALIFYING SHAPE IS THE DEFECT ITSELF: a v2-held SCHWAB long in RTH with UNRESERVED shares
# can only exist if it was entered PRE-MARKET and survived to 09:30. An ordinary RTH entry is
# bracketed on entry, so its shares are reserved - which is exactly why SUNE could not qualify.
# The window is LIVE only while that position is open. Miss it and we wait for the next one.
#
# ⛔ READ-ONLY. Places nothing, previews nothing. Gate 1 itself is a `previewOrder` taken by hand.
#
# ⛔ CRON_TZ IS IGNORED ON THIS BOX, so the ET window is enforced HERE, not in the crontab hour
# field. Runs as ROOT from ROOT's crontab: psql needs the postgres peer auth.
#
#   `--selftest`: force the alert path to prove delivery. Does NOT write state.
set -u

SELFTEST=0
[ "${1:-}" = "--selftest" ] && SELFTEST=1

REPO="${GATE1_REPO:-/home/trader/project-mai-tai}"
OUT="${GATE1_OUT:-/home/trader/gate1_watch}"
PYBIN="${GATE1_PYTHON:-$REPO/.venv/bin/python}"
NTFY_URL="${GATE1_NTFY_URL:-https://ntfy.sh/mai-tai-preopen-28806a5a97b7}"
STATE="$OUT/state"                 # <STATUS> <LAST_ALERT_EPOCH>
STATUS_TXT="$OUT/STATUS.txt"
COOLDOWN_SECS=1800                 # 30 min: the shape persists while the position is open
mkdir -p "$OUT"

STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))
ETDOW=$(TZ=America/New_York date '+%u')
TODAY=$(TZ=America/New_York date +%F)
NOW=$(date +%s)

if [ "$SELFTEST" -eq 0 ]; then
  [ "$ETDOW" -gt 5 ] && exit 0
  # 09:30 (570) .. 16:00 (960) ET. Before the open the shape cannot exist yet; after the close it
  # is moot. Guarding here is also automatically DST-correct.
  { [ "$ETMIN" -lt 570 ] || [ "$ETMIN" -ge 960 ]; } && exit 0
  HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
  case "$HOLIDAYS_2026" in *"$TODAY"*) exit 0 ;; esac
fi

REPORT=$("$PYBIN" "$REPO"/ops/health/gate1_shape_watch.py --status "$STATUS_TXT" 2>&1)
RC=$?
case "$RC" in
  0) LEVEL="NONE" ;;
  1) LEVEL="SHAPE" ;;
  *) LEVEL="CANNOT_SEE" ;;   # ⛔ UNKNOWN is not "no shape" — it pages.
esac
echo "$STAMP  $LEVEL (exit $RC)" >> "$OUT/watch.log"

# ⛔ DELIVERY IS VERIFIED, NEVER ASSUMED (the #924 lesson). --fail-with-body: plain `curl -s`
# returns 0 on an HTTP 500, so a dropped page would be recorded as delivered and then suppressed
# for the whole cooldown. Titles must be ASCII - an em-dash silently loses the push.
send_ntfy() {  # $1=title $2=priority $3=tags $4=body -> 0 delivered, non-0 NOT delivered
  curl -sS --fail-with-body --connect-timeout 10 --max-time 30 \
    -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" "$NTFY_URL" \
    >/dev/null 2>>"$OUT/alert.log"
}

PREV_STATUS="NONE"; LAST_ALERT=0
[ -f "$STATE" ] && read -r PREV_STATUS LAST_ALERT < "$STATE" 2>/dev/null || true
case "${LAST_ALERT:-}" in ''|*[!0-9]*) LAST_ALERT=0;; esac

if [ "$LEVEL" != "NONE" ] || [ "$SELFTEST" -eq 1 ]; then
  # ⛔⭐⭐ A TRANSITION BETWEEN DISTINCT ACTIONABLE LEVELS PAGES IMMEDIATELY (codex-2, #931).
  # The cooldown was shared across every non-NONE level, so a delivered CANNOT_SEE page put
  # LAST_ALERT in the future of the cooldown and a REAL shape arriving seconds later was silently
  # suppressed for up to 30 minutes — and the qualifying position can close inside that window.
  # CANNOT_SEE and SHAPE are different instructions; the cooldown may only suppress a REPEAT.
  SHOULD_PAGE=0
  if [ "$SELFTEST" -eq 1 ]; then
    SHOULD_PAGE=1
  elif [ "$LEVEL" != "$PREV_STATUS" ]; then
    SHOULD_PAGE=1
  elif [ $(( NOW - LAST_ALERT )) -ge "$COOLDOWN_SECS" ]; then
    SHOULD_PAGE=1
  fi
  if [ "$SHOULD_PAGE" -eq 1 ]; then
    if [ "$LEVEL" = "NONE" ]; then
      # ⛔⭐⭐ Only reachable via --selftest. A clean selftest must NOT claim a live shape, and must
      # NOT instruct the preview (codex-2, #931 — the same contradictory-selftest class already
      # corrected in #924, which I then rebuilt here). The discriminator is the MEASURED LEVEL,
      # never the flag: a selftest run while a shape IS live still says the shape is live.
      TITLE="SELFTEST Gate1 watch delivery"; PRIORITY="low"; TAGS="white_check_mark"
      BODY="[SELFTEST] DELIVERY-PATH TEST - THIS IS NOT A GATE 1 ALERT.

NO QUALIFYING SHAPE WAS OBSERVED. This message exists only to prove the alarm can reach you.
⛔ DO NOT take the Gate 1 preview on the strength of this page, and it says NOTHING about whether
oms_v2_rth_edge_bracket_enabled can be enabled. A real alert is titled
'Gate1 shape is LIVE - take the preview now'.

$REPORT"
    elif [ "$LEVEL" = "CANNOT_SEE" ]; then
      TITLE="AMBER Gate1 watch CANNOT SEE"; PRIORITY="high"; TAGS="warning"
      BODY="THE WATCH COULD NOT READ THE POSITION STATE. This is NOT a report that no shape exists.
⛔ UNKNOWN is not PASS: while this persists a qualifying Gate 1 window could open and close unseen.

$REPORT"
    else
      TITLE="Gate1 shape is LIVE - take the preview now"; PRIORITY="high"; TAGS="dart"
      BODY="A qualifying #647 Gate 1 shape exists RIGHT NOW: a v2 SCHWAB long, entered pre-market,
still held in regular hours, with NO shares reserved by an open exit.

⛔ THE WINDOW IS OPEN ONLY WHILE THAT POSITION IS. Take the Gate 1 previewOrder against it now.
⛔ PREVIEW ONLY - never a live probe. This unblocks enabling oms_v2_rth_edge_bracket_enabled.

$REPORT"
    fi
    [ "$SELFTEST" -eq 1 ] && BODY="[SELFTEST] $BODY"
    send_ntfy "$TITLE" "$PRIORITY" "$TAGS" "$BODY"
    DELIVERY_RC=$?
    if [ "$DELIVERY_RC" -eq 0 ]; then
      echo "$STAMP  ALERT[$LEVEL] DELIVERED" >> "$OUT/alert.log"
      LAST_ALERT=$NOW
    else
      # ⛔ Do NOT advance the cooldown on an undelivered page: retry on the next run instead of
      # sitting silent while the window closes.
      echo "$STAMP  ALERT[$LEVEL] DELIVERY FAILED (curl exit $DELIVERY_RC) - will retry next run" >> "$OUT/alert.log"
    fi
  fi
  [ "$SELFTEST" -eq 0 ] && echo "$LEVEL $LAST_ALERT" > "$STATE"
else
  echo "NONE $LAST_ALERT" > "$STATE"
fi
exit 0
