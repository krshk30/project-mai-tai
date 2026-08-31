#!/usr/bin/env bash
# Scanner-confirmed capture watch. The root crontab invokes this existing box path every 30 min.
# It compares the current ET window with the prior five matching weekdays at the same ET cutoff.
# LOW_VOLUME states observations only; it never diagnoses the capture writer as broken.
set -u

CHECK=/home/trader/project-mai-tai/ops/health/scanner_capture_check.py
PYTHON=/home/trader/project-mai-tai/.venv/bin/python
ENV_FILE=/etc/project-mai-tai/project-mai-tai.env
LOG=/home/trader/scanner_capture/cron.log
DEFAULT_NTFY_URL=https://ntfy.sh/mai-tai-preopen-28806a5a97b7
NTFY_URL=$DEFAULT_NTFY_URL

mkdir -p "$(dirname "$LOG")"
stamp=$(TZ=America/New_York date '+%F %H:%M:%S %Z')

notify() {
  local title=$1
  local priority=$2
  local body=$3
  curl --fail-with-body -sS -H "Title: $title" -H "Priority: $priority" \
    -d "$body" "$NTFY_URL" >/dev/null
}

if [[ ! -r "$ENV_FILE" ]]; then
  out="SCANNER_CAPTURE COULD_NOT_TELL database_read=FAILED row_count=UNMEASURED cause=NOT_DETERMINED reason=env_unreadable"
  printf '%s rc=2 %s\n' "$stamp" "$out" >>"$LOG"
  notify "scanner capture CHECK UNKNOWN" high "$out" || exit 2
  exit 2
fi

set -a
# shellcheck disable=SC1090 -- production path is intentionally fixed and root-owned.
if ! source "$ENV_FILE"; then
  set +a
  out="SCANNER_CAPTURE COULD_NOT_TELL database_read=FAILED row_count=UNMEASURED cause=NOT_DETERMINED reason=env_parse_failed"
  printf '%s rc=2 %s\n' "$stamp" "$out" >>"$LOG"
  notify "scanner capture CHECK UNKNOWN" high "$out" || exit 2
  exit 2
fi
set +a
NTFY_URL=${MAI_TAI_NTFY_URL:-$DEFAULT_NTFY_URL}

out=$("$PYTHON" "$CHECK" 2>&1)
rc=$?
printf '%s rc=%s %s\n' "$stamp" "$rc" "$out" >>"$LOG"

case "$rc" in
  0) exit 0 ;;
  1)
    notify "scanner capture LOW VOLUME" high "$out" || exit 2
    exit 1
    ;;
  2)
    notify "scanner capture CHECK UNKNOWN" high "$out" || exit 2
    exit 2
    ;;
  *)
    body="SCANNER_CAPTURE COULD_NOT_TELL checker_exit=$rc row_count=UNMEASURED cause=NOT_DETERMINED"
    printf '%s rc=2 %s\n' "$stamp" "$body" >>"$LOG"
    notify "scanner capture CHECK ERROR" high "$body" || exit 2
    exit 2
    ;;
esac
