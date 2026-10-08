#!/usr/bin/env bash
set -euo pipefail
unset TZ
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 "$(dirname "$0")/runner.py" --job "$(dirname "$0")"
