#!/bin/bash
# First-live-session watcher for the v2 confirmed-window (CW) ruleset (PRs #408/#409/#411).
# Alerts ONCE per event class the first time it fires after arming, so the operator sees
# the first CW entry / exit / flip-signal live on ntfy. Byte-offset reads: each pass scans
# only the NEW log bytes since the last pass (+8KB overlap so a boundary-split line is
# never lost), so it never re-greps the ~500MB logs (respects the no-heavy-CPU-in-RTH
# rule). Read-only. Retire after the first session confirms all paths fire.
#
# Usage:
#   v2_cw_first_session_watch.sh --arm     # capture baseline offsets at the flag flip
#   v2_cw_first_session_watch.sh --watch   # arm + loop (self-retiring); run at the flip
#   v2_cw_first_session_watch.sh           # one check pass (for cron use)
#
# ops/health/ = versioned source of truth for fleet monitors.
set -u
# Env-overridable (defaults are the live paths/topic); overrides exist for self-test only.
TOPIC=${CW_TOPIC:-mai-tai-preopen-28806a5a97b7}
V2LOG=${CW_V2LOG:-/var/log/project-mai-tai/schwab-1m-v2.log}
OMSLOG=${CW_OMSLOG:-/var/log/project-mai-tai/oms.log}
DIR=${CW_DIR:-/home/trader/v2_cw_watch}
OVER=8192
NCLASSES=6
mkdir -p "$DIR"

size() { sudo stat -c %s "$1" 2>/dev/null || echo 0; }

arm() {
  size "$V2LOG" > "$DIR/off.v2"
  size "$OMSLOG" > "$DIR/off.oms"
  rm -f "$DIR"/*.hit
  : > "$DIR/armed"
  echo "$(date -u +%FT%TZ) ARMED v2=$(cat "$DIR/off.v2") oms=$(cat "$DIR/off.oms")" >> "$DIR/arm.log"
}

# Emit the new (overlapped) bytes of a log to $DIR/chunk.$tag and advance its offset.
new_chunk() {
  local tag=$1 log=$2 o s start
  o=$(cat "$DIR/off.$tag" 2>/dev/null || echo 0)
  s=$(size "$log")
  [ "$s" -lt "$o" ] && o=0                       # log rotated/truncated -> restart
  start=$(( o > OVER ? o - OVER : 0 ))
  if [ "$s" -gt "$start" ]; then
    sudo tail -c +$((start + 1)) "$log" > "$DIR/chunk.$tag" 2>/dev/null || : > "$DIR/chunk.$tag"
  else
    : > "$DIR/chunk.$tag"
  fi
  echo "$s" > "$DIR/off.$tag"
}

# name|chunktag|pattern|ntfy-title|priority|tag
CLASSES=(
 "ENTRY|v2|\[V2-CW\].*ENTER break=|v2 CW FIRST ENTRY (wait-3 break)|high|green_circle"
 "FLIPSIG|v2|emitted v2_cw_flip signal|v2 CW flip signal emitted (strategy)|default|repeat"
 "FLIPARM|oms|\[OMS-V2-CW\] flip pending armed|v2 CW flip received by OMS (Route C OK)|default|link"
 "TARGET|oms|oms_v2_managed_exit:CW_TARGET|v2 CW exit: +2% TARGET|high|dart"
 "HARD|oms|oms_v2_managed_exit:CW_HARD_STOP|v2 CW exit: -5% HARD STOP|urgent|octagonal_sign"
 "FLIPEXIT|oms|oms_v2_managed_exit:CW_FLIP|v2 CW exit: bar-close FLIP|high|repeat"
)

check_once() {
  new_chunk v2 "$V2LOG"
  new_chunk oms "$OMSLOG"
  local c name ctag pat title pri tag hit line
  for c in "${CLASSES[@]}"; do
    IFS='|' read -r name ctag pat title pri tag <<< "$c"
    hit="$DIR/$name.hit"
    [ -f "$hit" ] && continue
    line=$(grep -aE "$pat" "$DIR/chunk.$ctag" 2>/dev/null | tail -1)
    if [ -n "$line" ]; then
      curl -s -H "Title: $title" -H "Priority: $pri" -H "Tags: $tag" \
           -d "$line" "https://ntfy.sh/$TOPIC" >/dev/null
      : > "$hit"
      echo "$(date -u +%FT%TZ) HIT $name :: $line" >> "$DIR/hit.log"
    fi
  done
}

hits() { ls "$DIR"/*.hit 2>/dev/null | wc -l; }

case "${1:-}" in
  --arm)
    arm; echo "armed v2=$(cat "$DIR/off.v2") oms=$(cat "$DIR/off.oms")"; exit 0 ;;
  --watch)
    arm
    curl -s -H "Title: v2 CW watch ARMED" -H "Tags: eyes" \
         -d "Watching first-session CW entry/exit/flip on ntfy. Will report each once." \
         "https://ntfy.sh/$TOPIC" >/dev/null
    end=$(( $(date +%s) + 13 * 3600 ))          # cover one RTH+EH session
    while [ "$(date +%s)" -lt "$end" ]; do
      check_once
      if [ "$(hits)" -ge "$NCLASSES" ]; then
        curl -s -H "Title: v2 CW watch COMPLETE" -H "Tags: white_check_mark" \
             -d "All $NCLASSES CW event classes seen this session. Watch retiring." \
             "https://ntfy.sh/$TOPIC" >/dev/null
        break
      fi
      sleep 20
    done
    echo "$(date -u +%FT%TZ) WATCH exit hits=$(hits)" >> "$DIR/arm.log"
    exit 0 ;;
  *)
    [ -f "$DIR/armed" ] || arm                   # first bare run auto-arms
    check_once; exit 0 ;;
esac
