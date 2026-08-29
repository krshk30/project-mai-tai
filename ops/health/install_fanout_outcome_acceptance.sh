#!/usr/bin/env bash
# Install the reviewed D6 acceptance report and its root-cron runner.
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source_check="$repo_root/ops/health/fanout_outcome_acceptance.py"
source_cron="$repo_root/ops/health/fanout_outcome_acceptance_cron.py"
source_lib="$repo_root/ops/health/fanout_outcome_acceptance_install_lib.sh"
production_target_dir=/home/trader/fanout_outcome_acceptance
production_python_bin=/home/trader/project-mai-tai/.venv/bin/python
if [[ -n "${MAI_TAI_D6_INSTALL_TEST_ROOT:-}" ]]; then
  # The end-to-end test executes this exact installer without writing /home or root's real crontab.
  # A real root invocation may never redirect the production bindings through this test seam.
  if [[ "${EUID:-0}" -eq 0 ]]; then
    echo "REFUSED: D6 installer test root is unavailable to a real root invocation" >&2
    exit 1
  fi
  target_dir="$MAI_TAI_D6_INSTALL_TEST_ROOT/installed"
  python_bin="$MAI_TAI_D6_INSTALL_TEST_ROOT/python"
  PATH="$MAI_TAI_D6_INSTALL_TEST_ROOT/fake-bin:$PATH"
  export PATH
else
  target_dir="$production_target_dir"
  python_bin="$production_python_bin"
fi
target_check="$target_dir/check.py"
target_cron="$target_dir/cron.py"
begin_marker="# BEGIN project-mai-tai D6 outcome acceptance"
end_marker="# END project-mai-tai D6 outcome acceptance"

source "$source_lib"

if ! effective_uid=$(id -u) || [[ ! "$effective_uid" =~ ^[0-9]+$ ]]; then
  echo "REFUSED: could not determine a numeric effective uid" >&2
  exit 1
fi
if ! require_root "$effective_uid"; then
  exit 1
fi

for source in "$source_check" "$source_cron"; do
  if ! python3 - "$source" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY
  then
    echo "REFUSED: reviewed source does not compile: $source" >&2
    exit 1
  fi
