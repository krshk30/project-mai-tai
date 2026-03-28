#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
"$REPO_DIR/ops/systemd/install_units.sh" "$REPO_DIR"
