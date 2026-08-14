#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# VALIDATE THE 2026-08-13 DEPLOY (#687 #688 #689 #690 #691 #692 #693) — HEAD 3ac4721
#
# RUN IT:   ssh mai-tai-vps 'bash -s' < ops/health/validate_0813_deploy.sh
#           (⛔ a quoted remote command HANGS on this box — always pipe on stdin)
#           optional:  ... < script.sh -- 2026-08-14      # validate a specific ET day
#
# ⛔⭐⭐ EVERY CHECK REPORTS THREE SEPARATE THINGS, NEVER COLLAPSED INTO ONE:
#       DEPLOYED   — is the code actually in the running tree?
#       EXERCISED  — did the path RUN, and out of how many opportunities? (n / N)
#       VERDICT    — PASS | FAIL | PARTIAL | UNEXERCISED | VOID
#   ⛔ UNEXERCISED and VOID are NOT the same claim. UNEXERCISED = "I looked, it did not run."
#      VOID = "I could not look." A zero read out of a log that does not cover the window is the
#      second one wearing the first one's clothes, so it is reported as VOID.
#   "UNEXERCISED" is a RESULT, not a pass. A bare zero with no denominator is not evidence:
#   an absence only means something against a known population.
#
# ⛔ WHAT THIS SCRIPT CANNOT SEE — state it, do not pretend otherwise:
#   1. The BROKER's book. Webull OCO children are broker-created and never land in `broker_orders`,
#      so "a pair is resting at Webull right now" is UNKNOWABLE here. Only fills/rejects are visible.
#   2. The WIRE. It cannot see the client_order_id actually sent, only what we logged.
#   3. Whether a cancel LANDED. `[OMS-EXIT-RELEASE]` means submitted; confirmation is backgrounded.
#   4. Manual operator activity at either broker (the broker's book is SHARED).
#   5. Only what logrotate still keeps. The rotated siblings ARE read (verified back to 2026-08-08),
#      so the 20:00 ET deadline this script used to impose is GONE — but once a day ages out of
#      retention entirely, it is unrecoverable and the window reports blind.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
set -uo pipefail

DAY_ET="${1:-}"
[ "${DAY_ET}" = "--" ] && DAY_ET="${2:-}"
[ -z "${DAY_ET}" ] && DAY_ET="$(TZ=America/New_York date +%F)"

ENV=/etc/project-mai-tai/project-mai-tai.env
REPO=/home/trader/project-mai-tai
V2LOG=/var/log/project-mai-tai/schwab-1m-v2.log
OMSLOG=/var/log/project-mai-tai/oms.log
# ⛔ journalctl is a FALSE ZERO for v2 — its sink is the file above.

URL=$(sudo grep -E '^MAI_TAI_DATABASE_URL=' "$ENV" | head -1 | cut -d= -f2-)
export PGPASSWORD=$(echo "$URL" | sed -E 's|^[^:]+://[^:]+:([^@]+)@.*|\1|')
PGUSER=$(echo "$URL" | sed -E 's|^[^:]+://([^:]+):.*|\1|')
DSN="dbname=project_mai_tai user=${PGUSER} host=localhost"
D0="'${DAY_ET} 00:00:00'::timestamp at time zone 'America/New_York'"
D1="'${DAY_ET} 23:59:59'::timestamp at time zone 'America/New_York'"

