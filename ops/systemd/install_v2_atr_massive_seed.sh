#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
ENV_FILE="${2:-/etc/project-mai-tai/project-mai-tai.env}"
FLAG="MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_MASSIVE_SEED_ENABLED"
UNIT="project-mai-tai-schwab-1m-v2.service"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE_DIR="${MAI_TAI_EVIDENCE_DIR:-/home/trader/restart-evidence}"
SNAPSHOT="$EVIDENCE_DIR/v2-atr-seed-$STAMP-before.json"
REPORT="$EVIDENCE_DIR/v2-atr-seed-$STAMP-report.json"

eastern_hour=$((10#$(TZ=America/New_York date +%H)))
eastern_weekday=$((10#$(TZ=America/New_York date +%u)))
if [[ "$eastern_weekday" -le 5 && "$eastern_hour" -lt 16 ]]; then
  echo "refusing v2 ATR Massive seed installation before 16:00 ET"
  exit 1
fi

key_count="$(sudo awk -F= '$1 == "MAI_TAI_MASSIVE_API_KEY" && length($2) > 0 {n++} END {print n+0}' "$ENV_FILE")"
if [[ "$key_count" != "1" ]]; then
  echo "expected exactly one non-empty MAI_TAI_MASSIVE_API_KEY in $ENV_FILE"
  exit 1
fi

mkdir -p "$EVIDENCE_DIR"
"$REPO_DIR/.venv/bin/python" "$REPO_DIR/ops/health/v2_restart_evidence.py" \
  snapshot --output "$SNAPSHOT"

backup="$ENV_FILE.v2-atr-seed-$STAMP"
sudo cp --preserve=all "$ENV_FILE" "$backup"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
sudo awk -F= -v key="$FLAG" '$1 != key {print}' "$ENV_FILE" > "$tmp"
printf '%s=true\n' "$FLAG" >> "$tmp"
sudo tee "$ENV_FILE" < "$tmp" >/dev/null

flag_count="$(sudo grep -Ec "^${FLAG}=true$" "$ENV_FILE" || true)"
if [[ "$flag_count" != "1" ]]; then
  echo "failed to write exactly one ${FLAG}=true; restore $backup"
  exit 1
fi

alembic_head="$(
  sudo -n -u postgres psql -d project_mai_tai -X -tA \
    -c 'select version_num from alembic_version' | tr -d '[:space:]'
)"
if [[ -z "$alembic_head" ]]; then
  echo "could not read the current Alembic head; restore $backup"
  exit 1
fi

"$REPO_DIR/ops/systemd/deploy_service.sh" "$REPO_DIR" main schwab-1m-v2

pid="$(sudo systemctl show "$UNIT" --property MainPID --value)"
if [[ ! "$pid" =~ ^[1-9][0-9]*$ ]]; then
  echo "v2 unit did not start; restore $backup and restart v2 to roll back"
  exit 1
fi
sudo grep -zFxq "${FLAG}=true" "/proc/$pid/environ"
sudo grep -zEq '^MAI_TAI_MASSIVE_API_KEY=.+$' "/proc/$pid/environ"

"$REPO_DIR/.venv/bin/python" "$REPO_DIR/ops/health/v2_restart_evidence.py" \
  report \
  --snapshot "$SNAPSHOT" \
  --restarted schwab-1m-v2 \
  --expect-flag "${FLAG}=true" \
  --expect-flag 'MAI_TAI_STRATEGY_SCHWAB_1M_V2_FLIP_OWNED_FIRST_ENTRY_ENABLED=true' \
  --expect-flag 'MAI_TAI_STRATEGY_SCHWAB_1M_V2_CONFIRMATION_ACCOUNT_NEUTRAL_DISCOVERY_ENABLED=true' \
  --expect-flag 'MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_V2_RECLAIM_ENABLED=false' \
  --expected-alembic-head "$alembic_head" \
  --no-schema-change \
  --output "$REPORT"

session_day="$(TZ=America/New_York date +%F)"
census="$(sudo grep "\[V2-ATR-SEED-CENSUS\] session=$session_day" /var/log/project-mai-tai/schwab-1m-v2.log | tail -1 || true)"
if [[ -z "$census" ]]; then
  echo "missing current-session [V2-ATR-SEED-CENSUS] after restart"
  exit 1
fi

echo "$census"
echo "V2 ATR MASSIVE SEED INSTALL: PASS pid=$pid snapshot=$SNAPSHOT report=$REPORT backup=$backup"
echo "ROLLBACK: set ${FLAG}=false exactly once, then restart only $UNIT under the same evidence checklist"
