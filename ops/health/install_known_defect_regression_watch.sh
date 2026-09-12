#!/bin/bash
# Install only this watch's dedicated cron.d file. No shared crontab is read or rewritten.
set -euo pipefail

if [ "${KNOWN_DEFECT_INSTALL_TEST_MODE:-0}" = "1" ]; then
  source_cron="${KNOWN_DEFECT_INSTALL_SOURCE:?test source is required}"
  target_cron="${KNOWN_DEFECT_INSTALL_TARGET:?test target is required}"
else
  if [ "$(id -u)" -ne 0 ]; then
    echo "REFUSED: install the known-defect watch as root" >&2
    exit 2
  fi
  source_cron="/home/trader/project-mai-tai/ops/health/known_defect_regression_watch.cron"
  target_cron="/etc/cron.d/project-mai-tai-known-defect-regression-watch"
fi

expected='*/5 * * * * root /home/trader/project-mai-tai/ops/health/known_defect_regression_watch_cron.sh >> /var/log/project-mai-tai/known-defect-regression-watch.log 2>&1'
if [ ! -r "$source_cron" ] || [ "$(grep -Fxc "$expected" "$source_cron")" -ne 1 ]; then
  echo "REFUSED: reviewed cron source is missing or does not contain exactly one schedule" >&2
  exit 2
fi

target_dir="$(dirname "$target_cron")"
mkdir -p "$target_dir"
temporary="$(mktemp "$target_dir/.known-defect-watch.XXXXXXXX")"
trap 'rm -f "$temporary"' EXIT
if [ "${KNOWN_DEFECT_INSTALL_TEST_MODE:-0}" = "1" ]; then
  install -m 0644 "$source_cron" "$temporary"
else
  install -o root -g root -m 0644 "$source_cron" "$temporary"
fi
mv "$temporary" "$target_cron"
trap - EXIT

if ! cmp -s "$source_cron" "$target_cron"; then
  echo "REFUSED: installed cron bytes do not match the reviewed source" >&2
  exit 2
fi
printf 'installed target=%s sha256=%s shared_crontab_untouched=1 restart_required=0\n' \
  "$target_cron" "$(sha256sum "$target_cron" | awk '{print $1}')"
