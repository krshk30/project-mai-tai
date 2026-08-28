#!/usr/bin/env bash
# Side-effect-free installer helpers. Safe to source from tests; the real installer itself has no
# library-only or exit-zero bypass.

verify_d6_runtime() {
  local runtime_python=$1
  local runtime_cron=$2
  local runtime_check=$3
  local expected_check_sha256=$4
  local runtime_out_dir=$5
  if [[ ! -x "$runtime_python" ]]; then
    echo "REFUSED: production Python is not executable: $runtime_python" >&2
    return 1
  fi
  "$runtime_python" "$runtime_cron" \
    --acceptance "$runtime_check" \
    --acceptance-sha256 "$expected_check_sha256" \
    --out-dir "$runtime_out_dir" \
    --verify-artifact-only >/dev/null
}

require_root() {
  local effective_uid=$1
  if [[ "$effective_uid" -ne 0 ]]; then
    echo "REFUSED: run as root" >&2
    return 1
  fi
}

verify_d6_installed_copy() {
  local reviewed_source=$1
  local installed_target=$2
  if ! cmp "$reviewed_source" "$installed_target"; then
    echo "REFUSED: installed D6 bytes differ from reviewed source: $installed_target" >&2
    return 1
  fi
}

verify_exactly_one_d6_schedule() {
  local expected_line=$1
  local installed_crontab=$2
  if [[ $(grep -Fxc "$expected_line" <<<"$installed_crontab" || true) -ne 1 ]]; then
    echo "REFUSED: installed root crontab does not contain exactly one D6 schedule" >&2
    return 1
  fi
}
