#!/usr/bin/env bash
# Install the reviewed EOD wrapper without changing its existing root-cron schedule.
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "REFUSED: run as root" >&2
  exit 1
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source_cron="$repo_root/ops/health/eod_cron.sh"
target_dir=/home/trader/entry_fix_watch
target_cron="$target_dir/eod_cron.sh"
target_check="$target_dir/eod_counts.py"
env_file=/etc/project-mai-tai/eod-watch.env

bash -n "$source_cron"
if [[ ! -x "$target_check" ]]; then
  echo "REFUSED: reviewed eod_counts.py is not installed executable at $target_check" >&2
  exit 1
fi
if ! crontab -l | grep -Fq "$target_cron"; then
  echo "REFUSED: root crontab does not reference $target_cron" >&2
  exit 1
fi
if [[ -z ${MAI_TAI_NTFY_URL:-} && ! -r "$env_file" ]]; then
  echo "REFUSED: MAI_TAI_NTFY_URL is unset and $env_file does not exist" >&2
  exit 1
fi

install -d -o trader -g trader -m 0755 "$target_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
if [[ -f "$target_cron" ]] && ! cmp -s "$source_cron" "$target_cron"; then
  cp -a "$target_cron" "$target_cron.pre-versioned-$stamp"
fi
install -o root -g root -m 0755 "$source_cron" "$target_cron"
cmp "$source_cron" "$target_cron"

if [[ -n ${MAI_TAI_NTFY_URL:-} ]]; then
  install -d -o root -g root -m 0755 "$(dirname "$env_file")"
  env_tmp=$(mktemp "$(dirname "$env_file")/.eod-watch.env.XXXXXX")
  trap 'rm -f "$env_tmp"' EXIT
  printf 'MAI_TAI_NTFY_URL=%q\n' "$MAI_TAI_NTFY_URL" > "$env_tmp"
  install -o root -g root -m 0600 "$env_tmp" "$env_file"
fi

printf 'installed wrapper_sha256=%s env=%s schedule_unchanged=1 restart_required=0\n' \
  "$(sha256sum "$target_cron" | awk '{print $1}')" "$env_file"
