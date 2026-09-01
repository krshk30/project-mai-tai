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
NTFY="https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
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
    curl -s -m 20 -H "Title: A7 reject alarm BROKEN" -H "Priority: high" \
      -d "rc=$RC -- no verdict produced, so it is NOT reporting clean.
$(tail -4 <<<"$STDERR")" "$NTFY" >/dev/null
  fi
  exit 0
fi

grep "^PAGE " <<<"$REPORT" | while IFS= read -r line; do
  sig=$(md5sum <<<"$line" | cut -c1-12)
  grep -qx "$DAY:$sig" "$SEEN" && continue
  echo "$DAY:$sig" >>"$SEEN"
  curl -s -m 20 -H "Title: Intent refusal class - our defect" -H "Priority: default" \
    -d "${line#PAGE }

Full report: $OUT/STATUS.txt" "$NTFY" >/dev/null \
    || echo "  ntfy push failed" >>"$LOG"
done

if [[ "$SELFTEST" -eq 1 ]]; then
  curl -s -m 20 -H "Title: A7 reject alarm SELFTEST" \
    -d "selftest $(TZ=America/New_York date '+%H:%M:%S ET')
$(grep '^VERDICT' <<<"$REPORT")" "$NTFY" >/dev/null
  echo "SELFTEST pushed"
fi
exit 0
