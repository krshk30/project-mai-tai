#!/usr/bin/env bash
# Source from the pinned /home/trader/preopen.sh after its restart-evidence report.

preopen_record_restart_evidence() {
  local evidence_rc="$1" report="$2" current_output="$3" final_count final_call
  final_count="$(printf '%s\n' "$current_output" | grep -c '^Final call: ' || true)"
  final_call="$(printf '%s\n' "$current_output" | sed -n 's/^Final call: \([^;]*\);.*/\1/p')"
  if [[ "$final_count" != 1 ]]; then
    final_call=""
  fi
  case "$evidence_rc:$final_call" in
    '0:PASS') pass "restart evidence PASS: $report" ;;
    '0:EXPECTED BY DESIGN') pass "restart evidence EXPECTED BY DESIGN (N/A remains N/A): $report" ;;
    '1:REAL FAILURE') fail "restart evidence REAL FAILURE; inspect $report" ;;
    '2:UNKNOWN'|'2:')
      printf 'UNKNOWN: restart evidence could not establish readiness; inspect %s\n' "$report"
      unknowns=$((unknowns + 1))
      ;;
    *)
      printf 'UNKNOWN: restart evidence rc=%s disagrees with current final call=%s; inspect %s\n' \
        "$evidence_rc" "${final_call:-missing}" "$report"
      unknowns=$((unknowns + 1))
      ;;
  esac
}

preopen_record_expected_flags() {
  local flags_rc="$1" catalog="$2" current_output="$3" final_count final_call
  final_count="$(printf '%s\n' "$current_output" | grep -c '^Final call: ' || true)"
  final_call="$(printf '%s\n' "$current_output" | sed -n 's/^Final call: \([^;]*\);.*/\1/p')"
  if [[ "$final_count" != 1 ]]; then
    final_call=""
  fi
  case "$flags_rc:$final_call" in
    '0:PASS') pass "running flags match reviewed catalog: $catalog" ;;
    '1:REAL FAILURE') fail "running flag mismatch; inspect $catalog and the flag check output" ;;
    '2:UNKNOWN')
      printf 'UNKNOWN: running flags could not be read; inspect %s and the flag check output\n' "$catalog"
      unknowns=$((unknowns + 1))
      ;;
    *)
      printf 'UNKNOWN: flag check rc=%s disagrees with current final call=%s; inspect %s\n' \
        "$flags_rc" "${final_call:-missing}" "$catalog"
      unknowns=$((unknowns + 1))
      ;;
  esac
}

preopen_check_expected_flags() {
  local python_bin="$1" checker="$2" catalog="$3" flags_output flags_rc
  printf '\n=== RUNNING FLAG CONTRACT ===\n'
  flags_output="$("$python_bin" "$checker" --catalog "$catalog" 2>&1)"
  flags_rc=$?
  printf '%s\n' "$flags_output"
  preopen_record_expected_flags "$flags_rc" "$catalog" "$flags_output"
}

preopen_final_verdict() {
  if (( failures > 0 )); then
    printf 'BLOCKED: REAL FAILURE in %d check group(s); unknown groups=%d.\n' "$failures" "$unknowns"
    return 1
  fi
  if (( unknowns > 0 )); then
    printf 'BLOCKED: UNKNOWN in %d check group(s); readiness is not established.\n' "$unknowns"
    return 2
  fi
  printf 'PASS: daily pre-open gate is green; any N/A row remains EXPECTED BY DESIGN.\n'
  return 0
}