done
if ! source_check_sha256=$(sha256sum "$source_check" | awk '{print $1}') \
  || [[ ! "$source_check_sha256" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "REFUSED: could not hash reviewed D6 acceptance source: $source_check" >&2
  exit 1
fi
cron_line="17 4,5,6 * * 2-6 $python_bin $target_cron --acceptance $target_check --acceptance-sha256 $source_check_sha256 --out-dir $target_dir >> $target_dir/cron.log 2>&1"

if ! install -d -o trader -g trader -m 0755 "$target_dir"; then
  echo "REFUSED: could not create D6 target directory: $target_dir" >&2
  exit 1
fi
if ! stamp=$(date -u +%Y%m%dT%H%M%SZ) || [[ ! "$stamp" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
  echo "REFUSED: could not derive a unique D6 backup timestamp" >&2
  exit 1
fi

check_existed=0
cron_existed=0
check_needs_install=1
cron_needs_install=1
check_backup=""
cron_backup=""
current_cron=""
next_cron=""
cron_error=""
rollback_cron=""
install_transaction_active=0
cron_write_attempted=0

finish_d6_install() {
  local status=$1
  local rollback_failed=0
  trap - EXIT

  if [[ "$install_transaction_active" -eq 1 && "$status" -ne 0 ]]; then
    if [[ "$cron_write_attempted" -eq 1 && -n "$current_cron" && -f "$current_cron" ]]; then
      if ! crontab "$current_cron"; then
        echo "REFUSED: D6 install rollback could not restore the prior root crontab" >&2
        rollback_failed=1
      elif ! rollback_cron=$(mktemp) \
        || ! crontab -l >"$rollback_cron" \
        || ! cmp -s "$current_cron" "$rollback_cron"; then
        echo "REFUSED: D6 install rollback could not verify the restored root crontab" >&2
        rollback_failed=1
      fi
    fi
    if [[ "$check_needs_install" -eq 1 ]]; then
      if [[ "$check_existed" -eq 1 ]]; then
        if ! cp -a "$check_backup" "$target_check"; then
          echo "REFUSED: D6 install rollback could not restore prior acceptance bytes" >&2
          rollback_failed=1
        elif ! cmp -s "$check_backup" "$target_check"; then
          echo "REFUSED: D6 install rollback could not verify prior acceptance bytes" >&2
          rollback_failed=1
        fi
      elif ! rm -f "$target_check" || [[ -e "$target_check" ]]; then
        echo "REFUSED: D6 install rollback could not remove the new acceptance artifact" >&2
        rollback_failed=1
      fi
    fi
    if [[ "$cron_needs_install" -eq 1 ]]; then
      if [[ "$cron_existed" -eq 1 ]]; then
        if ! cp -a "$cron_backup" "$target_cron"; then
          echo "REFUSED: D6 install rollback could not restore prior cron-runner bytes" >&2
          rollback_failed=1
        elif ! cmp -s "$cron_backup" "$target_cron"; then
          echo "REFUSED: D6 install rollback could not verify prior cron-runner bytes" >&2
          rollback_failed=1
        fi
      elif ! rm -f "$target_cron" || [[ -e "$target_cron" ]]; then
        echo "REFUSED: D6 install rollback could not remove the new cron runner" >&2
        rollback_failed=1
      fi
    fi
    if [[ "$rollback_failed" -eq 0 ]]; then
      echo "REFUSED: D6 install failed after target mutation; prior artifacts and root crontab restored" >&2
    else
      echo "REFUSED: D6 install failed after target mutation and rollback was incomplete" >&2
      status=1
    fi
  fi

  [[ -z "$current_cron" ]] || rm -f "$current_cron"
  [[ -z "$next_cron" ]] || rm -f "$next_cron"
  [[ -z "$cron_error" ]] || rm -f "$cron_error"
  [[ -z "$rollback_cron" ]] || rm -f "$rollback_cron"
  exit "$status"
}
trap 'finish_d6_install $?' EXIT

if [[ -f "$target_check" ]]; then
  check_existed=1
fi
if [[ "$check_existed" -eq 1 ]] && cmp -s "$source_check" "$target_check"; then
  check_needs_install=0
elif [[ "$check_existed" -eq 1 ]]; then
  check_backup="$target_check.pre-versioned-$stamp"
  if [[ -e "$check_backup" ]]; then
    echo "REFUSED: D6 acceptance backup path already exists: $check_backup" >&2
    exit 1
  fi
  if ! cp -a "$target_check" "$check_backup"; then
    echo "REFUSED: could not preserve prior D6 acceptance: $check_backup" >&2
    exit 1
  fi
  if [[ ! -f "$check_backup" ]] || ! cmp -s "$target_check" "$check_backup"; then
    echo "REFUSED: prior D6 acceptance backup is missing or differs: $check_backup" >&2
    exit 1
  fi
fi
if [[ -f "$target_cron" ]]; then
  cron_existed=1
fi
if [[ "$cron_existed" -eq 1 ]] && cmp -s "$source_cron" "$target_cron"; then
  cron_needs_install=0
elif [[ "$cron_existed" -eq 1 ]]; then
  cron_backup="$target_cron.pre-versioned-$stamp"
  if [[ -e "$cron_backup" ]]; then
    echo "REFUSED: D6 cron-runner backup path already exists: $cron_backup" >&2
    exit 1
  fi
  if ! cp -a "$target_cron" "$cron_backup"; then
    echo "REFUSED: could not preserve prior D6 cron runner: $cron_backup" >&2
    exit 1
  fi
  if [[ ! -f "$cron_backup" ]] || ! cmp -s "$target_cron" "$cron_backup"; then
    echo "REFUSED: prior D6 cron runner backup is missing or differs: $cron_backup" >&2
    exit 1
  fi
fi
install_transaction_active=1
if [[ "$check_needs_install" -eq 1 ]]; then
  if ! install -o root -g root -m 0755 "$source_check" "$target_check"; then
    echo "REFUSED: could not install reviewed D6 acceptance: $target_check" >&2
    exit 1
  fi
fi
if [[ "$cron_needs_install" -eq 1 ]]; then
  if ! install -o root -g root -m 0755 "$source_cron" "$target_cron"; then
    echo "REFUSED: could not install reviewed D6 cron runner: $target_cron" >&2
    exit 1
  fi
fi
if ! verify_d6_installed_copy "$source_check" "$target_check"; then
  exit 1
fi
if ! verify_d6_installed_copy "$source_cron" "$target_cron"; then
  exit 1
fi
if ! verify_d6_runtime "$python_bin" "$target_cron" "$target_check" "$source_check_sha256" "$target_dir"; then
  exit 1
fi

if ! current_cron=$(mktemp) || ! next_cron=$(mktemp) || ! cron_error=$(mktemp); then
  echo "REFUSED: could not allocate D6 root-crontab transaction files" >&2
  exit 1
fi
if ! crontab -l >"$current_cron" 2>"$cron_error"; then
  if ! grep -qi "no crontab" "$cron_error"; then
    echo "REFUSED: could not read the existing root crontab" >&2
    cat "$cron_error" >&2
    exit 1
  fi
fi
if ! verify_d6_existing_cron_block "$begin_marker" "$end_marker" "$current_cron"; then
  exit 1
fi
if ! awk -v begin="$begin_marker" -v end="$end_marker" '
  $0 == begin { inside=1; next }
  $0 == end { inside=0; next }
  !inside { print }
' "$current_cron" >"$next_cron"; then
  echo "REFUSED: could not derive proposed root crontab" >&2
  exit 1
fi
# The box cron is UTC. In EDT, 04:17 is the first attempt and 05:17/06:17 are retries. In EST,
# 04:17 is 23:17 on the prior ET day and deduplicates its already-graded session; 05:17 is the
# first attempt and 06:17 the one retry. cron.py deduplicates every completed result.
if ! printf '%s\n%s\n%s\n' \
  "$begin_marker" \
  "$cron_line" \
  "$end_marker" >>"$next_cron"; then
  echo "REFUSED: could not compose proposed D6 root crontab" >&2
  exit 1
fi
if ! verify_d6_nonmanaged_cron_preserved \
  "$begin_marker" "$end_marker" "$current_cron" "$next_cron"; then
  exit 1
fi
if ! verify_d6_existing_cron_block "$begin_marker" "$end_marker" "$next_cron"; then
  echo "REFUSED: proposed root crontab does not contain one valid D6 block" >&2
  exit 1
fi
if ! proposed_cron=$(cat "$next_cron"); then
  echo "REFUSED: proposed root crontab is unreadable" >&2
  exit 1
fi
if ! verify_exactly_one_d6_schedule "$cron_line" "$proposed_cron"; then
  echo "REFUSED: proposed root crontab does not contain the reviewed D6 schedule" >&2
  exit 1
fi
cron_write_attempted=1
if ! crontab "$next_cron"; then
  echo "REFUSED: could not install the reviewed D6 root crontab" >&2
  exit 1
fi

if ! installed_cron=$(crontab -l); then
  echo "REFUSED: could not confirm the installed D6 root crontab" >&2
  exit 1
fi
if ! verify_exactly_one_d6_schedule "$cron_line" "$installed_cron"; then
  exit 1
fi

if ! installed_check_sha256=$(sha256sum "$target_check" | awk '{print $1}') \
  || [[ ! "$installed_check_sha256" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "REFUSED: could not verify the installed D6 acceptance hash" >&2
  exit 1
fi
if ! installed_cron_sha256=$(sha256sum "$target_cron" | awk '{print $1}') \
  || [[ ! "$installed_cron_sha256" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "REFUSED: could not verify the installed D6 cron-runner hash" >&2
  exit 1
fi
if ! printf 'REQUIRED BEFORE CHECKOUT ADVANCE: run the installed cron once as root to seed STATUS: sudo %s %s --acceptance %s --acceptance-sha256 %s --out-dir %s\n' \
  "$python_bin" "$target_cron" "$target_check" "$source_check_sha256" "$target_dir"
then
  echo "REFUSED: could not print the required D6 seed command" >&2
  exit 1
fi
if ! printf 'WARNING: fleet-health check #5 pages RED until a current D6 SUCCESS exists; a NONPASS seed remains RED until the first 00:17 ET success\n'; then
  echo "REFUSED: could not print the D6 fleet-health warning" >&2
  exit 1
fi
if ! printf 'installed check_sha256=%s cron_sha256=%s schedule="%s" restart_required=0\n' \
  "$installed_check_sha256" \
  "$installed_cron_sha256" \
  "$cron_line"; then
  echo "REFUSED: could not print the verified D6 install summary" >&2
  exit 1
fi
install_transaction_active=0
