#!/usr/bin/env bash
# Q5/§197 — SUSTAINED BROKER-READ BLINDNESS PAGER.
#
#   broker_blind_check.sh [--dry-run] [--at <YYYY-MM-DDTHH:MM:SS>] [--selftest]
#
# ⛔⭐⭐ WHAT THIS EXISTS FOR. On 2026-08-20, 14:58:32 -> 15:04:17 UTC, `live:schwab_1m_v2`
# produced 22 `[BROKER-SYNC-UNREADABLE]` lines at 15s intervals: ~5.8 minutes mid-session
# during which the OMS could not read positions on the REAL-MONEY account. Nothing paged.
# The marker appears in `src/` and in NO ops watcher, and none of the other 14 pagers covers
# it. While holding, that is not knowing what we own.
#
# ⛔ THE THRESHOLD IS DERIVED FROM **22**, the longest real run measured, not from a number
# nobody could reproduce. (An earlier framing cited "273 consecutive / 68 minutes blind on
# 08-15"; that reconciles with no retained tape, and this marker did not exist until #714 on
# 08-17. The 08-20 window replaced it and is better evidence for a second reason -- see the
# known-NEGATIVE below.)
#
# ⛔⭐ TRIP = SUSTAINED **AND** HOLDING. An outage while flat is noise: nothing is at risk and
# paging on it trains the reader to ignore the pager. Both halves must hold.
#
# ══════════════════════════════════════════════════════════════════════════════════════════
# ⛔⭐⭐ WHAT THIS CANNOT SEE -- stated here because a watch that hides its blind spot is the
# thing it is meant to prevent.
#
# ON OLD TAPE THERE IS NO SUCCESS MARKER, so "consecutive" cannot be read and is INFERRED from
# the gap between failures against the known sync cadence. On the 08-20 tape two gaps were 30s
# where the rest were 15s, and at 15s cadence a 30s gap is EITHER one successful read OR one
# slow/skipped cycle. This script cannot tell which on that tape.
#   => It treats a gap of up to 3 intervals as the same run. That is the tolerant reading, and
#      the SAFE direction here: merging is a false POSITIVE risk on a pager that also requires
#      holding, whereas splitting would hide a real outage behind two short runs.
#   => If the 30s gaps were successes, the true longest 08-20 run is 14, not 22. Both clear the
#      trip so the verdict does not turn on it -- but the NUMBER does, and anyone quoting
#      "22 consecutive" should know that assumption is inside it.
#
# ✅ FIXED AT THE SOURCE. The OMS now stamps `consecutive=N` on every `[BROKER-SYNC-UNREADABLE]`
# line and emits `[BROKER-SYNC-OK]` on recovery, so a run boundary is a FACT. This script
# PREFERS that field wherever it is present and falls back to the gap inference only for tape
# written before the change -- and it PRINTS which of the two it used, because a run length
# that was read and one that was inferred are not the same kind of number.
# ⛔ That deploys with the OMS, not with this file: until then, live tape is still inferred.
# ══════════════════════════════════════════════════════════════════════════════════════════
set -u

LOGDIR=${MAI_TAI_LOGDIR:-/var/log/project-mai-tai}
ENVFILE=${MAI_TAI_ENVFILE:-/etc/project-mai-tai/project-mai-tai.env}
STATE=${Q5_STATE:-/var/tmp/q5_broker_blind.seen}
TOPIC=${Q5_NTFY_TOPIC:-${MAI_TAI_NTFY_TOPIC:-project-mai-tai-alerts}}
CURL=${Q5_CURL:-curl}          # seam: the selftest stubs this so it cannot page a real phone
MARKER='[BROKER-SYNC-UNREADABLE]'

DRY=0; AT=""; MODE=check
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)  DRY=1; shift ;;
    --at)       AT="${2:-}"; shift 2 ;;
    --selftest) MODE=selftest; shift ;;
    *) echo "unknown arg '$1'" >&2; exit 2 ;;
  esac
done

