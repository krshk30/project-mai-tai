#!/usr/bin/env bash
# New F4 integration; historical G2 candidate and old install helpers stay frozen.
option_a_gateway_rebaseline_and_dryrun() (
  set -euo pipefail
  [[ $# -eq 6 || $# -eq 7 ]]
  local task_python="$1" task_root="$2" task_request="$3" task_sha="$4"
  local task_backup="$5" task_receipt="$6" task_helper task_pending
  local task_scope="${7:---apply-reviewed-afterclose}"
  [[ "$task_scope" == --apply-reviewed-afterclose || "$task_scope" == --apply-reviewed-recorded-test ]]
  task_helper="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/guard_poststep.py"
  [[ ! -e "$task_receipt" && -d "$(dirname -- "$task_receipt")" ]]
  task_pending="$(mktemp "${task_receipt}.pending.XXXXXX")"
  trap 'rm -f -- "$task_pending"' EXIT
  local task_rc=0
  "$task_python" -B "$task_helper" --root "$task_root" \
    --request "$task_request" --request-sha256 "$task_sha" --backup-dir "$task_backup" \
    "$task_scope" > "$task_pending" || task_rc=$?
  # Retain failure evidence too; a nonzero dry-run never authorizes continuation.
  ln -- "$task_pending" "$task_receipt"
  exit "$task_rc"
)
