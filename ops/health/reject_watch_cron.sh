#!/usr/bin/env bash
# A7 refusal-provenance alarm wrapper. Read-only; never changes order routing.
set -u

SELFTEST=0
[[ "${1:-}" == "--selftest" ]] && SELFTEST=1
OUT=/home/trader/reject_watch
LOG="$OUT/watch.log"
SEEN="$OUT/paged.seen"
CHECK=/home/trader/project-mai-tai/ops/health/reject_classes.py
PYTHON=/home/trader/project-mai-tai/.venv/bin/python
LOW_ALERT=/home/trader/project-mai-tai/ops/health/low_priority_alerts.py
mkdir -p "$OUT"
touch "$SEEN"
DAY=$(TZ=America/New_York date +%F)
ETMIN=$((10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M')))
ETDOW=$(TZ=America/New_York date '+%u')
if [[ "$SELFTEST" -eq 0 ]]; then
  [[ "$ETDOW" -gt 5 ]] && exit 0
  [[ "$ETMIN" -lt 420 ]] && exit 0
  [[ "$ETMIN" -ge 1200 ]] && exit 0
fi

REPORT=$("$PYTHON" "$CHECK" --days 10 2>"$OUT/stderr.last")
RC=$?
STDERR=$(cat "$OUT/stderr.last" 2>/dev/null)
{
  echo "===== $(TZ=America/New_York date '+%F %H:%M:%S %Z') ====="
  echo "$REPORT"
  [[ -z "$STDERR" ]] || { echo "--- stderr ---"; echo "$STDERR"; }
} >>"$LOG"
{
  echo "# A7 intent-refusal alarm -- last run $(TZ=America/New_York date '+%F %H:%M:%S %Z')"
  echo "# GREEN = no real-money class is new and none has a >=2-day streak. NOT zero refusals."
  echo
  echo "$REPORT"
} >"$OUT/STATUS.txt"

if [[ "$RC" -ne 0 ]] || ! grep -q "^VERDICT reject_alarm" <<<"$REPORT"; then
  if ! grep -qx "broken:$DAY" "$SEEN"; then
    echo "broken:$DAY" >>"$SEEN"
    printf '%s' "rc=$RC -- no verdict produced, so it is NOT reporting clean.
$(tail -4 <<<"$STDERR")" | "$PYTHON" "$LOW_ALERT" \
      --sender reject-watch --title "A7 reject alarm BROKEN" >/dev/null \
      || echo "  ntfy push failed" >>"$LOG"
  fi
  exit 0
fi

grep "^PAGE " <<<"$REPORT" | while IFS= read -r line; do
  sig=$(md5sum <<<"$line" | cut -c1-12)
  grep -qx "$DAY:$sig" "$SEEN" && continue
  echo "$DAY:$sig" >>"$SEEN"
  printf '%s' "${line#PAGE }

Full report: $OUT/STATUS.txt" | "$PYTHON" "$LOW_ALERT" \
    --sender reject-watch --title "Intent refusal class - our defect" >/dev/null \
    || echo "  ntfy push failed" >>"$LOG"
done

if [[ "$SELFTEST" -eq 1 ]]; then
  printf '%s' "selftest $(TZ=America/New_York date '+%H:%M:%S ET')
$(grep '^VERDICT' <<<"$REPORT")" | "$PYTHON" "$LOW_ALERT" \
    --sender reject-watch --title "A7 reject alarm SELFTEST" >/dev/null
  echo "SELFTEST pushed"
fi
exit 0
