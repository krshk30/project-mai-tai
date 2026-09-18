#!/usr/bin/env bash
set -euo pipefail

REFUSED=3
APP_OVERVIEW_URL="${APP_OVERVIEW_URL:-http://127.0.0.1:8100/api/overview}"

refuse_authorization() {
  echo "DEPLOY NOT AUTHORISED"
  echo "$1" >&2
  exit "$REFUSED"
}

approval_value() {
  local key="$1"
  local count
  count="$(grep -c "^${key}=" "$APPROVAL_FILE" || true)"
  [[ "$count" == "1" ]] || refuse_authorization "approval record must contain exactly one ${key} field"
  grep "^${key}=" "$APPROVAL_FILE" | cut -d= -f2-
}

REPO_DIR="${1:-}"
EXPECTED_SHA="${2:-}"
APPROVAL_FILE="${3:-}"

[[ -n "$APPROVAL_FILE" && -f "$APPROVAL_FILE" ]] \
  || refuse_authorization "no operator approval record was supplied"
[[ -O "$APPROVAL_FILE" ]] \
  || refuse_authorization "operator approval record is not owned by the invoking user"

if APPROVAL_MODE="$(stat -c '%a' "$APPROVAL_FILE" 2>/dev/null)"; then
  :
else
  APPROVAL_MODE="$(stat -f '%Lp' "$APPROVAL_FILE" 2>/dev/null || true)"
fi
[[ "$APPROVAL_MODE" == "600" || "$APPROVAL_MODE" == "400" ]] \
  || refuse_authorization "operator approval record must have mode 0600 or 0400"

[[ "$(wc -l < "$APPROVAL_FILE" | tr -d ' ')" == "6" ]] \
  || refuse_authorization "operator approval record must contain exactly six lines"
if grep -Ev \
  '^(DEPLOYMENT|AUTHORITY|APPROVED_DATE_ET|APPROVED_AT_UTC|EXPIRES_AT_UTC|EXPECTED_SHA)=' \
  "$APPROVAL_FILE" >/dev/null; then
  refuse_authorization "operator approval record contains an unknown field"
fi

DEPLOYMENT="$(approval_value DEPLOYMENT)"
AUTHORITY="$(approval_value AUTHORITY)"
APPROVED_DATE_ET="$(approval_value APPROVED_DATE_ET)"
APPROVED_AT_UTC="$(approval_value APPROVED_AT_UTC)"
EXPIRES_AT_UTC="$(approval_value EXPIRES_AT_UTC)"
APPROVED_SHA="$(approval_value EXPECTED_SHA)"

[[ "$DEPLOYMENT" == "oms-strategy" ]] \
  || refuse_authorization "approval record is not for the OMS plus strategy deployment"
[[ "$AUTHORITY" == "operator" ]] \
  || refuse_authorization "approval authority is not the operator"
[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] \
  || refuse_authorization "requested target is not a full commit SHA"
[[ "$APPROVED_SHA" == "$EXPECTED_SHA" ]] \
  || refuse_authorization "approval record does not name the requested target SHA"

CURRENT_DATE_ET="$(TZ=America/New_York date +%F)"
CURRENT_TIME_ET="$(TZ=America/New_York date +%H%M)"
[[ "$APPROVED_DATE_ET" == "$CURRENT_DATE_ET" ]] \
  || refuse_authorization "approval record is not for today's ET session"
(( 10#$CURRENT_TIME_ET >= 1605 )) \
  || refuse_authorization "the OMS plus strategy deployment window does not open until 16:05 ET"

NOW_EPOCH="$(date -u +%s)"
if ! APPROVED_EPOCH="$(date -u -d "$APPROVED_AT_UTC" +%s 2>/dev/null)"; then
  refuse_authorization "approval timestamp is not valid ISO-8601 UTC"
fi
if ! EXPIRES_EPOCH="$(date -u -d "$EXPIRES_AT_UTC" +%s 2>/dev/null)"; then
  refuse_authorization "approval expiry is not valid ISO-8601 UTC"
fi
(( APPROVED_EPOCH <= NOW_EPOCH )) \
  || refuse_authorization "approval timestamp is in the future"
(( EXPIRES_EPOCH > NOW_EPOCH )) \
  || refuse_authorization "operator approval has expired"
(( EXPIRES_EPOCH - APPROVED_EPOCH <= 43200 )) \
  || refuse_authorization "operator approval is valid for more than 12 hours"

[[ -d "$REPO_DIR/.git" ]] \
  || refuse_authorization "production repository is missing: $REPO_DIR"
PYTHON_BIN="$REPO_DIR/.venv/bin/python"
PREFLIGHT="$REPO_DIR/src/project_mai_tai/deploy_preflight.py"
OMS_FENCE="$REPO_DIR/ops/preflight/preflight_oms_restart.sh"
DEPLOY="$REPO_DIR/ops/systemd/deploy_service.sh"
for required in "$PYTHON_BIN" "$PREFLIGHT" "$OMS_FENCE" "$DEPLOY"; do
  [[ -x "$required" || "$required" == *.py && -f "$required" ]] \
    || refuse_authorization "required reviewed deploy component is missing: $required"
done

echo "[DEPLOY-AUTHORISED] deployment=$DEPLOYMENT expected_sha=$EXPECTED_SHA expires_at=$EXPIRES_AT_UTC"

set +e
"$PYTHON_BIN" "$PREFLIGHT" --service oms --overview-url "$APP_OVERVIEW_URL"
PREFLIGHT_RC=$?
set -e
if (( PREFLIGHT_RC != 0 )); then
  echo "DEPLOY REFUSED: the one-shot live OMS preflight did not prove a flat, healthy state" >&2
  exit "$PREFLIGHT_RC"
fi

set +e
"$OMS_FENCE"
FENCE_RC=$?
set -e
if (( FENCE_RC != 0 )); then
  echo "DEPLOY REFUSED: the OMS restart fence did not prove zero managed rows and fresh flat broker state" >&2
  exit "$FENCE_RC"
fi

git -C "$REPO_DIR" fetch origin main
REMOTE_SHA="$(git -C "$REPO_DIR" rev-parse origin/main)"
[[ "$REMOTE_SHA" == "$EXPECTED_SHA" ]] \
  || refuse_authorization "origin/main moved after approval: expected $EXPECTED_SHA, found $REMOTE_SHA"

MAI_TAI_EXPECTED_SHA="$EXPECTED_SHA" "$DEPLOY" "$REPO_DIR" main oms

DEPLOYED_SHA="$(git -C "$REPO_DIR" rev-parse HEAD)"
[[ "$DEPLOYED_SHA" == "$EXPECTED_SHA" ]] \
  || { echo "DEPLOY VERIFICATION FAILED: checkout is $DEPLOYED_SHA, expected $EXPECTED_SHA" >&2; exit 1; }

echo "[DEPLOY-COMPLETE] deployment=$DEPLOYMENT sha=$DEPLOYED_SHA"