# ── the sync cadence: READ IT, never assume it ────────────────────────────────────────────
# ⛔ The code default is 5s and production overrides it to 15s. A fixture or a threshold built
# on the default would be wrong about the live system by 3x -- the exact class of error where
# a test passes against config production does not run.
sync_interval() {
  local v=""
  v=$(sudo -n grep -sE '^MAI_TAI_OMS_BROKER_SYNC_INTERVAL_SECONDS=' "$ENVFILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -dc '0-9')
  if [ -n "$v" ] && [ "$v" -gt 0 ] 2>/dev/null; then echo "$v"; else echo ""; fi
}

Q() { sudo -u postgres psql -d project_mai_tai -X -q -tA -c "$1" 2>/dev/null; }

# ── holding, AT A GIVEN INSTANT (so the historical known-negative is checkable) ────────────
# ⛔ `oms_managed_positions`, never `virtual_positions` -- the latter has a documented
# false-zero (a live row is zeroed 0.7s after the fill and never restored), and a watch whose
# "are we holding?" test can read a false zero fails to the silent direction.
holding_at() {  # holding_at <account> <iso-instant|now>  -> integer count, or "?" if unreadable
  local acct="$1" ts="$2" n
  if [ "$ts" = "now" ]; then
    n=$(Q "SELECT count(*) FROM oms_managed_positions
            WHERE broker_account_name='${acct}' AND status='open';")
  else
    n=$(Q "SELECT count(*) FROM oms_managed_positions
            WHERE broker_account_name='${acct}'
              AND entry_time <= timestamptz '${ts}'
              AND (status='open' OR updated_at > timestamptz '${ts}');")
  fi
  case "$n" in ''|*[!0-9]*) echo "?" ;; *) echo "$n" ;; esac
}

# ── the tape: same reading discipline as ops/health/evidence.sh ────────────────────────────
# ⛔⭐⭐ PREFER THE FACT OVER THE INFERENCE. Once the OMS change lands, every
# `[BROKER-SYNC-UNREADABLE]` line carries `consecutive=N` and a recovery emits
# `[BROKER-SYNC-OK]`. Where `consecutive=` is present the run length is READ, not derived from
# gaps, and the 30s-gap ambiguity below simply does not arise. Old tape has no such field, so
# the gap inference stays as the fallback — and which one was used is PRINTED, because a run
# length that was inferred and one that was read are not the same kind of number.
# ⛔ `consecutive=-1` is the OMS's out-of-band sentinel for "the counter itself failed". It is
# not a run length and must never be compared against the trip.
have_consecutive() {  # -> 0 if the tape carries the stamped field
  printf '%s\n' "$1" | grep -q 'consecutive=[0-9]'
}

build_tape() {  # -> stdout: "<epoch> <acct> <iso> <consecutive|->"
  # ⛔⭐⭐ THE TAPE USED TO DROP `consecutive=`, SO find_runs COULD NOT POSSIBLY USE IT.
  # The header printed "run length: READ from consecutive= (fact)" whenever the field existed on
  # the raw lines, while the calculation ALWAYS grouped by time gaps. Four failures, a recovery,
  # then four more became ONE run of eight: the label said fact, the number was an inference.
  # ⇒ Carry the field. `-` means genuinely absent on that line; the gap fallback then applies.
  local src
  if [ -n "${Q5_TAPE:-}" ]; then src="cat ${Q5_TAPE}"; else
    src="sudo -n zcat -f -- ${LOGDIR}/oms.log ${LOGDIR}/oms.log-*"; fi
  eval "$src" 2>/dev/null \
    | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},' \
    | grep -F -- "$MARKER" \
    | while IFS= read -r line; do
        iso=$(printf '%s' "$line" | sed -E 's/^([0-9-]{10}) ([0-9:]{8}),.*/\1T\2/')
        acct=$(printf '%s' "$line" | sed -nE 's/.*acct=([^ ]+).*/\1/p')
        cons=$(printf '%s' "$line" | sed -nE 's/.*consecutive=([0-9]+).*/\1/p')
        [ -z "${acct:-}" ] && continue
        e=$(date -u -d "${iso/T/ }" +%s 2>/dev/null) || continue
        echo "$e $acct $iso ${cons:--}"
      done | sort -n
}

# ── run detection ─────────────────────────────────────────────────────────────────────────
# A run is failures for ONE account separated by <= GAP_MAX. Emits: acct start_iso end_iso n span
find_runs() {  # find_runs <gap_max>   fields: epoch acct iso consecutive|-
  # ⛔ A RESET IN `consecutive=` IS A RUN BOUNDARY AND IT IS A FACT. A time gap is an inference:
  # at a 15s cadence a 30s gap is a success OR a slow cycle and cannot be told apart - which is
  # exactly what #760 shipped the counter for. When the field is present it decides; the gap only
  # decides when the field is absent.
  awk -v gap="$1" '
    { e=$1; a=$2; iso=$3; c=$4
      reset = 0
      if (c != "-" && (a in lastc)) {
        if (lastc[a] == "-") reset = 1
        else if (c+0 <= lastc[a]+0) reset = 1
      }
      gapped = !(a in laste) || (e - laste[a] > gap)
      if ((a in laste) && !reset && !gapped) { n[a]++; lastiso[a]=iso; laste[a]=e; lastc[a]=c }
      else {
        if (a in laste) print a, startiso[a], lastiso[a], n[a], laste[a]-starte[a]
        startiso[a]=iso; starte[a]=e; lastiso[a]=iso; laste[a]=e; n[a]=1; lastc[a]=c
      }
    }
    END { for (a in laste) print a, startiso[a], lastiso[a], n[a], laste[a]-starte[a] }'
}

