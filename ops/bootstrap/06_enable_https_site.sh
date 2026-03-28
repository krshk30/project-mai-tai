#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
SITE_SRC="$REPO_DIR/ops/nginx/project-mai-tai.live.https.conf"
SITE_DST="/etc/nginx/sites-available/project-mai-tai.live.conf"

sudo cp "$SITE_SRC" "$SITE_DST"
sudo nginx -t
sudo systemctl reload nginx
