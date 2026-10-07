#!/usr/bin/env bash
# Sourced by the attended runner, never a standalone deployment.
# Native UTC systemd output is retained; ET is local to clock/display calls.
window_now() {
  local et_day et_clock
  et_day=$(TZ=America/New_York date +%F)
  et_clock=$(TZ=America/New_York date +%H%M%S)
  [[ $et_day == 2026-10-06 && $et_clock > 160000 ]]
}

v2_gate_now() {
  window_now || return 1
  local clock
  clock=$(TZ=America/New_York date +%H%M%S)
  if [[ $clock < 180000 ]]; then
    "$REPO/ops/preflight/preflight_v2_restart.sh" \
      --clock-override 'Operator 2026-10-06 after-close ruling; after16:00, zero armed, zero rows, both brokers flat' \
      --i-accept-clock
  else
    "$REPO/ops/preflight/preflight_v2_restart.sh"
  fi
}

read_only_retry() {
  local attempt rc
  for attempt in 1 2 3; do
    printf 'READ_ONLY_ATTEMPT attempt=%s/3 STAGE=%s\n' "$attempt" "$STAGE"
    if run "$@"; then rc=0; else rc=$?; fi
    printf 'READ_ONLY_RESULT attempt=%s/3 rc=%s STAGE=%s\n' "$attempt" "$rc" "$STAGE"
    if (( rc == 0 )); then return 0; fi
    if (( rc != 2 || attempt == 3 )); then return "$rc"; fi
    printf 'READ_ONLY_UNREADABLE wait_seconds=60; no policy waiver\n'
    sleep 60
  done
}