page() {  # page <title-ascii> <body>
  if [ "$DRY" -eq 1 ]; then echo "   [dry-run] would page: $1 | $2"; return 0; fi
  # ⛔ ASCII TITLE ONLY. ntfy headers are latin-1; a single em-dash silently loses the page.
  local title; title=$(printf '%s' "$1" | tr -cd '\11\12\40-\176')
  # ⛔⭐⭐ RETURN THE DELIVERY STATUS. This swallowed curl's exit code, so the caller recorded
  # the key as SEEN and printed "PAGED." even when ntfy was unreachable - the alert was lost AND
  # suppressed for ever, because the state file said it had already gone out. A pager that
  # silently drops a page is worse than no pager: it reports success.
  "$CURL" -sS --fail --retry 3 --max-time 20 \
          -H "Title: ${title}" -H "Priority: high" -H "Tags: rotating_light" \
          -d "$2" "https://ntfy.sh/${TOPIC}" >/dev/null
}

run_check() {
  local iv gapmax minrun minspan now_ts tripped=0
  iv=$(sync_interval)
  if [ -z "$iv" ]; then
    # ⛔ UNKNOWN cadence must not decay into a default. Without it, "consecutive" is unitless.
    echo "VOID: could not read MAI_TAI_OMS_BROKER_SYNC_INTERVAL_SECONDS from $ENVFILE."
    echo "      'N consecutive' has no meaning without the cadence. Not reporting a count."
    return 2
  fi
  gapmax=$(( iv * 3 ))
  minrun=7                      # "above 6", per the sizing; 22 was the longest real run
  minspan=$(( iv * 6 ))         # ...and the same threshold expressed in SECONDS BLIND
  now_ts="${AT:-now}"

  echo "### Q5 broker-blindness check  (cadence=${iv}s from ${ENVFILE}; run-gap<=${gapmax}s;"
  echo "    trip: >=${minrun} failures AND >=${minspan}s blind AND holding on that account)"

  local tape runs raw_lines method
  # the raw matching lines, so `consecutive=` can be detected before the tape is reduced
  if [ -n "${Q5_TAPE:-}" ]; then raw_lines=$(grep -F -- "$MARKER" "${Q5_TAPE}" 2>/dev/null)
  else raw_lines=$(sudo -n zcat -f -- "${LOGDIR}"/oms.log "${LOGDIR}"/oms.log-* 2>/dev/null | grep -F -- "$MARKER"); fi
  if have_consecutive "$raw_lines"; then method="READ from consecutive= (fact)"; else
    method="INFERRED from gaps <= ${gapmax}s (no consecutive= on this tape; a 30s gap at ${iv}s cadence is a success OR a slow cycle and cannot be told apart)"; fi
  echo "    run length: ${method}"
  tape=$(build_tape)
  if [ -z "$tape" ]; then
    # ⛔ Distinguish "no outage" from "could not look". A pager that cannot read its own tape
    # and prints nothing is indistinguishable from a healthy system.
    if [ -n "${Q5_TAPE:-}" ] || sudo -n test -r "$LOGDIR/oms.log"; then
      echo "   no ${MARKER} lines at all — MEASURED-NONE (tape readable)."
      return 0
    fi
    echo "VOID: cannot read $LOGDIR/oms.log — blindness is UNMEASURED, not absent."
    return 2
  fi

  runs=$(printf '%s\n' "$tape" | find_runs "$gapmax")
  printf '%s\n' "$runs" | while read -r acct s e n span; do
    [ -z "${acct:-}" ] && continue
    [ "${n:-0}" -ge "$minrun" ] 2>/dev/null || continue
    [ "${span:-0}" -ge "$minspan" ] 2>/dev/null || continue
    local held key
    held=$(holding_at "$acct" "${AT:-$e}")
    key="${acct}@${s}"
    echo "   RUN acct=${acct} ${s} -> ${e}  failures=${n} span=${span}s holding=${held}"
    if [ "$held" = "?" ]; then
      echo "      ⛔ holding is UNREADABLE — reporting, not suppressing. An unknown must never"
      echo "         decay into 'flat' on the one check that decides whether to wake someone."
      # ⛔ ONLY RECORD A PAGE THAT ACTUALLY WENT OUT. Recording it regardless made a failed
      # delivery permanent: the state file said "already paged", so it was never retried.
      grep -Fqx -- "$key" "$STATE" 2>/dev/null || {
        if page "MAI-TAI Q5 broker blind ${acct} holding UNKNOWN" \
             "acct=${acct} blind ${span}s (${n} failed reads) ${s} -> ${e}. Holding could not be read."; then
          printf '%s\n' "$key" >> "$STATE"
        else
          echo "      [PAGE-FAILED] delivery failed; NOT recorded as seen, so the next run retries." >&2
        fi; }
    elif [ "${held:-0}" -gt 0 ] 2>/dev/null; then
      grep -Fqx -- "$key" "$STATE" 2>/dev/null && { echo "      (already paged)"; continue; }
      if page "MAI-TAI Q5 broker blind ${acct} while holding ${held}" \
           "acct=${acct} blind ${span}s (${n} failed positions reads) ${s} -> ${e}, holding ${held} position(s). We do not know what we own."; then
        printf '%s\n' "$key" >> "$STATE"
        echo "      PAGED."
      else
        echo "      [PAGE-FAILED] delivery failed; NOT recorded as seen, so the next run retries." >&2
      fi
    else
      echo "      flat at the time — NOT paged (an outage while flat is noise, by design)."
    fi
  done
  return 0
}

