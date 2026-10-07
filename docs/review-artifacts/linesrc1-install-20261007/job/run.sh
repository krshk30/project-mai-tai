#!/usr/bin/env bash
# [codex] Immutable package entry point. Parent alone stages/activates.
set -euo pipefail
unset TZ
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=/home/trader/project-mai-tai/src
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
IFS= read -r sha < "$here/release.sha256"
[[ "$sha" =~ ^[0-9a-f]{64}$ ]] || exit 1
exec /home/trader/project-mai-tai/.venv/bin/python -B "$here/runner.py" "$sha"
