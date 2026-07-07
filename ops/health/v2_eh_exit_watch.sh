#!/bin/bash
# Durable watcher: first NEW v2 extended-hours managed exit -> ntfy, DB-classified.
# EH = exit outside 09:30-16:00 ET. RTH market exits are correct -> skipped.
LOG=/var/log/project-mai-tai/oms.log
TOPIC=mai-tai-preopen-28806a5a97b7
STATE=/home/trader/v2_eh_exit_watch.state
SENTINEL=/home/trader/v2_eh_exit_watch.hit
URL=$(sudo grep -E '^MAI_TAI_DATABASE_URL=' /etc/project-mai-tai/project-mai-tai.env | head -1 | cut -d= -f2-)
PW=$(echo "$URL" | sed -E 's|^[^:]+://[^:]+:([^@]+)@.*|\1|')
US=$(echo "$URL" | sed -E 's|^[^:]+://([^:]+):.*|\1|')
q(){ PGPASSWORD="$PW" psql -tA -U "$US" -h localhost -d project_mai_tai -c "$1" 2>/dev/null; }
base=$(sudo grep -c "OMS-V2-MANAGED-EXIT" "$LOG" 2>/dev/null || echo 0)
echo "$(date -u +%FT%TZ) armed base=$base pid=$$" >> "$STATE"
while true; do
  cur=$(sudo grep -c "OMS-V2-MANAGED-EXIT" "$LOG" 2>/dev/null || echo 0)
  if [ "$cur" -gt "$base" ]; then
    eth=$((10#$(TZ=America/New_York date +%H%M)))
    lineno=$(sudo grep -n "OMS-V2-MANAGED-EXIT" "$LOG" | tail -1 | cut -d: -f1)
    line=$(sudo sed -n "${lineno}p" "$LOG")
    sym=$(echo "$line" | grep -oP 'sym=\K[A-Z0-9.]+')
    if [ "$eth" -ge 930 ] && [ "$eth" -lt 1600 ]; then
      base=$cur; echo "$(date -u +%FT%TZ) RTH-exit skipped sym=$sym eth=$eth" >> "$STATE"; continue
    fi
    sleep 6   # let the broker order + fill land in the DB
    row=$(q "SELECT order_type||'|'||status||'|'||coalesce(payload->>'session','-') FROM broker_orders WHERE symbol='$sym' AND client_order_id LIKE 'schwab_1m_v2-%close%' AND submitted_at::date=CURRENT_DATE ORDER BY submitted_at DESC LIMIT 1;")
    otype=$(echo "$row" | cut -d'|' -f1); ostat=$(echo "$row" | cut -d'|' -f2); osess=$(echo "$row" | cut -d'|' -f3)
    if [ "$otype" = "limit" ] && [ "$ostat" = "filled" ] && { [ "$osess" = "AM" ] || [ "$osess" = "PM" ]; }; then
      cls="FIXED — limit+session=$osess, FILLED (#390 PROOF)"; pr="high"
    elif [ "$otype" = "limit" ] && { [ "$osess" = "AM" ] || [ "$osess" = "PM" ]; }; then
      cls="FIXED-routing (limit+$osess) but status=$ostat — watch for stuck"; pr="high"
    elif [ "$otype" = "market" ]; then
      cls="REGRESSION — market-in-EH (order_type=$otype status=$ostat)"; pr="urgent"
    else
      cls="EH exit fired sym=$sym — DB row='$row' (inspect)"; pr="high"
    fi
    printf '%s\nsym=%s db=[%s]\ncls=%s\n' "$line" "$sym" "$row" "$cls" > "$SENTINEL"
    curl -s -H "Title: v2 EH-EXIT $sym — $cls" -H "Priority: $pr" -H "Tags: rotating_light" -d "$line" "https://ntfy.sh/$TOPIC" >/dev/null
    echo "$(date -u +%FT%TZ) HIT sym=$sym eth=$eth cls=$cls" >> "$STATE"
    exit 0
  fi
  sleep 15
done