q() { psql "$DSN" -tAF' | ' -v ON_ERROR_STOP=0 -c "$1" 2>&1; }
q1() { psql "$DSN" -tAc "$1" 2>/dev/null | tr -d ' '; }
hdr() { echo; echo "═══ $* "; }
# ⛔⭐⭐ THE §4/§6 DECISIONS LIVE HERE, NOT INLINE, SO A TEST CAN PIN THEM.
# Both shipped a WRONG verdict on 2026-08-14 because the rule sat in an if/elif nothing exercised:
# §4 called bracketed fills "unprotected" (it counted only the re-protect marker, ignoring the 148
# `[V2-OCO-EMIT]` brackets that actually protect), and §6 printed PASS over 9 live failures (it
# counted `[OMS-EXIT-REPROTECT-FAILED]`=0 while the attach it triggers failed under its own marker).
# The operator's Webull screen falsified §4; nothing in this script could have.
# Output contract: "<STATUS>|<zero|pos|db>|<message>" — the caller routes to verdict_zero/_pos/plain.
classify_protection() {   # <barefill> <oco_real> <attached> <attach_failed>
  local bf="${1:-0}" oco="$2" att="$3" af="$4"
  if unknown "$att" "$af" "$oco"; then
    echo "VOID|pos|could not read the OMS log — cannot tell a bracket from a silent failure"; return; fi
  if [ "$bf" -eq 0 ] 2>/dev/null; then
    echo "UNEXERCISED|db|no Webull buy filled — nothing to protect"; return; fi
  if [ "$oco" -eq 0 ] && [ "$att" -eq 0 ]; then
    echo "FAIL|zero|${bf} Webull fills and ZERO brackets from EITHER path — genuinely unprotected"; return; fi
  if [ "$af" -gt 0 ]; then
    echo "FAIL|pos|🔴 ${af} RE-PROTECT failure(s): #691 cancelled a working bracket to close, the close was refused, and #692 could not put it back. THAT is the uncovered window — not a bare fill"; return; fi
  echo "PASS|pos|${oco} brackets placed against ${bf} fills; re-protect ${att} ok / 0 failed"
}
classify_reprotect() {    # <releases> <reprotects> <skipped> <failed> <attach_failed>
  local rel="$1" rp="$2" rps="$3" rpf="$4" af="$5"
  if unknown "$rel" "$rp" "$rps" "$rpf" "$af"; then
    echo "VOID|zero|could not read the OMS log — an uncovered position would be INVISIBLE here"; return; fi
  # ⛔ EITHER marker means a re-protect failed. This section TRIGGERS the Webull attach, and the
  # attach reports its own outcome under its OWN marker — counting only the caller's is a false clean.
  if [ "$rps" -gt 0 ] || [ "$rpf" -gt 0 ] || [ "$af" -gt 0 ]; then
    echo "FAIL|pos|🔴 a released position may be sitting with NO protection — $(( rps + rpf )) via REPROTECT-SKIPPED/FAILED and ${af} via WEBULL-PROTECT-FAILED. Check by hand NOW"; return; fi
  if [ "$rel" -eq 0 ]; then
    echo "UNEXERCISED|zero|no release happened, so no re-protect could be needed"; return; fi
  echo "PASS|zero|${rel} releases, ${rp} needed re-protection, none failed by EITHER marker"
}
say_verdict() {           # <blind> "<STATUS>|<kind>|<msg>"
  local blind="$1" st kind msg rest
  st=${2%%|*}; rest=${2#*|}; kind=${rest%%|*}; msg=${rest#*|}
  case "$kind" in
    zero) verdict_zero "$blind" "$st" "$msg";;
    pos)  verdict_pos  "$blind" "$st" "$msg";;
    *)    verdict "$st" "$msg";;
  esac
}
# ⛔⭐⭐ A TRUNCATED LOG WINDOW POISONS A ZERO, NOT A COUNT.
# When the file does not cover the whole ET day, a log-derived ZERO is indistinguishable from "I
# could not see" — so ANY verdict resting on one becomes VOID, whichever way it was leaning. A
# log-derived NON-ZERO survives: it is a lower bound, and a lower bound of 12 still proves 12.
# ⛔ This is not cosmetic. On any past-day control run the logs have ALWAYS rotated, and §3 answered
#   "UNEXERCISED — no RTH broker rest happened at all"  for 2026-08-13 — a day we KNOW placed 215.
# The truncation banner did say so two lines above, but the VERDICT line did not, and the standing
# instruction on this script is to read the verdict lines only. An unknown had decayed into a result.
# ⛔ A verdict resting on a DB count is NOT poisoned — the DB does not rotate. Section 5 is entirely
# DB-driven, and §4's "no Webull buy filled" reads its denominator from `fills`; both deliberately
# keep the plain `verdict`.
verdict_zero() {  # verdict_zero <blind> <status> <msg>  — the verdict rests on a log ZERO
  if [ "${CONTROL_VOID:-0}" = "1" ]; then
    verdict "VOID" "a §0 POPULATION CONTROL FAILED — this run cannot prove it would see the thing it is counting, so its zero is meaningless. (would have read: ${2} — $3)"
  elif [ "${1:-0}" = "1" ]; then
    verdict "VOID" "cannot say ${2} — the log does not cover this window, so the zero it rests on means 'I could not see', not 'it did not happen'. (would have read: $3)"
  else verdict "$2" "$3"; fi
}
verdict_pos() {   # verdict_pos  <blind> <status> <msg>  — the verdict rests on a log NON-ZERO
  if [ "${1:-0}" = "1" ]; then
    verdict "$2" "$3  ⛔ [log window truncated ⇒ a LOWER BOUND: the finding is real, the magnitude is a floor]"
  else verdict "$2" "$3"; fi
}
verdict() { printf '   ➤ VERDICT: %s — %s\n' "$1" "$2"; }

