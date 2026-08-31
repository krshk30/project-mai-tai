#!/usr/bin/env bash
# Replace the unversioned scanner-capture cron body while preserving its existing root schedule.
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source_check="$repo_root/ops/health/scanner_capture_check.py"
source_cron="$repo_root/ops/health/scanner_capture_verify_cron.sh"
production_target=/home/trader/scanner_capture_verify_cron.sh
production_backup_dir=/var/backups/project-mai-tai/scanner-capture
schedule="0,30 12-15 * * 1-5 $production_target"

if [[ -n "${MAI_TAI_SCANNER_CAPTURE_INSTALL_TEST_ROOT:-}" ]]; then
  if [[ "${EUID:-0}" -eq 0 ]]; then
    echo "REFUSED: scanner-capture installer test root is unavailable to root" >&2
    exit 1
  fi
  target="$MAI_TAI_SCANNER_CAPTURE_INSTALL_TEST_ROOT/scanner_capture_verify_cron.sh"
  backup_dir="$MAI_TAI_SCANNER_CAPTURE_INSTALL_TEST_ROOT/backups"
  PATH="$MAI_TAI_SCANNER_CAPTURE_INSTALL_TEST_ROOT/fake-bin:$PATH"
  export PATH
  effective_uid=0
else
  target="$production_target"
  backup_dir="$production_backup_dir"
  if ! effective_uid=$(id -u) || [[ ! "$effective_uid" =~ ^[0-9]+$ ]]; then
    echo "REFUSED: could not determine a numeric effective uid" >&2
    exit 1
  fi
fi

if [[ "$effective_uid" -ne 0 ]]; then
  echo "REFUSED: run as root" >&2
  exit 1
fi
if ! bash -n "$source_cron"; then
  echo "REFUSED: reviewed scanner-capture wrapper does not compile: $source_cron" >&2
  exit 1
fi
if ! python3 - "$source_check" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY
then
  echo "REFUSED: reviewed scanner-capture check does not compile: $source_check" >&2
  exit 1
fi
if [[ ! -x "$source_cron" ]]; then
  echo "REFUSED: reviewed scanner-capture wrapper is not executable: $source_cron" >&2
  exit 1
fi

if ! cron_text=$(crontab -l); then
  echo "REFUSED: could not read the existing root crontab" >&2
  exit 1
fi
schedule_count=$(printf '%s\n' "$cron_text" | grep -Fxc "$schedule" || true)
if [[ "$schedule_count" -ne 1 ]]; then
  echo "REFUSED: existing root crontab must contain exactly one scanner-capture schedule" >&2
  exit 1
fi

if ! mkdir -p "$backup_dir"; then
  echo "REFUSED: could not create scanner-capture backup directory: $backup_dir" >&2
  exit 1
fi
if [[ -z "${MAI_TAI_SCANNER_CAPTURE_INSTALL_TEST_ROOT:-}" ]]; then
  chown root:root "$backup_dir"
  chmod 0700 "$backup_dir"
fi
if ! stamp=$(date -u +%Y%m%dT%H%M%SZ) || [[ ! "$stamp" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
  echo "REFUSED: could not derive a unique scanner-capture backup timestamp" >&2
  exit 1
fi

backup=""
target_existed=0
target_needs_install=1
if [[ -f "$target" ]]; then
  target_existed=1
fi
if [[ "$target_existed" -eq 1 ]] && cmp -s "$source_cron" "$target"; then
  target_needs_install=0
elif [[ "$target_existed" -eq 1 ]]; then
  backup="$backup_dir/scanner_capture_verify_cron.sh.pre-versioned-$stamp"
  if [[ -e "$backup" ]] || ! cp -a "$target" "$backup" || ! cmp -s "$target" "$backup"; then
    echo "REFUSED: could not preserve prior scanner-capture wrapper: $backup" >&2
    exit 1
  fi
  if [[ -z "${MAI_TAI_SCANNER_CAPTURE_INSTALL_TEST_ROOT:-}" ]]; then
    chown root:root "$backup"
    chmod 0600 "$backup"
  fi
fi

rollback_required=0
finish_install() {
  local status=$1
  trap - EXIT
  if [[ "$status" -ne 0 && "$rollback_required" -eq 1 ]]; then
    if [[ "$target_existed" -eq 1 && -n "$backup" ]] \
      && cp -a "$backup" "$target" && cmp -s "$backup" "$target"; then
      echo "REFUSED: scanner-capture install failed; prior wrapper restored" >&2
    elif [[ "$target_existed" -eq 0 ]] && rm -f "$target" && [[ ! -e "$target" ]]; then
      echo "REFUSED: scanner-capture install failed; new wrapper removed" >&2
    else
      echo "REFUSED: scanner-capture install failed and rollback was incomplete" >&2
      status=1
    fi
  fi
  exit "$status"
}
trap 'finish_install $?' EXIT

if [[ "$target_needs_install" -eq 1 ]]; then
  rollback_required=1
  if ! install -m 0755 "$source_cron" "$target"; then
    echo "REFUSED: could not install reviewed scanner-capture wrapper" >&2
    exit 1
  fi
  if [[ -z "${MAI_TAI_SCANNER_CAPTURE_INSTALL_TEST_ROOT:-}" ]]; then
    chown root:root "$target"
  fi
fi
if ! cmp -s "$source_cron" "$target"; then
  echo "REFUSED: installed scanner-capture wrapper differs from reviewed source" >&2
  exit 1
fi
if ! post_cron=$(crontab -l); then
  echo "REFUSED: could not read back the scanner-capture root schedule" >&2
  exit 1
fi
post_count=$(printf '%s\n' "$post_cron" | grep -Fxc "$schedule" || true)
if [[ "$post_count" -ne 1 ]]; then
  echo "REFUSED: installed root crontab no longer contains exactly one scanner-capture schedule" >&2
  exit 1
fi
target_sha=$(sha256sum "$target" | awk '{print $1}')
if [[ ! "$target_sha" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "REFUSED: could not hash installed scanner-capture wrapper" >&2
  exit 1
fi
if ! printf 'installed wrapper_sha256=%s schedule="%s" managed_entries=1 backup=%s restart_required=0\n' \
  "$target_sha" "$schedule" "${backup:-none}"
then
  echo "REFUSED: could not print verified scanner-capture install summary" >&2
  exit 1
fi
rollback_required=0
