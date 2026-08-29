#!/usr/bin/env bash
# Install the repository-owned CW-v2 armed-segment pager into root's crontab.
# This arms only the existing read-only wrapper; it does not restart or reconfigure v2.
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
wrapper="$repo_root/ops/health/armed_segments_cron.sh"
schedule="*/5 10-21 * * 1-5 $wrapper"
begin_marker="# BEGIN project-mai-tai armed-segments pager"
end_marker="# END project-mai-tai armed-segments pager"

test_mode=0
if [[ -n "${MAI_TAI_ARMED_SEGMENTS_INSTALL_TEST_ROOT:-}" ]]; then
  # The end-to-end controls execute this exact installer without touching root's real crontab.
  # A real root invocation may never redirect production bindings through the test seam.
  if [[ "${EUID:-0}" -eq 0 ]]; then
    echo "REFUSED: armed-segments installer test root is unavailable to root" >&2
    exit 1
  fi
  test_mode=1
  backup_dir="$MAI_TAI_ARMED_SEGMENTS_INSTALL_TEST_ROOT/backups"
  PATH="$MAI_TAI_ARMED_SEGMENTS_INSTALL_TEST_ROOT/fake-bin:$PATH"
  export PATH
  effective_uid=0
else
  backup_dir=/var/backups/project-mai-tai/armed-segments
  if ! effective_uid=$(id -u) || [[ ! "$effective_uid" =~ ^[0-9]+$ ]]; then
    echo "REFUSED: could not determine a numeric effective uid" >&2
    exit 1
  fi
fi

if [[ "$effective_uid" -ne 0 ]]; then
  echo "REFUSED: run as root" >&2
  exit 1
fi
if [[ ! -x "$wrapper" ]]; then
  echo "REFUSED: reviewed armed-segments wrapper is not executable: $wrapper" >&2
  exit 1
fi
if ! bash -n "$wrapper"; then
  echo "REFUSED: reviewed armed-segments wrapper does not compile: $wrapper" >&2
  exit 1
fi

if [[ "$test_mode" -eq 1 ]]; then
  if ! mkdir -p "$backup_dir"; then
    echo "REFUSED: could not create armed-segments backup directory: $backup_dir" >&2
    exit 1
  fi
else
  if ! install -d -o root -g root -m 0700 "$backup_dir"; then
    echo "REFUSED: could not create armed-segments backup directory: $backup_dir" >&2
    exit 1
  fi
fi

current_cron=""
next_cron=""
post_cron=""
cron_error=""
rollback_read=""
expected_nonmanaged=""
actual_nonmanaged=""
preimage=""
preimage_present=""
transaction_active=0
cron_write_attempted=0
had_crontab=0

strip_managed_block() {
  local source_file=$1
  local target_file=$2
  awk -v begin="$begin_marker" -v end="$end_marker" '
    $0 == begin { inside=1; next }
    $0 == end { inside=0; next }
    !inside { print }
  ' "$source_file" >"$target_file"
}

verify_block_shape() {
  local candidate=$1
  local begin_count end_count begin_line end_line
  if begin_count=$(grep -Fxc "$begin_marker" "$candidate"); then :
  elif [[ "$?" -eq 1 ]]; then begin_count=0
  else echo "REFUSED: could not inspect armed-segments begin marker" >&2; return 1
  fi
  if end_count=$(grep -Fxc "$end_marker" "$candidate"); then :
  elif [[ "$?" -eq 1 ]]; then end_count=0
  else echo "REFUSED: could not inspect armed-segments end marker" >&2; return 1
  fi
  if [[ "$begin_count" -gt 1 || "$end_count" -gt 1 || "$begin_count" -ne "$end_count" ]]; then
    echo "REFUSED: malformed armed-segments cron block begin=$begin_count end=$end_count" >&2
    return 1
  fi
  if [[ "$begin_count" -eq 1 ]]; then
    begin_line=$(grep -nFx "$begin_marker" "$candidate" | cut -d: -f1)
    end_line=$(grep -nFx "$end_marker" "$candidate" | cut -d: -f1)
    if [[ "$begin_line" -ge "$end_line" ]]; then
      echo "REFUSED: armed-segments cron block markers are out of order" >&2
      return 1
    fi
  fi
}

verify_exactly_one_managed_entry() {
  local candidate=$1
  local count
  if count=$(grep -Fxc "$schedule" "$candidate"); then :
  elif [[ "$?" -eq 1 ]]; then count=0
  else echo "REFUSED: could not inspect armed-segments schedule" >&2; return 1
  fi
  if [[ "$count" -ne 1 ]]; then
    echo "REFUSED: installed root crontab does not contain exactly one armed-segments schedule" >&2
    return 1
  fi
}

