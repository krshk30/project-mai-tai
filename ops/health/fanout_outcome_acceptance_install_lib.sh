#!/usr/bin/env bash
# Side-effect-free installer helpers. Safe to source from tests; the real installer itself has no
# library-only or exit-zero bypass.

verify_d6_runtime() {
  local runtime_python=$1
  local runtime_cron=$2
  local runtime_check=$3
  local expected_check_sha256=$4
  if [[ ! -x "$runtime_python" ]]; then
    echo "REFUSED: production Python is not executable: $runtime_python" >&2
    return 1
  fi
  "$runtime_python" "$runtime_cron" \
    --acceptance "$runtime_check" \
    --acceptance-sha256 "$expected_check_sha256" \
    --verify-artifact-only >/dev/null
}
