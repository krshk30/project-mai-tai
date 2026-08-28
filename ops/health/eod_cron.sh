#!/bin/bash
# END-OF-SESSION COUNTS -- fires after v2's 18:00 ET entry window shuts.
# ⛔ 18:05 ET, not 16:05: v2 trades to 18:00, so a 16:xx reading is still mid-session.
# ET-guarded HERE because CRON_TZ is ignored on this box. Read-only.
set -u
OUT=/home/trader/entry_fix_watch
DAY=$(TZ=America/New_York date +%F)
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))
ETDOW=$(TZ=America/New_York date '+%u')
# Plain guard. A cron that silently never runs is this project's recurring ops failure, so the
# condition stays readable rather than clever. 1085 = 18:05 ET, 1145 = 19:05 ET.
if [ "${1:-}" != "--force" ]; then
  [ "$ETDOW" -gt 5 ] && exit 0
  [ "$ETMIN" -lt 1085 ] && exit 0
  [ "$ETMIN" -ge 1145 ] && exit 0
  [ -f "$OUT/eod_${DAY}.txt" ] && exit 0     # already produced today -- fire once
fi
REPORT="$OUT/eod_${DAY}.txt"
/home/trader/project-mai-tai/.venv/bin/python "$OUT/eod_counts.py" --day "$DAY" > "$REPORT" 2>"$OUT/eod_stderr.last"
V=$(grep -m1 "^VERDICT eod" "$REPORT" || echo "VERDICT eod MISSING -- the report did not complete")
{ echo "--- $DAY ---"; cat "$REPORT"; [ -s "$OUT/eod_stderr.last" ] && { echo "--- stderr ---"; cat "$OUT/eod_stderr.last"; }; } >> "$OUT/eod.log"
curl -s -m 20 -H "Title: v2 end-of-session counts $DAY" -H "Priority: default" \
     -d "$V

Full report: $REPORT
These are RESULTS (post-18:00). Mid-session readings are not." \
     "https://ntfy.sh/mai-tai-preopen-28806a5a97b7" >/dev/null || echo "ntfy push failed" >> "$OUT/eod.log"
