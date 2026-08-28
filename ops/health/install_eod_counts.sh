#!/usr/bin/env bash
# Install the repository-owned end-of-session report into the root-cron location.
# The existing cron wrapper and schedule remain untouched; this replaces only eod_counts.py.
set -euo pipefail

verify_cron_wrapper() {
  local wrapper=$1
  if [[ ! -f "$wrapper" ]]; then
    echo "REFUSED: installed cron wrapper is missing: $wrapper" >&2
    return 1
  fi
  if ! grep -Eq '(^|[/"[:space:]])eod_counts\.py(["[:space:]]|$)' "$wrapper"; then
    echo "REFUSED: installed cron wrapper does not execute eod_counts.py: $wrapper" >&2
    return 1
  fi
}

main() {
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "REFUSED: run as root" >&2
  exit 1
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source_report="$repo_root/ops/health/eod_counts.py"
target_dir=/home/trader/entry_fix_watch
target_report="$target_dir/eod_counts.py"
target_cron="$target_dir/eod_cron.sh"

python3 - "$source_report" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY

# The declaration grants permission; this live check can only veto. A repository file must not
# silently create a new root schedule or install into a location cron does not execute.
if ! crontab -l | grep -Fq "$target_cron"; then
  echo "REFUSED: root crontab does not reference $target_cron" >&2
  exit 1
fi
verify_cron_wrapper "$target_cron"

install -d -o trader -g trader -m 0755 "$target_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
if [[ -f "$target_report" ]] && ! cmp -s "$source_report" "$target_report"; then
  cp -a "$target_report" "$target_report.pre-versioned-$stamp"
fi

install -o root -g root -m 0755 "$source_report" "$target_report"
cmp "$source_report" "$target_report"
printf 'installed report=%s sha256=%s owner=root:root mode=0755 cron_schedule_unchanged=1\n' \
  "$target_report" "$(sha256sum "$target_report" | awk '{print $1}')"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
