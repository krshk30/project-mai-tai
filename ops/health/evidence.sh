#!/usr/bin/env bash
# §190 — THE ONE ENTRY POINT FOR READING EVIDENCE OUT OF THE LOGS.
#
# Usage (run ON the box; feed over ssh with `ssh mai-tai-vps 'bash -s' -- <args> < evidence.sh`):
#
#   evidence.sh count  --service <name> --marker '<literal>' [--pattern '<regex>'] [--since <when>]
#   evidence.sh lines  --service <name> --marker '<literal>' [--pattern '<regex>'] [--since <when>]
#                      [--out <file>]
#   evidence.sh markers --service <name>            # every bracketed marker present, with counts
#   evidence.sh verify --marker '<literal>'         # is this string actually in the source?
#   evidence.sh selftest                            # prove every branch against known tape
#
#   <when> is `boot` (that service's ActiveEnterTimestamp), `all`, or an ISO instant with a
#   **T** separator and no space: 2026-08-20T20:16:46. ⛔ NO SPACES — `ssh host 'bash -s' -- "a b"`
#   joins argv with spaces before the remote shell re-splits it, so a spaced timestamp silently
#   arrives as two arguments and the window becomes nonsense. Learned the hard way in §185.
#
# ══════════════════════════════════════════════════════════════════════════════════════════
# ⛔⭐⭐ WHY THIS FILE EXISTS
#
# The rules for reading evidence were written down and then broken anyway, five times in one
# week, every time inside tooling we had just built:
#
#   1. `grep -c … || echo 0` turned *Permission denied* into a clean 0 — three separate times.
#      The logs are root:root 0640, so this is the DEFAULT outcome of the obvious command.
#   2. `awk '$0 >= "<ts>"'` string-compares whole lines, so every traceback continuation line
#      in the file passed the "time filter" regardless of its time. It manufactured
#      "48 tracebacks since boot" when the truth was 0.
#   3. `zcat file.log file.log-*` emits the CURRENT file before the rotations, so `tail -3`
#      returned the end of the OLDEST rotation and silently omitted TODAY.
#   4. `head -24` / `head -45` / a 110-char cut produced three wrong conclusions in one day —
#      one of them nearly reported a real fill as a phantom row.
#   5. greps against marker names and unit names that do not exist, which return a confident 0.
#
# Every one of those failures produced a NUMBER, not an error. That is the whole problem: they
# are indistinguishable from good news. This script makes each of them impossible rather than
# discouraged.
#
# ⛔ THE CONTRACT: this script prints a count ONLY when it can prove it looked. Everything else
# exits 2 with the word VOID. VOID is not zero and is not a failure of the system under test —
# it means the instrument could not measure, and the caller must not substitute 0.
# ══════════════════════════════════════════════════════════════════════════════════════════
set -u

LOGDIR=${MAI_TAI_LOGDIR:-/var/log/project-mai-tai}
REPO_DIR=${MAI_TAI_REPO_DIR:-/home/trader/project-mai-tai}
OUTDIR=${MAI_TAI_EVIDENCE_OUT:-/tmp/evidence}

die_void() { echo "VOID: $*" >&2; echo "VOID"; exit 2; }

# ── ⛔⭐⭐ THE STATUS TRAVELS IN THE OUTPUT, BECAUSE A PIPE EATS THE OTHER KIND ──────────
# Written down twice, broken twice in one day — the second time while verifying the tool
# built to stop truncation traps:
#   `preflight_v2_restart.sh ... | tail -30; echo "EXIT=$?"`  -> printed EXIT=0 on a gate
#      whose own verdict line read `===> NO-GO`. `$?` was TAIL's status.
#   `evidence.sh acceptance ... | tail -3; echo "exit=$?"`    -> printed exit=0 on a VOID.
# ⇒ That is not a habit problem. `$?` after a pipeline is the LAST command's status, and no
#   amount of remembering fixes a shape that is wrong by default.
#
# So the status stops being something a caller has to collect. It is printed as the FINAL
# stdout line on every exit path — including `die_void` and any unexpected error — which
# means `| tail -N` still shows it, because tail keeps the END.
# ⛔ Do not "tidy" this into an echo at the bottom of main(): an EXIT trap is what makes it
# cover the early-exit paths, and those are exactly the ones that were being misread.
_emit_status() {
  # ⛔ Takes the status EXPLICITLY. Reading `$?` here is only correct when this is the FIRST
  # command in the trap; in a compound trap it reports whatever ran just before it.
  local rc=${1:-$?}
  if [ "$rc" -eq 0 ]; then printf 'EXIT_STATUS=0\n'
  else printf 'EXIT_STATUS=%d  <<< NON-ZERO (0=ok 1=FAIL 2=VOID 3=UNMEASURED)\n' "$rc"; fi
  return "$rc"
}
trap _emit_status EXIT

