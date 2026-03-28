#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
VENV_DIR="$REPO_DIR/.venv"
APP_ENV_FILE="/etc/project-mai-tai/project-mai-tai.env"

if [[ ! -f "$APP_ENV_FILE" ]]; then
  echo "missing env file: $APP_ENV_FILE"
  exit 1
fi

cd "$REPO_DIR"

sudo -u trader "$PYTHON_BIN" -m venv "$VENV_DIR"
sudo -u trader "$VENV_DIR/bin/python" -m pip install --upgrade pip
sudo -u trader "$VENV_DIR/bin/pip" install -e "$REPO_DIR"

set -a
# shellcheck disable=SC1090
source "$APP_ENV_FILE"
set +a

sudo -u trader "$VENV_DIR/bin/alembic" upgrade head
