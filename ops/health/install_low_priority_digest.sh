#!/usr/bin/env bash
# Install the DST-safe 20:00 ET digest without disturbing unrelated root cron entries.
set -euo pipefail

repo_root=${ALERT_SPLIT_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
router="$repo_root/ops/health/low_priority_alerts.py"
digest="$repo_root/ops/health/low_priority_digest_cron.sh"
begin='# BEGIN mai-tai-low-priority-digest'
end='# END mai-tai-low-priority-digest'

[[ -f $router && -f $digest ]] || { echo "REFUSED: alert routing sources missing" >&2; exit 1; }
python3 - "$router" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY
bash -n "$digest"
router_sha=$(sha256sum "$router" | awk '{print $1}')
digest_sha=$(sha256sum "$digest" | awk '{print $1}')
[[ $router_sha =~ ^[a-f0-9]{64}$ && $digest_sha =~ ^[a-f0-9]{64}$ ]] ||
  { echo "REFUSED: could not hash alert routing sources" >&2; exit 1; }

log=${ALERT_SPLIT_DIGEST_LOG:-/var/log/project-mai-tai/low-priority-digest.log}
curl_bin=${ALERT_SPLIT_CURL:-curl}
low_url='https://ntfy.sh/mai-tai-routine-112964cc8f26787132a29538'
line="0 0,1 * * * [ \"\$(TZ=America/New_York date +\\%H\\%M)\" = 2000 ] || exit 0; if [ \"\$(sha256sum $router 2>/dev/null | cut -d' ' -f1)\" != \"$router_sha\" ]; then reason=$router; elif [ \"\$(sha256sum $digest 2>/dev/null | cut -d' ' -f1)\" != \"$digest_sha\" ]; then reason=$digest; else reason=; fi; if [ -n \"\$reason\" ]; then echo \"\$(date) digest guard refused: \$reason\" >> $log; $curl_bin -sS --fail-with-body --connect-timeout 10 --max-time 30 -H 'Title: Mai Tai digest guard refused' -H 'Priority: low' --data-binary \"digest guard refused: \$reason\" $low_url >> $log 2>&1; else $digest >> $log 2>&1; fi"
if [[ ${1:-} == --print-cron ]]; then
  printf '%s\n%s\n%s\n' "$begin" "$line" "$end"
  exit 0
fi
[[ ${1:-} == --install ]] || { echo "usage: $0 --print-cron|--install" >&2; exit 2; }
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "REFUSED: run as root" >&2; exit 1; }
[[ -n ${EXPECTED_SHA:-} ]] || { echo "REFUSED: EXPECTED_SHA is required" >&2; exit 1; }
[[ $(git -C "$repo_root" rev-parse HEAD) == "$EXPECTED_SHA" ]] ||
  { echo "REFUSED: deployed checkout does not match EXPECTED_SHA" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
crontab -l > "$tmp/before" || { echo "REFUSED: root crontab unreadable" >&2; exit 1; }
[[ $(grep -Fxc "$begin" "$tmp/before" || true) -le 1 ]] ||
  { echo "REFUSED: duplicate digest begin marker" >&2; exit 1; }
[[ $(grep -Fxc "$end" "$tmp/before" || true) -le 1 ]] ||
  { echo "REFUSED: duplicate digest end marker" >&2; exit 1; }
[[ $(grep -Fxc "$begin" "$tmp/before" || true) == $(grep -Fxc "$end" "$tmp/before" || true) ]] ||
  { echo "REFUSED: incomplete digest cron block" >&2; exit 1; }
awk -v begin="$begin" -v end="$end" '
  $0 == begin {if (skip) bad=1; skip=1; next}
  $0 == end {if (!skip) bad=1; skip=0; next}
  !skip {print}
  END {if (skip || bad) exit 1}
' "$tmp/before" > "$tmp/after"
printf '%s\n%s\n%s\n' "$begin" "$line" "$end" >> "$tmp/after"
crontab "$tmp/after" || { echo "REFUSED: proposed digest cron rejected" >&2; exit 1; }
if ! crontab -l > "$tmp/readback" || ! cmp -s "$tmp/after" "$tmp/readback"; then
  crontab "$tmp/before" || true
  echo "REFUSED: digest cron readback differed; prior crontab restored" >&2
  exit 1
fi
printf 'installed digest_sha256=%s router_sha256=%s root_cron=verified\n' \
  "$digest_sha" "$router_sha"