# run_checked — for anything THIS script shells out to. Takes the status FIRST, emits the
# output second, so the two can never be transposed by a pipe in between.
# ⛔ Never `run_checked cmd | something` expecting `$?` to be cmd's: use the return value,
#    or read the EXIT_STATUS line. That is the whole point.
run_checked() {
  local out rc
  out=$("$@" 2>&1); rc=$?
  printf '%s\n' "$out"
  return "$rc"
}


# ── 1. NEVER TRUNCATE ─────────────────────────────────────────────────────────────────────
# There is deliberately no `head`, no `tail` and no `cut` anywhere below. Full output goes to a
# file and the caller is told the path. A truncated read is a wrong answer wearing a confident
# number, and the only reliable way to stop doing it is to remove the verbs.
_assert_no_truncation() { :; }   # documentation anchor; see the selftest, which greps for them

# ── 2. THE SERVICE MUST EXIST ─────────────────────────────────────────────────────────────
# A typo'd service name would otherwise glob to nothing and count 0.
resolve_service() {
  local svc="$1"
  [ -n "$svc" ] || die_void "no --service given"
  if ! sudo -n test -e "$LOGDIR/$svc.log"; then
    local avail
    avail=$(sudo -n ls "$LOGDIR"/*.log 2>/dev/null | while read -r f; do basename "$f" .log; done | tr '\n' ' ')
    die_void "unknown service '$svc' (no $LOGDIR/$svc.log). Known: ${avail:-<could not list>}"
  fi
  echo "$svc"
}

svc_boot() {  # -> 'YYYY-MM-DD HH:MM:SS' UTC for that service, or empty
  local raw
  raw=$(systemctl show "project-mai-tai-$1.service" -p ActiveEnterTimestamp --value 2>/dev/null)
  [ -n "$raw" ] && date -u -d "$raw" '+%Y-%m-%d %H:%M:%S' 2>/dev/null
}

resolve_since() {  # resolve_since <svc> <when> -> cutoff or empty (= no cutoff)
  local svc="$1" when="${2:-all}" b
  case "$when" in
    all|"") echo "" ;;
    boot)
      b=$(svc_boot "$svc")
      [ -n "$b" ] || die_void "could not read a boot time for project-mai-tai-$svc.service"
      echo "$b" ;;
    *T*)
      # ISO with a T separator -> the space form the log lines use.
      echo "${when/T/ }" ;;
    *)
      die_void "unparseable --since '$when' (use boot | all | 2026-08-20T20:16:46 — no spaces)" ;;
  esac
}

# ── 3. THE MARKER MUST EXIST IN THE SOURCE ────────────────────────────────────────────────
# ⛔ This is the check that would have caught the mirror-leg watch returning 0 for every
# pattern anyone tried while `broker_orders` held 720 rows. A marker that no code can emit
# will read 0 forever, and 0 was the SUCCESS criterion — a broken watch and a passing deploy
# are the same number. So: prove the string is emittable before believing its count.
verify_marker() {
  local marker="$1"
  [ -n "$marker" ] || die_void "no --marker given"
  [ -d "$REPO_DIR/src" ] || die_void "no source tree at $REPO_DIR/src to validate the marker against"
  if grep -rqF -- "$marker" "$REPO_DIR/src" 2>/dev/null; then
    return 0
  fi
  die_void "marker '$marker' does not appear anywhere in $REPO_DIR/src — a typo or a renamed
      marker returns a confident 0 forever. If the string is genuinely emitted from outside
      src/ (a shell script, a library), pass --unchecked-marker and say so in the report."
}

# ── 4. BUILD THE STREAM: ALL ROTATIONS, TIMESTAMPED LINES ONLY, CHRONOLOGICAL ─────────────
build_stream() {  # build_stream <svc> <outfile>
  local svc="$1" out="$2"
  # `zcat -f` reads .gz AND plain rotations — a plain grep silently skips compressed days.
  # The grep keeps ONLY lines that begin with a real timestamp, which is what makes the later
  # substring time-comparison honest: a traceback continuation line can never be dated or
  # counted. The sort makes the stream chronological, because the concatenation is not.
  sudo -n zcat -f -- "$LOGDIR/$svc".log "$LOGDIR/$svc".log-* 2>/dev/null \
    | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},' \
    | sort > "$out"
}

# ── 5. PROVE READABILITY BEFORE EMITTING ANY NUMBER ───────────────────────────────────────
assert_readable() {  # assert_readable <svc> <streamfile>
  local svc="$1" f="$2" n
  # ⛔ NOT `grep -c '' f || echo 0`: grep exits 1 on an EMPTY file, so the `||` fires and the
  # value becomes a two-line string (a zero, a newline, another zero), which then fails every
  # numeric test that follows. That is defect #1 from this file's own header, committed inside
  # the fix for it. Caught by selftest T5, which is why T5 exists.
  n=$(awk 'END{print NR}' "$f" 2>/dev/null)
  if [ "${n:-0}" -eq 0 ] 2>/dev/null; then
    die_void "read 0 timestamped lines from $LOGDIR/$svc.log* — unreadable (these files are
      root:root 0640; run with sudo) or genuinely empty. Either way this is NOT a count of zero."
  fi
  echo "$n"
}

apply_since() {  # apply_since <streamfile> <cutoff> <outfile>
  local f="$1" since="$2" out="$3"
  if [ -z "$since" ]; then cp "$f" "$out"; else
    awk -v s="$since" 'substr($0,1,19) >= s' "$f" > "$out"
  fi
}

select_lines() {  # select_lines <infile> <marker> <pattern> <outfile>
  local f="$1" marker="$2" pattern="$3" out="$4"
  if [ -n "$pattern" ]; then
    grep -F -- "$marker" "$f" | grep -E -- "$pattern" > "$out" || true
  else
    grep -F -- "$marker" "$f" > "$out" || true
  fi
}

# ══════════════════════════════════════════════════════════════════════════════════════════
CMD="${1:-}"; shift || true
SERVICE=""; MARKER=""; PATTERN=""; SINCE="all"; OUT=""; UNCHECKED=0; DENOM=""; MINHITS=1; MFILE=""; MFIND=""; MREPL=""; MTEST=""; MLABEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --service) SERVICE="${2:-}"; shift 2 ;;
    --marker)  MARKER="${2:-}";  shift 2 ;;
    --pattern) PATTERN="${2:-}"; shift 2 ;;
    --since)   SINCE="${2:-}";   shift 2 ;;
    --out)     OUT="${2:-}";     shift 2 ;;
    --denominator) DENOM="${2:-}"; shift 2 ;;
    --min)         MINHITS="${2:-1}"
                   # ⛔⭐ AN UNVALIDATED THRESHOLD IS A PASS SWITCH. `--min -1` made
                   # `HITS >= MINHITS` true at zero successes, so the acceptance check printed
                   # PASS on 0 of 16 opportunities — the exact reading it exists to prevent.
                   case "$MINHITS" in
                     ''|*[!0-9]*) die_void "--min must be a non-negative integer, got '${MINHITS}'" ;;
                   esac
                   [ "$MINHITS" -ge 1 ] 2>/dev/null || die_void "--min must be >= 1; a threshold of 0 passes on zero successes and is not an acceptance test"
                   shift 2 ;;
    --file)    MFILE="${2:-}";  shift 2 ;;
    --find)    MFIND="${2:-}";  shift 2 ;;
    --replace) MREPL="${2:-}";  shift 2 ;;
    --test)    MTEST="${2:-}";  shift 2 ;;
    --label)   MLABEL="${2:-}"; shift 2 ;;
    --unchecked-marker) UNCHECKED=1; shift ;;
    *) die_void "unknown argument '$1'" ;;
  esac
done

mkdir -p "$OUTDIR" 2>/dev/null || true

case "$CMD" in
  mutate)
    # ⛔⭐⭐ B29 — A MUTANT THAT DID NOT APPLY IS NOT A SURVIVING MUTANT.
    #
    # Twice in one session a mutation run reported SURVIVED because the patch silently failed
    # to match — once because a refactor had renamed the anchor, once because the `find` string
    # had never existed. Both times the harness printed a confident "applied" and the result
    # read as coverage the tests did not have.
    #
    # The first time, the fix was a hash check added to that one script. **The second time was
    # a NEW one-off script that did not have it** — so the fix had not actually been made.
    # ⇒ A fix that lives in one script is not a fix. It has to live where the next caller will
    #   reach it. That is why this is a subcommand and not a snippet to copy.
    #
    # Usage:
    #   evidence.sh mutate --file <path> --find '<literal>' --replace '<literal>' \
    #       --test '<shell command>' [--label M1]
    #
    # VERDICTS — four, and the fourth is the reason this exists:
    #   KILLED       exit 0  the test FAILED under the mutant. The test has real coverage.
    #   SURVIVED     exit 1  the test PASSED under the mutant. A genuine gap.
    #   NOT-APPLIED  exit 4  the file did not change. ⛔ NOT survived, NOT killed — the mutant
    #                        never existed, and any verdict drawn from it is void.
    #   VOID         exit 2  bad arguments / unreadable file.
    #
    # ⛔ The file is ALWAYS restored, including on interrupt — a mutation harness that can
    #    leave a mutant on disk is worse than no harness.
    [ -n "$MFILE" ] && [ -n "$MFIND" ] && [ -n "$MTEST" ] || \
      die_void "mutate needs --file, --find and --test (--replace may be empty to delete)"
    [ -r "$MFILE" ] || die_void "cannot read $MFILE"
    _MUT_BACKUP=$(mktemp)
    cp "$MFILE" "$_MUT_BACKUP"
    # shellcheck disable=SC2064
    # ⛔⭐⭐ CAPTURE THE VERDICT BEFORE CLEANUP CLOBBERS IT. This trap ran
    #   cp ...; rm -f ...; _emit_status
    # and `_emit_status` reads `$?` — which by then is the status of the `rm`, not of the script.
    # So a SURVIVING MUTANT exited 1 while printing EXIT_STATUS=0. The mutation harness reported
    # the cleanup's success as the mutation's verdict: a surviving mutant read as a pass, which is
    # the only failure this harness exists to make impossible.
    # shellcheck disable=SC2064
    trap '_MUT_RC=$?; cp "$_MUT_BACKUP" "$MFILE"; rm -f "$_MUT_BACKUP"; _emit_status "$_MUT_RC"' EXIT
    BEFORE=$(md5sum "$MFILE" | awk '{print $1}')
    MFILE="$MFILE" MFIND="$MFIND" MREPL="$MREPL" python3 - <<'PYEOF'
import io, os
p = os.environ["MFILE"]
s = io.open(p, encoding="utf-8").read()
s = s.replace(os.environ["MFIND"], os.environ["MREPL"], 1)
io.open(p, "w", encoding="utf-8", newline="\n").write(s)
PYEOF
    AFTER=$(md5sum "$MFILE" | awk '{print $1}')
    echo "### MUTATE ${MLABEL:-mutant}  file=$MFILE"
    if [ "$BEFORE" = "$AFTER" ]; then
      echo "    md5 $BEFORE unchanged"
      echo "    => ⛔⛔ NOT-APPLIED. The --find string did not match, so no mutant existed."
      echo "       This is NOT 'survived'. Any coverage claim from this run is void."
      echo "       ⛔ Check the anchor against the CURRENT file — a refactor renames anchors"
      echo "          silently, and the patch then fails quietly while printing success."
      exit 4
    fi
    echo "    md5 $BEFORE -> $AFTER  (mutant applied and verified)"
    sh -c "$MTEST" > "$OUTDIR/mutate.out" 2>&1
    TRC=$?
    if [ "$TRC" -ne 0 ]; then
      echo "    test exited $TRC  => KILLED. The test caught it."
      exit 0
    fi
    echo "    test exited 0  => ⛔ SURVIVED. The mutant was not caught — this is a real gap."
    echo "       output: $OUTDIR/mutate.out"
    exit 1
    ;;

  acceptance)
    # ⛔⭐⭐ B28 — A FEATURE THAT NEVER PRODUCED ITS SUCCESS MARKER DID NOT SHIP.
    #
    # Two features were BORN BROKEN on consecutive days and neither was caught for a week:
    #   #688 (2026-08-14) the Webull resting mirror — 720 orders, 0 fills.
    #   #689 (2026-08-13) the protective attach   — succeeded at the venue every time and
    #                     crashed building its own report, so `[WEBULL-PROTECT-ATTACHED]`
    #                     was structurally unreachable and read 0 for its entire life.
    #
    # ⛔ NEITHER DEGRADED. There was no working period to compare against, so every counter
    # read exactly as it always had. **The absence of a CHANGE is not a signal** — which is
    # why "tests pass" and "deployed" both said yes while the feature did nothing.
    #
    # ⇒ The check is not "did it error" but "did it produce the thing it exists to produce,
    #   in production, against a real denominator". Both would have failed this on day one.
    #
    # Usage:
    #   evidence.sh acceptance --service <svc> --marker '<success marker>' \
    #       --denominator '<opportunity marker>' [--min N] [--since boot|all|ISO]
    #
    # VERDICTS — three, never two:
    #   PASS       success marker seen >= --min times.
    #   FAIL       opportunities occurred and the success marker NEVER appeared. The feature
    #              did not ship. This is the #688/#689 case.
    #   UNMEASURED opportunities = 0. Nothing could have succeeded, so nothing is proven.
    #              ⛔ NOT a pass. A quiet window must never retire an acceptance check.
    SERVICE=$(resolve_service "$SERVICE") || exit 2
    [ -n "$DENOM" ] || die_void "acceptance needs --denominator (the opportunity marker)"
    if [ "$UNCHECKED" -eq 0 ]; then
      verify_marker "$MARKER" || exit 2
      verify_marker "$DENOM"  || exit 2
    fi
    CUT=$(resolve_since "$SERVICE" "$SINCE") || exit 2
    S="$OUTDIR/$SERVICE.stream"; build_stream "$SERVICE" "$S"
    TOTAL=$(assert_readable "$SERVICE" "$S") || exit 2
    W="$OUTDIR/$SERVICE.window";  apply_since "$S" "$CUT" "$W"
    select_lines "$W" "$MARKER" "" "$OUTDIR/acc.hit"
    select_lines "$W" "$DENOM"  "" "$OUTDIR/acc.opp"
    HITS=$(awk 'END{print NR}' "$OUTDIR/acc.hit" 2>/dev/null)
    OPPS=$(awk 'END{print NR}' "$OUTDIR/acc.opp" 2>/dev/null)
    echo "### ACCEPTANCE  marker='$MARKER'  denominator='$DENOM'"
    echo "    successes=${HITS:-0}  opportunities=${OPPS:-0}  min=${MINHITS}"
    echo "    window: since='${CUT:-ALL RETAINED}'  (stream ${TOTAL} lines, service ${SERVICE})"
    if [ "${OPPS:-0}" -eq 0 ] 2>/dev/null; then
      echo "    => UNMEASURED. No opportunity occurred, so nothing could have succeeded."
      echo "       ⛔ This is NOT a pass. Re-run over a window that contains the feature's input."
      exit 3
    fi
    if [ "${HITS:-0}" -ge "${MINHITS}" ] 2>/dev/null; then
      echo "    => PASS. The feature produced its success marker in production."
      exit 0
    fi
    echo "    => ⛔⛔ FAIL. ${OPPS} opportunit(y|ies) occurred and the success marker appeared"
    echo "       ${HITS} time(s). On this evidence the feature DID NOT SHIP — it is not"
    echo "       degraded, it has never worked. Check that the marker is REACHABLE before"
    echo "       concluding the feature is merely idle: #689's line sat after a call that"
    echo "       raised on the success path, so no amount of traffic could ever reach it."
    exit 1
    ;;

  verify)
    verify_marker "$MARKER" && echo "OK: '$MARKER' is present in $REPO_DIR/src"
    ;;

  markers)
    SERVICE=$(resolve_service "$SERVICE") || exit 2
    S="$OUTDIR/$SERVICE.stream"; build_stream "$SERVICE" "$S"
    TOTAL=$(assert_readable "$SERVICE" "$S") || exit 2
    echo "# service=$SERVICE timestamped_lines=$TOTAL (all rotations, chronological)"
    grep -oE '\[[A-Z][A-Z0-9-]{3,}\]' "$S" | sort | uniq -c | sort -rn
    ;;

  count|lines)
    SERVICE=$(resolve_service "$SERVICE") || exit 2
    if [ "$UNCHECKED" -eq 0 ]; then verify_marker "$MARKER" || exit 2; fi
    CUT=$(resolve_since "$SERVICE" "$SINCE") || exit 2
    S="$OUTDIR/$SERVICE.stream";  build_stream "$SERVICE" "$S"
    TOTAL=$(assert_readable "$SERVICE" "$S") || exit 2
    W="$OUTDIR/$SERVICE.window";  apply_since "$S" "$CUT" "$W"
    WN=$(awk 'END{print NR}' "$W" 2>/dev/null)
    M="${OUT:-$OUTDIR/$SERVICE.matches}"
    select_lines "$W" "$MARKER" "$PATTERN" "$M"
    N=$(awk 'END{print NR}' "$M" 2>/dev/null)
    if [ "$CMD" = "count" ]; then
      # ⛔ The denominator travels with the numerator, always. A bare count cannot tell a clean
      # window from an empty one, and that ambiguity is what every one of this week's false
      # findings was made of.
      echo "count=$N marker='$MARKER' pattern='${PATTERN:-none}' service=$SERVICE"
      echo "  window_lines=$WN of stream_lines=$TOTAL  since='${CUT:-ALL RETAINED}'"
      echo "  matches_file=$M (complete, untruncated)"
      # ⛔ `cond && echo` as the LAST statement makes the branch exit 1 whenever cond is
      # false. Found by the EXIT_STATUS trap on its first run: every successful non-zero
      # count was exiting 1. Use an explicit if, and end the branch deliberately.
      if [ "${N:-0}" -eq 0 ]; then
        echo "  NOTE: 0 matches, and readability IS proven ($TOTAL lines read) => a real zero."
      fi
    else
      echo "wrote $N complete lines to $M  (window_lines=$WN of stream_lines=$TOTAL, since='${CUT:-ALL RETAINED}')"
    fi
    ;;

  selftest)
    # ⛔⭐⭐ A READER THAT HAS ONLY EVER PRINTED SENSIBLE NUMBERS PROVES NOTHING.
    # Every branch below is aimed at tape whose answer is known independently.
    P=0; F=0
    ok()  { P=$((P+1)); echo "  ✅ $1"; }
    bad() { F=$((F+1)); echo "  ❌ $1"; }
    command_not_found_handle() { bad "command not found: $1"; return 127; }
    SELF="${BASH_SOURCE[0]}"

    echo "T1 — a KNOWN-POSITIVE marker returns a non-zero count"
    r=$(bash "$SELF" count --service schwab-1m-v2 --marker '[V2-DB-SEED-GAP]' 2>&1)
    n=$(echo "$r" | grep -oE '^count=[0-9]+' | cut -d= -f2)
    [ "${n:-0}" -gt 0 ] && ok "counted ${n} (>0)" || bad "known positive returned '${n}': $r"

    echo "T2 — a REAL zero: marker exists in source, never emitted"
    r=$(bash "$SELF" count --service oms --marker '[WEBULL-PROTECT-ATTACHED]' 2>&1)
    echo "$r" | grep -q '^count=0' && ok "count=0 with readability proven" || bad "expected count=0: $r"
    echo "$r" | grep -q 'a real zero' && ok "and it SAYS the zero is real" || bad "did not qualify the zero"

    echo "T3 ★ — a TYPO'd marker must VOID, never return 0"
    r=$(bash "$SELF" count --service oms --marker '[WEBULL-PROTECT-ATACHED]' 2>&1)
    echo "$r" | grep -q 'VOID' && ok "VOID on a marker absent from source" || bad "typo returned: $r"
    echo "$r" | grep -qE '^count=' && bad "a typo produced a count" || ok "and emitted no count at all"

    echo "T4 ★ — an unknown SERVICE must VOID, never return 0"
    r=$(bash "$SELF" count --service oms-typo --marker '[V2-DB-SEED-GAP]' 2>&1)
    echo "$r" | grep -q 'VOID' && ok "VOID on an unknown service" || bad "unknown service returned: $r"

    echo "T5 ★ — UNREADABLE must VOID (empty logdir), never return 0"
    d=$(mktemp -d); : > "$d/fake.log"
    r=$(MAI_TAI_LOGDIR="$d" bash "$SELF" count --service fake --marker '[V2-DB-SEED-GAP]' --unchecked-marker 2>&1)
    echo "$r" | grep -q 'VOID' && ok "VOID on a readable-but-empty stream" || bad "empty stream returned: $r"
    rm -rf "$d"

    echo "T6 — --since narrows the window (boot < all), and both are reported"
    a=$(bash "$SELF" count --service schwab-1m-v2 --marker '[V2-DB-SEED-GAP]' --since all 2>&1 | grep -oE '^count=[0-9]+' | cut -d= -f2)
    b=$(bash "$SELF" count --service schwab-1m-v2 --marker '[V2-DB-SEED-GAP]' --since boot 2>&1 | grep -oE '^count=[0-9]+' | cut -d= -f2)
    [ "${b:-0}" -le "${a:-0}" ] && ok "boot=${b} <= all=${a}" || bad "boot=${b} > all=${a}"

    echo "T7 ★ — a spaced --since must VOID rather than silently mis-window"
    r=$(bash "$SELF" count --service oms --marker '[OMS-P0A-CENSUS]' --since '2026-08-20 20:14:49' 2>&1)
    echo "$r" | grep -q 'VOID' && ok "VOID on a spaced timestamp" || bad "spaced --since was accepted: $r"

    echo "T8 ★ — the stream is CHRONOLOGICAL (the zcat-order defect)"
    bash "$SELF" lines --service schwab-1m-v2 --marker '[V2-DB-SEED-GAP-CENSUS]' --out "$OUTDIR/t8" >/dev/null 2>&1
    if [ -s "$OUTDIR/t8" ]; then
      if LC_ALL=C sort -c "$OUTDIR/t8" 2>/dev/null; then ok "output is in timestamp order"; else bad "output is NOT sorted"; fi
    else bad "T8 had no tape to check"; fi

    echo "T9 * the READING PATH contains no truncating verbs"
    # ⛔⭐⭐ SELF-EXCLUDING BY CONSTRUCTION, NOT BY SENTINEL.
    # This guard matched ITSELF five separate times in one session: its own pattern line,
    # its own success message, a wiring assertion quoting the old code, and twice more when
    # new pipe-trap tests legitimately used `| tail`. Each time the fix was another sentinel
    # comment — i.e. remembering. Same argument as B28 and the EXIT trap: build the property
    # in, do not ask the next person to recall it.
    #
    # ⇒ The invariant is about the READING PATH, not the harness. A test may legitimately
    #   truncate to build a fixture or inspect output; the code that answers questions may
    #   not. So the guard scans only the file ABOVE the selftest case — and since the guard
    #   lives INSIDE selftest, it can never see itself, no matter what it is rewritten to say.
    reading_path=$(awk '/^  selftest\)/{exit} {print}' "$SELF" | grep -vE '^\s*#')
    V1='hea''d'; V2='tai''l'; V3='cut -''c'
    if printf '%s
' "$reading_path" | grep -qE "\| *($V1|$V2) |$V1 -[0-9]|$V2 -[0-9]|$V3"; then
      bad "a truncating verb is present in the reading path"
    else
      ok "no truncating verbs in the reading path"
    fi

    echo "T10 — matches_file is complete: its line count equals the reported count"
    r=$(bash "$SELF" count --service schwab-1m-v2 --marker '[V2-DB-SEED-GAP]' 2>&1)
    n=$(echo "$r" | grep -oE '^count=[0-9]+' | cut -d= -f2)
    f=$(echo "$r" | grep -oE 'matches_file=[^ ]+' | cut -d= -f2)
    fn=$(awk 'END{print NR}' "$f" 2>/dev/null)
    [ "${n:-0}" -eq "${fn:--1}" ] && ok "count=$n equals file lines=$fn" || bad "count=$n but file has $fn"

    echo "T11 * THE PIPE TRAP: a non-zero status must survive being piped"
    # ⛔ The exact shape that misread a live-money gate twice: `cmd | tail -N; echo $?`
    # reports TAIL's status, which is always 0. The status is now in the OUTPUT, so a
    # reader that pipes still sees it — which is the only fix that does not rely on memory.
    piped=$(bash "$SELF" count --service oms --marker '[NO-SUCH-MARKER-XYZ]' 2>&1 | tail -3)  # TRUNCATION_GUARD
    rc_of_tail=$?
    [ "$rc_of_tail" -eq 0 ] && ok "confirms the trap: \$? after the pipe is 0, as always" \
                            || bad "expected the pipe to mask the status; harness assumption wrong"
    echo "$piped" | grep -q 'EXIT_STATUS=2' \
      && ok "but EXIT_STATUS=2 survived the pipe, in the output" \
      || bad "the status did NOT survive the pipe: $piped"
    echo "$piped" | grep -q 'NON-ZERO' && ok "and it is labelled NON-ZERO" || bad "not labelled"

    echo "T12 * a SUCCESSFUL run still ends with EXIT_STATUS=0"
    ok0=$(bash "$SELF" count --service schwab-1m-v2 --marker '[V2-DB-SEED-GAP]' 2>&1 | tail -1)  # TRUNCATION_GUARD
    [ "$ok0" = "EXIT_STATUS=0" ] && ok "clean run ends EXIT_STATUS=0" || bad "got: $ok0"

    echo "T13 * run_checked returns the command's status, not the printf's"
    run_checked false > /dev/null 2>&1 && bad "run_checked false returned success" || ok "run_checked false -> non-zero"
    run_checked true  > /dev/null 2>&1 && ok "run_checked true  -> zero" || bad "run_checked true returned failure"

    echo "T14 * mutate: KILLED / SURVIVED / and the verdict that motivated it"
    subj=$(mktemp)
    printf 'threshold=10\nif [ "$1" -gt "$threshold" ]; then echo HIGH; else echo LOW; fi\n' > "$subj"
    orig_md5=$(md5sum "$subj" | awk '{print $1}')
    tcmd="out=\$(bash $subj 5); [ \"\$out\" = \"LOW\" ]"

    bash "$SELF" mutate --file "$subj" --find 'threshold=10' --replace 'threshold=1' \
         --test "$tcmd" --label T14a > /dev/null 2>&1
    [ $? -eq 0 ] && ok "a caught mutant reports KILLED (exit 0)" || bad "KILLED case wrong"

    bash "$SELF" mutate --file "$subj" --find 'echo HIGH' --replace 'echo VERYHIGH' \
         --test "$tcmd" --label T14b > /dev/null 2>&1
    [ $? -eq 1 ] && ok "an uncaught mutant reports SURVIVED (exit 1)" || bad "SURVIVED case wrong"

    echo "T15 ** THE POINT: a mutant that never applied is NOT 'survived'"
    # ⛔ This is the verdict the whole subcommand exists for. Twice in one session a run
    # reported SURVIVED because the anchor had been renamed by a refactor, and the false
    # coverage read as real. Exit 4 is deliberately distinct from BOTH 0 and 1.
    out14=$(bash "$SELF" mutate --file "$subj" --find 'threshold=999' --replace 'x' \
              --test "$tcmd" --label T15 2>&1)
    rc14=$?
    [ "$rc14" -eq 4 ] && ok "NOT-APPLIED is its own verdict (exit 4)" || bad "got exit $rc14, expected 4"
    echo "$out14" | grep -q 'NOT-APPLIED' && ok "and it says so in words" || bad "no NOT-APPLIED text"
    # ⛔ Match the VERDICT line, not any occurrence of the word — the message deliberately
    # contains "This is NOT 'survived'", and a bare grep flagged its own clarification.
    echo "$out14" | grep -qE '=>.*SURVIVED'       && bad "the VERDICT line says SURVIVED"       || ok "the verdict is never SURVIVED (the word appears only in the disclaimer)"

    echo "T16 * the subject file is RESTORED after every run"
    [ "$(md5sum "$subj" | awk '{print $1}')" = "$orig_md5" ] \
      && ok "file byte-identical after 3 mutations" || bad "the harness left a mutant on disk"
    rm -f "$subj"

    echo
    echo "PASS=$P FAIL=$F"
    [ "$F" -eq 0 ] || exit 1
    ;;

  *)
    die_void "unknown command '${CMD:-<none>}' (count|lines|markers|verify|acceptance|mutate|selftest)"
    ;;
esac