# ⛔⭐⭐ LOG LINES ARE STAMPED IN **UTC**, AND THE FILE ROTATES AT 00:00 UTC (= 20:00 ET).
# A bare `grep -c` over the logfile counts WHATEVER THE FILE HAPPENS TO HOLD — which is the tail of
# the PREVIOUS ET day plus part of this one. That is a false-clean generator: pass `--day` for a past
# date and every log count silently answers about TODAY's file instead. So every log count below is
# windowed to the UTC range of the requested ET day, and the file's true coverage is printed.
UTC_FROM=$(date -u -d "TZ=\"America/New_York\" ${DAY_ET} 00:00:00" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
UTC_TO=$(date -u -d "TZ=\"America/New_York\" ${DAY_ET} 23:59:59" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
# ⛔⭐⭐ WINDOW SANITY — the `TZ="..."` prefix is a GNU-on-Linux extension. Where it is unsupported
# (Git Bash, busybox) date does NOT error: it returns the string UNSHIFTED, and the window silently
# becomes a UTC day. Every count below would then be windowed to the wrong 24h and read as a clean
# result. ET is UTC-4/-5, so a correct conversion can NEVER leave the time at 00:00:00.
if [ -z "$UTC_FROM" ] || [ -z "$UTC_TO" ] || [ "$UTC_FROM" = "${DAY_ET} 00:00:00" ]; then
  echo "⛔⛔ ABORT: date(1) here does not support the TZ=\"America/New_York\" prefix, so the ET->UTC"
  echo "     window was NOT converted (UTC_FROM='${UTC_FROM}'). Every count would be windowed to the"
  echo "     wrong 24 hours and would read as a clean result. Run this on the VPS via:"
  echo "     ssh mai-tai-vps 'bash -s' < ops/health/validate_0813_deploy.sh"
  exit 2
fi
logreadable() { sudo test -r "$1"; }   # ⛔ the logs are ROOT-ONLY; a bare [ -r ] as trader says NO
# ⛔⭐⭐ THE ROTATED FILES ARE STILL ON THE BOX — THE DAY IS NOT "GONE" AFTER 20:00 ET.
# This script used to read ONLY the live file and told you to run before 20:00 ET or lose the day.
# That was wrong: logrotate keeps `<log>-YYYYMMDD[.gz]` siblings (verified back to 2026-08-08), and
# they hold the markers — `schwab-1m-v2.log-20260814` carries the 215 [V2-RESTING-PLACE] lines for
# ET 08-13 that the live file no longer has. Reading only the live file turned a retained day into
# a false zero, which is the exact failure this script exists to prevent.
# ⛔ NAMING: logrotate stamps the file with the date it was CREATED, so `<log>-20260814` holds the
# UTC day 2026-08-13. An ET day spans TWO UTC days (04:00 → next 03:59), hence two or more files.
# We do not try to compute which: we feed every sibling through the same window filter and let the
# timestamp comparison select. Cheap, and immune to the off-by-one that naming invites.
logfiles() {  # every file that could hold lines for this window, oldest first, live file last
  local base="$1"
  sudo sh -c "ls -1 '${base}'-* 2>/dev/null" | sort
  echo "$base"
}
_readlog() { case "$1" in *.gz) sudo zcat -- "$1" 2>/dev/null;; *) sudo cat -- "$1" 2>/dev/null;; esac; }
logcat() {  # stream every readable file for this log, in chronological order
  local f
  while IFS= read -r f; do [ -n "$f" ] && logreadable "$f" && _readlog "$f"; done < <(logfiles "$1")
}
logcount() {  # logcount <log-base> <fixed-string>  -> a count, or -1 meaning UNKNOWN
  # ⛔⭐⭐ RETURNS -1, NOT 0, WHEN NO FILE CAN ANSWER FOR THIS WINDOW. An empty or freshly rotated
  # logfile otherwise yields 0 and reads as "nothing happened" — the difference between "it did not
  # fire" and "I could not see" is the whole point of this script.
  logreadable "$1" || { echo -1; return; }
  logcat "$1" | awk -v a="$UTC_FROM" -v b="$UTC_TO" -v pat="$2" '
    { ts = substr($0,1,19); gsub(",",".",ts)
      if (ts ~ /^[0-9]{4}-/) { if (fst=="") fst=ts; if (ts>lst) lst=ts }
      if (ts >= a && ts <= b && index($0,pat)) n++ }
    END {
      if (fst == "" || lst < a || fst > b) { print -1 }   # nothing on disk overlaps the window
      else print n+0
    }'
}
logtail() {  # logtail <log-base> <fixed-string> <n>
  logreadable "$1" || return
  logcat "$1" | awk -v a="$UTC_FROM" -v b="$UTC_TO" -v pat="$2" '
    { ts = substr($0,1,19); gsub(",",".",ts) }
    ts >= a && ts <= b && index($0,pat)' | tail -"$3"
}
# ⛔⭐⭐ AN UNKNOWN MUST NEVER BECOME A PASS. The first version of this script let an unreadable log
# fall through the numeric comparisons and printed "VERDICT: PASS" on counts it had never read —
# the exact false-clean it exists to prevent. Every verdict block now calls this FIRST.
unknown() { for v in "$@"; do case "$v" in ''|*[!0-9-]*|-1) return 0;; esac; done; return 1; }
show() { case "$1" in -1) echo "UNKNOWN(log cannot answer for this window)";; *) echo "$1";; esac; }
LOG_SILENT_WARN_SECS=${LOG_SILENT_WARN_SECS:-1800}   # 30 min of silence ⇒ suspect a dead writer
_ts_epoch() { date -d "$1 UTC" +%s 2>/dev/null; }
# ⛔ Sets LOG_BLIND=1 when this file does NOT cover the requested window, so the verdicts below can
# refuse to read a zero out of it. "The day has not happened yet" is NOT blindness — a zero then
# genuinely means it has not happened, which is exactly what UNEXERCISED asserts.
logcoverage() {
  local f="$1"
  LOG_BLIND=0
  logreadable "$f" || { LOG_BLIND=1; echo "   ⛔⛔ CANNOT READ $f — counts from it are UNKNOWN, not zero."; return; }
  local first last
  # ⛔ Take the first/last line that actually CARRIES a timestamp. A traceback at the tail has none,
  # and the old `tail -1 | cut -c1-19` handed back junk — which then failed the `[ -n "$last" ]`
  # guard and SKIPPED the coverage check entirely. A skipped check prints exactly like a clean one.
  # ⛔ Coverage must span the SAME file set the counts do (live + rotated siblings). Measuring
  # coverage on the live file alone while counting across all of them would declare the window blind
  # on exactly the past days the rotated files can now answer for — a false UNKNOWN in place of a
  # real count, which is the same defect as a false zero, just wearing the other mask.
  local nfiles; nfiles=$(logfiles "$f" | grep -c . )
  first=$(logcat "$f" | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' | head -1)
  last=$(logcat "$f"  | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' | tail -1)
  echo "   log $(basename "$f") + $((nfiles-1)) rotated sibling(s) cover ${first:-?} .. ${last:-?} UTC"
  if [ -z "$first" ] || [ -z "$last" ]; then
    LOG_BLIND=1
    echo "   ⛔⛔ NO TIMESTAMPED LINE in the head/tail of $f — coverage is UNKNOWN, not clean."
    return
  fi
  if [[ "$first" > "$UTC_FROM" ]]; then
    LOG_BLIND=1
    echo "   ⛔⛔ THE LOG STARTS AFTER THE WINDOW OPENS (${UTC_FROM} UTC). Rotation already ate part"
    echo "        of this day — every log count below is a LOWER BOUND, not a count."
  fi
  # ⛔⭐⭐ THE OTHER END IS **TWO DIFFERENT THINGS**. NEVER GIVE THEM THE SAME BANNER:
  #   (a) ROTATION LOSS — the ET day is OVER and the file still ends before the window closes. The
  #       evening (20:00-23:59 ET) is in the NEXT file and these counts are a genuine LOWER BOUND.
  #   (b) THE DAY HAS NOT HAPPENED YET — counts are partial by construction. Not a defect.
  # The first version compared `last` against min(now, window_end), so on EVERY intraday run the
  # few seconds of log latency tripped (a)'s ⛔⛔ banner. A warning that is always on is not a
  # warning: at 19:30, when rotation loss is real, it would have looked identical to 06:38 noise.
  local now_utc; now_utc=$(date -u '+%Y-%m-%d %H:%M:%S')
  if [[ "$now_utc" > "$UTC_TO" ]]; then
    if [[ "$last" < "$UTC_TO" ]]; then
      LOG_BLIND=1
      echo "   ⛔⛔ THE ET DAY IS OVER but the log ends at ${last} UTC, before the window closes"
      echo "        (${UTC_TO} UTC). The rotated siblings ARE being read, so this is not the old"
      echo "        20:00 ET problem — this day has AGED OUT of retention. Counts are a LOWER BOUND."
    fi
  else
    echo "   ℹ the ET day is still running (now ${now_utc} UTC, closes ${UTC_TO} UTC) — counts are"
    echo "     PARTIAL by construction, not by rotation. Re-run after the close for the full day —"
    echo "     rotation no longer costs you the evening."
    # ⛔ A SILENT log is a different failure from a partial day, and it is the one that fakes a
    # clean: a dead writer yields zeros that read exactly like "the path never fired".
    local le ne age; le=$(_ts_epoch "$last"); ne=$(date +%s)
    if [ -n "$le" ]; then
      age=$(( ne - le ))
      if [ "$age" -gt "$LOG_SILENT_WARN_SECS" ]; then
        echo "   ⛔ $(basename "$f") HAS BEEN SILENT FOR $(( age / 60 )) MIN — suspect a dead writer."
        echo "     Its zeros below are UNKNOWN, not counts. Check the service before reading them."
      fi
    fi
  fi
}

NOW_ET=$(TZ=America/New_York date '+%F %H:%M')
NOW_H=$(TZ=America/New_York date +%H)
echo "VALIDATING THE 2026-08-13 DEPLOY   day=${DAY_ET} (ET)   run at ${NOW_ET} ET"

# ── 0. THE SCRIPT MUST PROVE IT CAN SEE ANYTHING AT ALL ──────────────────────────────────────────
# ⛔ A watch that fails to a false clean is worse than no watch. Before trusting a single zero
# below, run the reject query against a KNOWN-BAD TAPE: 2026-08-13 had exactly 58 rejects on
# live:orb, 56 of them XHG. If this control does not reproduce, EVERY zero in this script is
# meaningless and the run is void.
hdr "0. SELF-TEST against the known-bad tape (2026-08-13 = 58 orb rejects)"
CTRL=$(q1 "select count(*) from broker_orders bo join broker_accounts ba on ba.id=bo.broker_account_id
 where ba.name='live:orb' and bo.status='rejected'
   and bo.submitted_at >= '2026-08-13 00:00:00'::timestamp at time zone 'America/New_York'
   and bo.submitted_at <  '2026-08-14 00:00:00'::timestamp at time zone 'America/New_York';")
echo "   control reject count for 2026-08-13 = ${CTRL:-ERROR} (expected 58)"
if [ "${CTRL:-0}" -ge 55 ] 2>/dev/null; then
  verdict "PASS" "the query reproduces a known-bad day, so a zero below is real"
else
  verdict "VOID" "SELF-TEST FAILED — the query cannot see a day we KNOW was bad. Every result below is untrustworthy. Fix the query before reading further."
fi

# ── 0b. THE CONTROL MUST COVER THE THING BEING MEASURED, NOT JUST *A* THING ──────────────────────
# ⛔⭐⭐ §0 reproduces 58 rejects. That proves the reject query can see `broker_order_events`. It
# proves NOTHING about whether a RESTING ENTRY or a BRACKET would be visible if one existed —
# different tables, different joins, different log file. A control on the wrong population is how
# "it never happened" gets asserted from a source that could not have held it either way.
# ⛔ CONTROL DAY = 2026-08-12, chosen because it is the most recent day with a strong population in
# every channel at once. Do NOT move it earlier to "before the change": 2026-08-07 has ZERO Schwab
# brackets and ZERO filled rests, so it would pass this control vacuously.
CTRL_DAY=2026-08-12
CONTROL_VOID=0
ctrl() {  # ctrl <label> <actual> <min-expected>
  local lbl="$1" got="${2:-}" min="$3"
  if [ -n "$got" ] && [ "$got" -ge "$min" ] 2>/dev/null; then
    printf '   ✓ %-46s %s (>= %s)\n' "$lbl" "$got" "$min"
  else
    printf '   ✗ %-46s %s (expected >= %s)\n' "$lbl" "${got:-ERROR}" "$min"; CONTROL_VOID=1
  fi
}
hdr "0b. POPULATION CONTROLS — could this run SEE a rest / a fill / a bracket at all? (${CTRL_DAY})"
C0="'${CTRL_DAY} 00:00:00'::timestamp at time zone 'America/New_York'"
C1="'${CTRL_DAY} 23:59:59'::timestamp at time zone 'America/New_York'"
ctrl "DB  Schwab resting entries (STOP_LIMIT buy)" "$(q1 "select count(*) from broker_orders bo
  join broker_accounts ba on ba.id=bo.broker_account_id where ba.name='live:schwab_1m_v2'
  and bo.order_type='STOP_LIMIT' and lower(bo.side)='buy'
  and bo.submitted_at >= $C0 and bo.submitted_at <= $C1;")" 300
ctrl "DB  Schwab BRACKET legs (oco_exit)" "$(q1 "select count(*) from broker_orders bo
  join broker_accounts ba on ba.id=bo.broker_account_id where ba.name='live:schwab_1m_v2'
  and bo.order_type='oco_exit' and bo.submitted_at >= $C0 and bo.submitted_at <= $C1;")" 25
ctrl "DB  Webull BRACKET legs (oco_exit)" "$(q1 "select count(*) from broker_orders bo
  join broker_accounts ba on ba.id=bo.broker_account_id where ba.name='live:orb'
  and bo.order_type='oco_exit' and bo.submitted_at >= $C0 and bo.submitted_at <= $C1;")" 10
ctrl "DB  Webull BUY fills (the attach opportunity)" "$(q1 "select count(*) from fills f
  join broker_accounts ba on ba.id=f.broker_account_id where ba.name='live:orb'
  and lower(f.side)='buy' and f.filled_at >= $C0 and f.filled_at <= $C1;")" 10
# ⭐ The LOG control is the one that matters most, because it exercises the whole chain §3 uses:
# rotated-file reading + the UTC window filter + the exact marker string.
CTRL_UF=$(date -u -d "TZ=\"America/New_York\" ${CTRL_DAY} 00:00:00" '+%Y-%m-%d %H:%M:%S')
CTRL_UT=$(date -u -d "TZ=\"America/New_York\" ${CTRL_DAY} 23:59:59" '+%Y-%m-%d %H:%M:%S')
ctrl "LOG [V2-RESTING-PLACE] in a ROTATED file" "$(UTC_FROM=$CTRL_UF UTC_TO=$CTRL_UT logcount "$V2LOG" '[V2-RESTING-PLACE]')" 300
if [ "$CONTROL_VOID" = "0" ]; then
  verdict "PASS" "a rest, a fill and a bracket are all VISIBLE to this run — a zero below is a real zero"
else
  verdict "VOID" "A CONTROL FAILED — this run cannot prove it would see what it is counting. Every zero-based verdict below is now VOID. Fix the query before reading anything."
fi
echo "   ⛔ NOT CONTROLLABLE: [V2-WEBULL-RESTING-PLACE] and [WEBULL-PROTECT-ATTACHED] are NEW in the"
echo "      2026-08-13 deploy — no past day could have emitted them, so no historical control exists."
echo "      Controlled here instead: the populations they act ON, and the log machinery §3 reads with."

if [ "$NOW_H" -ge 20 ] 2>/dev/null && [ "${DAY_ET}" = "$(TZ=America/New_York date +%F)" ]; then
  echo
  echo "   ✅ It is past 20:00 ET. The live log has rotated, but the rotated sibling is read too,"
  echo "        so the day is INTACT. (This used to declare every log count a false zero.)"
fi

# ── 1. IS THE DEPLOY EVEN ON THE BOX, AND ARE THE FLAGS LIVE? ────────────────────────────────────
hdr "1. DEPLOYED — code + flags (confirm by CONTENT, never by 'the deploy said success')"
cd "$REPO" || exit 1
echo "   HEAD: $(git log --oneline -1)"
echo "   tree: $(git status --porcelain | wc -l) local changes (⛔ non-zero BLOCKS the next deploy)"
for m in V2-FANOUT-CLAIM-EXPIRED V2-WEBULL-RESTING-PLACE WEBULL-PROTECT-ATTACHED OMS-EXIT-RELEASE OMS-EXIT-REPROTECT; do
  if grep -rqF "$m" src/ --include='*.py'; then echo "   ✓ marker present in source: [$m]"
  else echo "   ✗ MARKER MISSING FROM SOURCE: [$m]  ⛔ the deploy did not land"; fi
done
echo "   -- flags as the PROCESS sees them (not as the env file claims) --"
for p in $(pgrep -f 'mai-tai-oms|mai-tai-schwab-1m-v2'); do
  sudo tr '\0' '\n' < /proc/$p/environ 2>/dev/null \
    | grep -E 'WEBULL_RESTING_MIRROR_ENABLED|EXIT_RELEASE_RESERVATION_ENABLED' | sed "s/^/   pid=$p /"
done

# ── 2. THE DENOMINATORS — everything below is read against these ─────────────────────────────────
hdr "2. DENOMINATORS for ${DAY_ET} (an absence means nothing without these)"
q "select 'fills: '||ba.name||' = '||count(*) from fills f join broker_accounts ba on ba.id=f.broker_account_id
   where f.filled_at >= $D0 and f.filled_at <= $D1 and ba.name like 'live:%' group by ba.name;"
q "select 'orders by status: '||ba.name||' '||bo.status||' = '||count(*)
   from broker_orders bo join broker_accounts ba on ba.id=bo.broker_account_id
   where bo.submitted_at >= $D0 and bo.submitted_at <= $D1 and ba.name like 'live:%'
   group by ba.name, bo.status order by 1;"
echo "   ⛔ ACCOUNT VISIBILITY: this script sees live:schwab_1m_v2 + live:orb + paper:polygon_30s."
echo "   ⛔ Reject counts on broker_order_events conflate OUR aborts with BROKER refusals; the"
echo "      checks below read the verbatim reason string instead of trusting the count."

# ── 3. #688 — DO BOTH LEGS ACTUALLY REST AT THEIR OWN BROKER? ────────────────────────────────────
hdr "3. #688 RESTING MIRROR — the Webull leg should now WAIT at the broker, not watch a price"
logcoverage "$V2LOG"; V2_BLIND="$LOG_BLIND"
SCH_REST=$(logcount "$V2LOG" '[V2-RESTING-PLACE]')
WB_REST=$(logcount "$V2LOG" '[V2-WEBULL-RESTING-PLACE]')
WB_CANC=$(logcount "$V2LOG" '[V2-WEBULL-RESTING-CANCEL]')
EH_ARM=$(logcount "$V2LOG" '[V2-RESTING-EH-ARM]')
echo "   Schwab broker rests placed (RTH)        : $(show $SCH_REST)"
echo "   EH soft-rests (mirror CANNOT fire here) : $(show $EH_ARM)   <- excluded from the denominator"
echo "   Webull mirrors placed                   : $(show $WB_REST)"
echo "   Webull mirrors cancelled                : $(show $WB_CANC)"
if unknown "$SCH_REST" "$WB_REST"; then
  verdict "VOID" "could not read the v2 log — this is an UNKNOWN, not a pass"
elif [ "$SCH_REST" -eq 0 ]; then
  verdict_zero "$V2_BLIND" "UNEXERCISED" "no RTH broker rest happened at all — the mirror had no opportunity. NOT a pass."
elif [ "$WB_REST" -eq 0 ]; then
  verdict_zero "$V2_BLIND" "FAIL" "${SCH_REST} Schwab rests and ZERO Webull mirrors — the flag is on but nothing mirrored"
else
  verdict_pos "$V2_BLIND" "PASS" "${WB_REST} mirrors against ${SCH_REST} RTH rests"
fi
echo "   -- ⛔ ORPHAN CHECK: a mirrored rest nobody cancelled is a live order nobody owns (FRTT, 136 min) --"
q "select 'orb resting entries left non-terminal: '||count(*)
   from broker_orders bo join broker_accounts ba on ba.id=bo.broker_account_id
   where ba.name='live:orb' and lower(bo.side)='buy'
     and lower(bo.status) not in ('filled','cancelled','canceled','rejected','expired','replaced')
     and bo.submitted_at >= $D0;"
echo "   ⛔ CANNOT SEE: whether an order is still working AT WEBULL. This is our DB's view only."

# ── 4. IS A WEBULL FILL ACTUALLY PROTECTED? (#689/#690 + the OCO path) ───────────────────────────
# ⛔⭐⭐ CORRECTED 2026-08-14 — THIS SECTION READ ONE MARKER AND CALLED ITS ABSENCE "UNPROTECTED".
# There are TWO ways a Webull position gets a broker-side bracket and this only counted the second:
#   (a) `[V2-OCO-EMIT]`            — the NORMAL bracket, placed with the entry. 148 of them on 08-14.
#   (b) `[WEBULL-PROTECT-ATTACHED]`— the RE-PROTECT, invoked by `[OMS-EXIT-REPROTECT]` AFTER #691 has
#                                    cancelled (a) to attempt a close. It is NOT a bare-fill rescue.
# Reading 0 of (b) as "held with no broker-side stop" produced a FAIL on 11 fills that were in fact
# bracketed — the operator's own Webull screen (WETO Target@8.17/Stop@7.61) matched an (a) line to
# the cent and falsified it. ⇒ **Count both paths, and name which one failed.**
hdr "4. PROTECTION — a Webull fill must end up with a broker-side bracket, by EITHER path"
logcoverage "$OMSLOG"; OMS_BLIND="$LOG_BLIND"
OCO_ALL=$(logcount "$OMSLOG" '[V2-OCO-EMIT]')
OCO_SKIP=$(logcount "$OMSLOG" 'SKIPPED (outside regular hours)')
OCO_REAL=-1
if ! unknown "$OCO_ALL" "$OCO_SKIP"; then OCO_REAL=$(( OCO_ALL - OCO_SKIP )); fi
ATT=$(logcount "$OMSLOG" '[WEBULL-PROTECT-ATTACHED]')
AFAIL=$(logcount "$OMSLOG" '[WEBULL-PROTECT-FAILED]')
ARETRY=$(logcount "$OMSLOG" '[WEBULL-PROTECT-RETRY]')
BAREFILL=$(q1 "select count(*) from fills f join broker_accounts ba on ba.id=f.broker_account_id
   where ba.name='live:orb' and lower(f.side)='buy' and f.filled_at >= $D0 and f.filled_at <= $D1;")
echo "   Webull BUY fills (the opportunity)     : ${BAREFILL:-?}"
echo "   (a) [V2-OCO-EMIT] brackets placed      : $(show $OCO_REAL)   <- the NORMAL path; this is the one that protects"
echo "       ...of which SKIPPED (EH, no stop)  : $(show $OCO_SKIP)"
echo "   (b) [WEBULL-PROTECT-ATTACHED] re-attach: $(show $ATT)   <- only after #691 CANCELLED (a)"
echo "       [WEBULL-PROTECT-RETRY]             : $(show $ARETRY)   (a retry is fine; a FAILED is not)"
echo "       [WEBULL-PROTECT-FAILED]            : $(show $AFAIL)   🔴 a cancelled bracket that could NOT be restored"
say_verdict "$OMS_BLIND" "$(classify_protection "${BAREFILL:-0}" "$OCO_REAL" "$ATT" "$AFAIL")"
[ "${AFAIL:-0}" -gt 0 ] 2>/dev/null && logtail "$OMSLOG" '[WEBULL-PROTECT-FAILED]' 3 | sed 's/^/      /'
echo "   -- #690: the 40-char cap. Look for the id-length refusal, which is what it prevents --"
q "select 'ILLEGAL_PARAMETER/coid-length rejects: '||count(*) from broker_order_events e
   join broker_orders bo on bo.id=e.order_id join broker_accounts ba on ba.id=bo.broker_account_id
   where ba.name='live:orb' and lower(e.event_type)='rejected' and e.event_at >= $D0
     and (e.payload->>'reason') ~* 'ILLEGAL_PARAMETER|client_order_id';"
echo "   (expect 0. Non-zero ⇒ #690 did not cover the id being minted.)"

# ── 5. #691 — DID THE RESERVATION FIGHT ACTUALLY STOP? ───────────────────────────────────────────
hdr "5. #691 RELEASE — the close should no longer be refused as a naked short"
REL=$(logcount "$OMSLOG" '[OMS-EXIT-RELEASE]')
RELRAISE=$(logcount "$OMSLOG" '[OMS-EXIT-RELEASE-RAISED]')
echo "   [OMS-EXIT-RELEASE]        : $(show $REL)"
echo "   [OMS-EXIT-RELEASE-RAISED] : $(show $RELRAISE)  (a failed release ⇒ the close may still be refused)"
echo "   -- THE HEADLINE NUMBER: the two reservation reject strings. 2026-08-13 = 58. --"
RESV=$(q1 "select count(*) from broker_order_events e
   join broker_orders bo on bo.id=e.order_id join broker_accounts ba on ba.id=bo.broker_account_id
   where ba.name='live:orb' and lower(e.event_type)='rejected'
     and e.event_at >= $D0 and e.event_at <= $D1
     and (e.payload->>'reason') ~* 'CAN_NOT_SELL_SHORT|NOT_SUPPORT_REVERSE';")
CLOSES=$(q1 "select count(*) from broker_orders bo join broker_accounts ba on ba.id=bo.broker_account_id
   where ba.name='live:orb' and lower(bo.side)='sell' and bo.client_order_id like '%-close-%'
     and bo.submitted_at >= $D0 and bo.submitted_at <= $D1;")
echo "   reservation rejects today : ${RESV:-?}   (was 58 on 08-13)"
echo "   -close- sells attempted   : ${CLOSES:-?}   <- the denominator"
q "select '   -close- outcome: '||bo.status||' = '||count(*)
   from broker_orders bo join broker_accounts ba on ba.id=bo.broker_account_id
   where ba.name='live:orb' and lower(bo.side)='sell' and bo.client_order_id like '%-close-%'
     and bo.submitted_at >= $D0 and bo.submitted_at <= $D1 group by bo.status;"
if unknown "${CLOSES:-x}" "${RESV:-x}"; then
  verdict "VOID" "the DB query did not return a number — do not read this section"
elif [ "${CLOSES:-0}" -eq 0 ]; then
  verdict "UNEXERCISED" "the software ladder never tried to close a Webull position — #691 had no opportunity"
elif [ "${RESV:-0}" -eq 0 ]; then
  verdict "PASS" "${CLOSES} closes attempted, ZERO reservation rejects (08-13 had 58)"
elif [ "${RESV:-0}" -lt 10 ]; then
  verdict "PARTIAL" "${RESV} rejects vs 58 on 08-13 — collapsed but not gone; expect 1 per episode while the cancel lands"
else
  verdict "FAIL" "${RESV} reservation rejects — the release is not clearing the shares"
fi

# ── 6. #692 — DID ANYTHING END UP UNCOVERED? ─────────────────────────────────────────────────────
hdr "6. #692 REPROTECT — the net that #691 removes must be put back"
RP=$(logcount "$OMSLOG" '[OMS-EXIT-REPROTECT]')
RPS=$(logcount "$OMSLOG" '[OMS-EXIT-REPROTECT-SKIPPED]')
RPF=$(logcount "$OMSLOG" '[OMS-EXIT-REPROTECT-FAILED]')
echo "   [OMS-EXIT-REPROTECT]         : $(show $RP)   (of $(show $REL) releases — 0 here is GOOD, and is not a bare zero)"
echo "   [OMS-EXIT-REPROTECT-SKIPPED] : $(show $RPS)  🔴 could not price a pair ⇒ MAY BE UNCOVERED"
echo "   [OMS-EXIT-REPROTECT-FAILED]  : $(show $RPF)  🔴 re-attach itself failed ⇒ MAY BE UNCOVERED"
# ⛔⭐⭐ CORRECTED 2026-08-14 — THIS PRINTED **PASS** OVER NINE LIVE FAILURES.
# It read `[OMS-EXIT-REPROTECT-FAILED]` (=0) and declared "none failed", while the re-attach it
# triggers was failing 9x under `[WEBULL-PROTECT-FAILED]`. The re-protect CALLS the Webull attach,
# and the attach reports its own outcome under its OWN marker — so counting only the caller's marker
# is a false clean on the exact condition this section exists to catch.
# ⇒ **A re-protect has failed if EITHER marker fires.** $AFAIL is carried down from §4.
echo "   [WEBULL-PROTECT-FAILED]      : $(show $AFAIL)  🔴 the re-attach this section TRIGGERS — counts as a failure here"
say_verdict "$OMS_BLIND" "$(classify_reprotect "$REL" "$RP" "$RPS" "$RPF" "$AFAIL")"
if [ "${RPS:-0}" -gt 0 ] || [ "${RPF:-0}" -gt 0 ] || [ "${AFAIL:-0}" -gt 0 ] 2>/dev/null; then
  { logtail "$OMSLOG" '[OMS-EXIT-REPROTECT-SKIPPED]' 2; logtail "$OMSLOG" '[OMS-EXIT-REPROTECT-FAILED]' 2
    logtail "$OMSLOG" '[WEBULL-PROTECT-FAILED]' 2; } | sed 's/^/      /'
fi

# ── 7. #687 — DOES A REJECTED WEBULL LEG STOP KILLING THE FLIP? ──────────────────────────────────
hdr "7. #687 CLAIM EXPIRY — one rejection must not burn the rest of the flip"
EXP=$(logcount "$V2LOG" '[V2-FANOUT-CLAIM-EXPIRED]')
COLL=$(logcount "$OMSLOG" 'fanout_webull_collision_managed')
ONFILL=$(logcount "$V2LOG" '[V2-FANOUT-ON-FILL]')
echo "   [V2-FANOUT-CLAIM-EXPIRED] : $(show $EXP)   (fires only when a claimed leg never filled)"
echo "   [V2-FANOUT-ON-FILL]       : $(show $ONFILL)"
echo "   fanout_webull_collision_managed skips : $(show $COLL)"
if unknown "$EXP"; then
  verdict "VOID" "could not read the v2 log"
elif [ "$EXP" -gt 0 ]; then
  verdict_pos "$V2_BLIND" "PASS" "the claim expired ${EXP}x instead of latching the flip shut"
else
  verdict_zero "$V2_BLIND" "UNEXERCISED" "no claim needed expiring — requires a Webull leg that was claimed and never filled. NOT a pass."
fi

# ── 8. #693 — ARE THE CRONS STILL ALIVE, AND CAN WE STILL DEPLOY? ────────────────────────────────
hdr "8. #693 CRON EXEC BIT — a dirty tree blocks every deploy; a lost +x kills two pagers"
for f in ops/health/bar_gap_watch_cron.sh ops/health/reconcile_alert_cron.sh; do
  if [ -x "$REPO/$f" ]; then echo "   ✓ executable: $f"; else echo "   ✗ NOT EXECUTABLE: $f  ⛔ this cron is silently dead"; fi
done
DIRTY=$(cd "$REPO" && git status --porcelain | wc -l)
if [ "$DIRTY" -eq 0 ]; then verdict "PASS" "tree clean, next deploy will not be refused"
else verdict "FAIL" "${DIRTY} local change(s) — the next deploy WILL be refused. ⛔ do NOT 'git checkout' a cron to fix this; commit the mode."; cd "$REPO" && git status --short | sed 's/^/      /'; fi

# ── 9. WHAT IT COST — percentages, median first ──────────────────────────────────────────────────
hdr "9. THE MONEY — per-trade %, median first (never a bare dollar total)"
q "with rt as (
     select f.symbol, ba.name acct, lower(f.side) side, f.price, f.filled_at,
            row_number() over (partition by ba.name, f.symbol order by f.filled_at) rn
     from fills f join broker_accounts ba on ba.id=f.broker_account_id
     where f.filled_at >= $D0 and f.filled_at <= $D1 and ba.name like 'live:%')
   select '   '||acct||': trades='||count(*)/2||'  median%='||
          coalesce(round(percentile_cont(0.5) within group (order by pct)::numeric,2)::text,'-')||
          '  worst%='||coalesce(round(min(pct)::numeric,2)::text,'-')||
          '  best%='||coalesce(round(max(pct)::numeric,2)::text,'-')
   from (
     select b.acct, 100*(s.price-b.price)/b.price pct
     from rt b join rt s on s.acct=b.acct and s.symbol=b.symbol and s.rn=b.rn+1
     where b.side='buy' and s.side='sell') x group by acct;"
echo "   ⛔ Naive buy→next-sell pairing. It CANNOT attribute per-lot and it cannot see which route"
echo "      exited. Treat as a magnitude, not as attribution."

hdr "DONE"
echo "⛔ A VOID means the window aged out of log retention, not that you ran too late — the rotated"
echo "   siblings are read, so a same-day evening run is fine."
echo "Read the VERDICT lines only. UNEXERCISED means the path never ran — that is a RESULT,"
echo "not a pass, and it means tomorrow has to ask again."
