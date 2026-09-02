#!/bin/bash
# SEED-EXPOSURE WATCH — the 04:00-11:00 ET window the pre-open routine cannot cover.
#
# ⛔⭐⭐ WHY THIS EXISTS SEPARATELY FROM `preopen_readiness_cron.sh`. That routine is ET-guarded to
# ~09:12, but seed exposure is created from 04:00 ET and at EVERY WATCHLIST ADD. Measured
# 2026-08-19: BIVI was already exposed at 06:50, and VRAX truncated at 09:13 — both OUTSIDE the
# 09:12 slot. A clean line there is a DAILY RECORD, not a clean session. This is the watch.
#
# ⛔ CRON_TZ IS IGNORED ON THIS BOX. The crontab hours are UTC; the ET window is enforced HERE, which
# is also automatically DST-correct. Weekday is likewise checked in ET, not by the crontab.
#
#   Crontab:  */5 8-16 * * *  /home/trader/project-mai-tai/ops/health/seed_exposure_cron.sh
#             (08-16 UTC spans 04:00-11:00 ET in BOTH EDT and EST; the guard below trims it)
#
# ⛔ EXIT-CODE DISCIPLINE, same as #738: an UNKNOWN MUST NEVER DECAY INTO A PASS.
#     0 = swept, no exposure      -> quiet
#     1 = EXPOSURE found          -> AMBER
#     2 = CANNOT SEE (refused)    -> RED   (NOT better than 1)
#     missing tool / crash        -> RED   (never quiet)
#
# ⛔⭐ ALERT ON CHANGE, NOT ON EVERY RUN. At 5-minute cadence this fires ~84 times per window. An
# alert every time is noise, and a noisy detector is a disabled detector — the same reasoning that
# made the LIVE_LOCKED audit compare by MEANING rather than by string. State is kept per ET session
# so a NEW exposure always alerts and a standing one does not re-alert.
set -u

REPO=/home/trader/project-mai-tai
OUT=/home/trader/seed_exposure_out
mkdir -p "$OUT"
STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
TODAY=$(TZ=America/New_York date +%F)
ETH=$(TZ=America/New_York date '+%H')
DOW=$(TZ=America/New_York date '+%u')      # 1-5 = Mon-Fri

# ---- ET window guard: 04:00-10:59 ET, weekdays only ----
if [ "$DOW" -gt 5 ]; then exit 0; fi
if [ "$ETH" -lt 4 ] || [ "$ETH" -gt 10 ]; then exit 0; fi

# ---- NYSE full-closure holidays (half-days NOT skipped). UPDATE ANNUALLY. ----
HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
HOLIDAYS_2027="2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24"
case " $HOLIDAYS_2026 $HOLIDAYS_2027 " in
  *" $TODAY "*) exit 0 ;;
esac

OUTFILE="$OUT/latest.txt"
SEEN="$OUT/alerted-$TODAY.txt"      # per-ET-session alert state; old files are harmless
touch "$SEEN"

# ⛔⭐⭐ THE DETECTOR NEEDS THE SERVICE ENV — WITHOUT IT THIS WATCH IS BLIND.
# The detector reads Postgres (MAI_TAI_DATABASE_URL) and Redis, and both live in the service env
# file. This wrapper never sourced it, so every single run refused with
#   ⛔ CANNOT SEE — REFUSING: no DSN: pass --dsn or set MAI_TAI_DATABASE_URL
# on EVERY trading session from 2026-08-20 through 2026-09-02 — 10 days with the 04:00-11:00 ET
# window unwatched, and a standing RED that also dragged the 09:12 readiness verdict to AMBER daily.
# The exit-code discipline worked exactly as designed (it refused rather than decaying into a PASS);
# what was missing was the env. `bar_gap_watch_cron.sh` has always sourced it the same way, under
# the same `set -u`, every 5 minutes — this is the established pattern, not a new one.
# ⛔ Runs as ROOT from ROOT's crontab; the env file is root-readable only.
ENV_FILE=/etc/project-mai-tai/project-mai-tai.env

if [ ! -r "$ENV_FILE" ]; then
  # ⛔ An unreadable env is CANNOT SEE, never quiet — the same rule as a missing tool. This is the
  # branch that would have caught the defect above on day one instead of on day ten.
  echo "seed-exposure: service env NOT READABLE at $ENV_FILE (must run as root from root's crontab)" \
    > "$OUTFILE"
  CODE=2
elif [ -x "$REPO/.venv/bin/python" ] && [ -f "$REPO/scripts/seed_exposure_detector.py" ]; then
  # Sourced INSIDE the subshell so the wrapper's own `set -u` string handling below is untouched.
  ( set -a
    # shellcheck disable=SC1091
    . "$ENV_FILE"
    set +a
    cd "$REPO" && ./.venv/bin/python scripts/seed_exposure_detector.py --assert-constants ) \
    > "$OUTFILE" 2>&1
  CODE=$?
else
  echo "seed-exposure detector NOT FOUND at $REPO/scripts/seed_exposure_detector.py" > "$OUTFILE"
  CODE=2   # ⛔ a missing tool is CANNOT SEE, never quiet
fi

echo "$STAMP  exit=$CODE" >> "$OUT/cron.log"

case "$CODE" in
  0) exit 0 ;;                                   # quiet: swept, nothing exposed
  1) LEVEL=AMBER ;;
  2) LEVEL=RED   ;;
  *) LEVEL=RED   ;;                              # crash
esac

# ---- alert on CHANGE only ----
# The key is the detector's own verdict line plus the exposed symbol names, so a NEW name always
# produces a NEW key and therefore a NEW alert; an unchanged picture stays quiet.
KEY=$(grep -E '^\s+(VERDICT|⛔ CANNOT SEE)' "$OUTFILE" | head -1)
KEY="$KEY|$(grep -oE '^\s+[A-Z0-9]+ +window=' "$OUTFILE" | awk '{print $1}' | sort | tr '\n' ',')"
if grep -Fqx "$KEY" "$SEEN" 2>/dev/null; then
  exit 0                                          # already alerted this session, unchanged
fi
echo "$KEY" >> "$SEEN"

SUMMARY=$(grep -E '^\s+(VERDICT|⛔ CANNOT SEE)' "$OUTFILE" | head -1)
if [ -x /home/trader/preopen_alert.sh ]; then
  /home/trader/preopen_alert.sh "$LEVEL" "SEED-EXPOSURE $SUMMARY" "$OUTFILE"
else
  echo "$STAMP  $LEVEL  SEED-EXPOSURE $SUMMARY  (no alert transport)" >> "$OUT/cron.log"
fi
exit 0