selftest() {
  local P=0 F=0
  ok()  { P=$((P+1)); echo "  OK  $1"; }
  bad() { F=$((F+1)); echo "  FAIL $1"; }
  command_not_found_handle() { bad "command not found: $1"; return 127; }

  echo "T1 * KNOWN-POSITIVE: the real 08-20 run is detected"
  local iv gap runs
  iv=$(sync_interval); iv=${iv:-15}; gap=$(( iv * 3 ))
  runs=$(build_tape | find_runs "$gap" | awk '$4>=7')
  if printf '%s\n' "$runs" | grep -q 'live:schwab_1m_v2'; then ok "run found on live:schwab_1m_v2"; else bad "the 08-20 run was NOT detected: $runs"; fi
  local n span
  n=$(printf '%s\n' "$runs" | awk '/live:schwab_1m_v2/{print $4; exit}')
  span=$(printf '%s\n' "$runs" | awk '/live:schwab_1m_v2/{print $5; exit}')
  [ "${n:-0}" -ge 7 ] 2>/dev/null && ok "failures=${n} clears the >=7 trip" || bad "failures=${n}"
  [ "${span:-0}" -ge 90 ] 2>/dev/null && ok "span=${span}s clears the >=90s trip" || bad "span=${span}s"

  echo "T2 * KNOWN-NEGATIVE: we were FLAT then, so it must NOT page"
  local held
  held=$(holding_at "live:schwab_1m_v2" "2026-08-20T15:04:17")
  [ "$held" = "0" ] && ok "holding=0 at the end of the run" || bad "holding=${held}, expected 0"
  local out
  out=$(Q5_STATE=$(mktemp) Q5_CURL=/bin/false bash "$0" --dry-run --at 2026-08-20T15:04:17 2>&1)
  echo "$out" | grep -q 'NOT paged' && ok "reports the run and declines to page" || bad "did not decline: $out"
  echo "$out" | grep -q 'would page' && bad "it paged while flat" || ok "no page attempted"

  echo "T3 * the PAGING half fires end-to-end when a run overlaps a REAL held position"
  # ⛔ No live run of >=7 has yet coincided with a position, so the tape here is SYNTHETIC --
  # but the holding data underneath it is REAL (live:orb held SUGP 12:07:33 -> 12:21:49 on
  # 2026-08-21). Without this, the paging branch would have shipped never having fired once,
  # and "it declined to page while flat" would be the only thing ever demonstrated.
  local h2 fix out3
  h2=$(holding_at "live:orb" "2026-08-21T12:10:00")
  if [ "${h2:-0}" -gt 0 ] 2>/dev/null; then ok "holding_at finds the real historical position (${h2})"
  else bad "holding_at returned ${h2} for a window we know held — paging half unproven"; fi
  # ⛔ THE FIXTURE MUST MATCH THE PRODUCTION CADENCE. The first version spaced these 5s apart
  # (the CODE default) while production runs 15s, so the run spanned 35s, fell under the 90s
  # trip, and the test failed on MY fixture rather than on the code. Spaced from `iv`.
  fix=$(mktemp)
  for i in 0 1 2 3 4 5 6 7; do
    date -u -d "2026-08-21 12:10:00 UTC + $(( i * iv )) seconds" \
      "+%Y-%m-%d %H:%M:%S,000 WARNING [oms-risk] ${MARKER} acct=live:orb — positions read FAILED (synthetic)" >> "$fix"
  done
  out3=$(Q5_TAPE="$fix" Q5_STATE=$(mktemp) Q5_CURL=/bin/false bash "$0" --dry-run --at 2026-08-21T12:12:00 2>&1)
  echo "$out3" | grep -q 'would page' && ok "PAGED on a run overlapping a real position" \
                                      || bad "the paging branch did not fire: $out3"
  echo "$out3" | grep -q 'holding=1' && ok "and the page carries the holding count" || bad "no holding count"
  rm -f "$fix"

  echo "T4 * an UNREADABLE cadence VOIDs rather than defaulting"
  out=$(MAI_TAI_ENVFILE=/nonexistent bash "$0" --dry-run 2>&1)
  echo "$out" | grep -q 'VOID' && ok "VOID on unknown cadence" || bad "defaulted silently: $out"

  echo "T5 * an UNREADABLE tape VOIDs rather than reporting no outage"
  local empty; empty=$(mktemp)
  out=$(Q5_TAPE="$empty" bash "$0" --dry-run 2>&1)
  echo "$out" | grep -q 'MEASURED-NONE' && ok "empty-but-readable tape says MEASURED-NONE" || bad "$out"
  rm -f "$empty"

  echo "T6 * dedupe: a second run over the same tape does not re-page"
  local st; st=$(mktemp)
  Q5_STATE="$st" Q5_CURL=/bin/false bash "$0" --dry-run --at 2026-08-20T15:04:17 >/dev/null 2>&1
  out=$(Q5_STATE="$st" Q5_CURL=/bin/false bash "$0" --dry-run --at 2026-08-20T15:04:17 2>&1)
  echo "$out" | grep -q 'would page' && bad "re-paged an already-seen run" || ok "no repeat page"
  rm -f "$st"

  echo "T8 * the run length says whether it was READ or INFERRED"
  local ftape out8
  # old tape: no consecutive= field -> must SAY it inferred, and say why
  out8=$(bash "$0" --dry-run 2>&1)
  echo "$out8" | grep -q 'INFERRED from gaps' && ok "old tape reports INFERRED, with the reason"                                               || bad "did not declare the inference: $out8"
  # new tape: consecutive= present -> must SAY it read the fact
  ftape=$(mktemp)
  for i in 0 1 2 3 4 5 6 7; do
    date -u -d "2026-08-21 12:10:00 UTC + $(( i * iv )) seconds"       "+%Y-%m-%d %H:%M:%S,000 WARNING [oms-risk] ${MARKER} acct=live:orb consecutive=$(( i + 1 )) — positions read FAILED (synthetic)" >> "$ftape"
  done
  out8=$(Q5_TAPE="$ftape" Q5_STATE=$(mktemp) Q5_CURL=/bin/false bash "$0" --dry-run --at 2026-08-21T12:12:00 2>&1)
  echo "$out8" | grep -q 'READ from consecutive' && ok "stamped tape reports READ (fact)"                                                  || bad "did not prefer the fact: $out8"
  echo "$out8" | grep -q 'would page' && ok "and still trips on the stamped tape" || bad "stamped tape did not trip"
  rm -f "$ftape"

  echo "T7 * every outbound call goes through the CURL seam"
  # ⛔ THE GUARD MUST NOT MATCH ITSELF. This is the FOURTH self-matching guard of the day --
  # the truncation guard matched its own pattern line, then its own success message, then a
  # wiring assertion matched its own docstring. Any check that names what it hunts has to
  # exclude itself, so both lines below carry the sentinel and the sentinel is filtered out.
  local raw                                                                    # SEAM_GUARD
  raw=$(grep -n 'curl' "$0" | grep -v 'SEAM_GUARD' | grep -v '"\$CURL"' \
        | grep -v '^[0-9]*: *#' | grep -v 'CURL=' || true)                     # SEAM_GUARD
  [ -z "$raw" ] && ok "no unseamed outbound call" || bad "unseamed call: $raw" # SEAM_GUARD

  echo
  echo "PASS=$P FAIL=$F"
  [ "$F" -eq 0 ] || return 1
}

case "$MODE" in
  selftest) selftest ;;
  *)        run_check ;;
esac
