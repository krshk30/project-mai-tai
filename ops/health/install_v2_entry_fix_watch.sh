#!/usr/bin/env bash
# Install the repository-owned #644 composition detector into the root-cron location.
# This deliberately does not add or edit the cron schedule; it replaces only the two artifacts
# the already-installed cron executes. Run after the reviewed commit is on the box.
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "REFUSED: run as root" >&2
  exit 1
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source_check="$repo_root/ops/health/v2_entry_fix_watch.py"
source_cron="$repo_root/ops/health/v2_entry_fix_watch_cron.sh"
target_dir=/home/trader/entry_fix_watch
target_check="$target_dir/check.py"
target_cron="$target_dir/watch_cron.sh"

python3 - "$source_check" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY
bash -n "$source_cron"

if ! crontab -l | grep -Fq "$target_cron"; then
  echo "REFUSED: root crontab does not reference $target_cron" >&2
  exit 1
fi

install -d -o trader -g trader -m 0755 "$target_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
if [[ -f "$target_check" ]] && ! cmp -s "$source_check" "$target_check"; then
  cp -a "$target_check" "$target_check.pre-versioned-$stamp"
fi
if [[ -f "$target_cron" ]] && ! cmp -s "$source_cron" "$target_cron"; then
  cp -a "$target_cron" "$target_cron.pre-versioned-$stamp"
fi

install -o root -g root -m 0755 "$source_check" "$target_check"
install -o root -g root -m 0755 "$source_cron" "$target_cron"

cmp "$source_check" "$target_check"
cmp "$source_cron" "$target_cron"
printf 'installed check_sha256=%s cron_sha256=%s cron_schedule_unchanged=1\n' \
  "$(sha256sum "$target_check" | awk '{print $1}')" \
  "$(sha256sum "$target_cron" | awk '{print $1}')"