finish_armed_segments_install() {
  local status=$1
  local rollback_failed=0
  local rollback_status=0
  trap - EXIT
  set +e

  if [[ "$transaction_active" -eq 1 && "$status" -ne 0 && "$cron_write_attempted" -eq 1 ]]; then
    if [[ "$had_crontab" -eq 1 ]]; then
      crontab "$preimage"
      rollback_status=$?
    else
      crontab -r
      rollback_status=$?
    fi
    if [[ "$rollback_status" -ne 0 ]]; then
      echo "REFUSED: armed-segments rollback could not restore the prior root crontab" >&2
      rollback_failed=1
    else
      rollback_read=$(mktemp)
      if [[ "$had_crontab" -eq 1 ]]; then
        crontab -l >"$rollback_read"
        if [[ "$?" -ne 0 ]] || ! cmp -s "$preimage" "$rollback_read"; then
          echo "REFUSED: armed-segments rollback could not verify the restored crontab" >&2
          rollback_failed=1
        fi
      elif crontab -l >"$rollback_read" 2>/dev/null; then
        echo "REFUSED: armed-segments rollback created a crontab where none existed" >&2
        rollback_failed=1
      fi
    fi
    if [[ "$rollback_failed" -eq 0 ]]; then
      echo "REFUSED: armed-segments install failed after mutation; prior crontab restored" >&2
    else
      echo "REFUSED: armed-segments install failed and rollback was incomplete" >&2
      status=1
    fi
  fi

  [[ -z "$current_cron" ]] || rm -f "$current_cron"
  [[ -z "$next_cron" ]] || rm -f "$next_cron"
  [[ -z "$post_cron" ]] || rm -f "$post_cron"
  [[ -z "$cron_error" ]] || rm -f "$cron_error"
  [[ -z "$rollback_read" ]] || rm -f "$rollback_read"
  [[ -z "$expected_nonmanaged" ]] || rm -f "$expected_nonmanaged"
  [[ -z "$actual_nonmanaged" ]] || rm -f "$actual_nonmanaged"
  exit "$status"
}
trap 'finish_armed_segments_install $?' EXIT

if ! current_cron=$(mktemp) || ! next_cron=$(mktemp) || ! post_cron=$(mktemp) \
  || ! cron_error=$(mktemp) || ! expected_nonmanaged=$(mktemp) \
  || ! actual_nonmanaged=$(mktemp); then
  echo "REFUSED: could not allocate armed-segments crontab transaction files" >&2
  exit 1
fi
if crontab -l >"$current_cron" 2>"$cron_error"; then
  had_crontab=1
elif grep -qi "no crontab" "$cron_error"; then
  had_crontab=0
  : >"$current_cron"
else
  echo "REFUSED: could not read the existing root crontab" >&2
  cat "$cron_error" >&2
  exit 1
fi
if ! verify_block_shape "$current_cron"; then
  exit 1
fi

if ! preimage=$(mktemp "$backup_dir/root.crontab.pre-install.XXXXXXXX"); then
  echo "REFUSED: could not allocate an armed-segments crontab pre-image" >&2
  exit 1
fi
if ! cp -a "$current_cron" "$preimage" || ! cmp -s "$current_cron" "$preimage"; then
  echo "REFUSED: armed-segments crontab pre-image is missing or differs: $preimage" >&2
  exit 1
fi
preimage_present="$preimage.present"
if ! printf '%s\n' "$had_crontab" >"$preimage_present"; then
  echo "REFUSED: could not record whether the root crontab pre-image existed" >&2
  exit 1
fi
if [[ "$test_mode" -eq 0 ]]; then
  if ! chown root:root "$preimage" "$preimage_present" \
    || ! chmod 0600 "$preimage" "$preimage_present"; then
    echo "REFUSED: could not protect the armed-segments crontab pre-image" >&2
    exit 1
  fi
fi

if ! strip_managed_block "$current_cron" "$next_cron"; then
  echo "REFUSED: could not derive proposed root crontab" >&2
  exit 1
fi
if ! printf '%s\n%s\n%s\n' "$begin_marker" "$schedule" "$end_marker" >>"$next_cron"; then
  echo "REFUSED: could not compose proposed armed-segments crontab" >&2
  exit 1
fi
if ! verify_block_shape "$next_cron" || ! verify_exactly_one_managed_entry "$next_cron"; then
  exit 1
fi
if ! strip_managed_block "$current_cron" "$expected_nonmanaged" \
  || ! strip_managed_block "$next_cron" "$actual_nonmanaged" \
  || ! cmp -s "$expected_nonmanaged" "$actual_nonmanaged"; then
  rm -f "$expected_nonmanaged" "$actual_nonmanaged"
  echo "REFUSED: proposed root crontab would alter a non-armed-segments line" >&2
  exit 1
fi

transaction_active=1
cron_write_attempted=1
if ! crontab "$next_cron"; then
  echo "REFUSED: could not install the armed-segments root crontab" >&2
  exit 1
fi
if ! crontab -l >"$post_cron"; then
  echo "REFUSED: could not read back the installed armed-segments root crontab" >&2
  exit 1
fi
if ! verify_block_shape "$post_cron" || ! verify_exactly_one_managed_entry "$post_cron"; then
  exit 1
fi
if ! strip_managed_block "$post_cron" "$actual_nonmanaged" \
  || ! strip_managed_block "$current_cron" "$expected_nonmanaged" \
  || ! cmp -s "$expected_nonmanaged" "$actual_nonmanaged"; then
  rm -f "$expected_nonmanaged" "$actual_nonmanaged"
  echo "REFUSED: installed root crontab changed a non-armed-segments line" >&2
  exit 1
fi

transaction_active=0
printf 'installed schedule="%s" managed_entries=1 preimage=%s restart_required=0\n' \
  "$schedule" "$preimage"
