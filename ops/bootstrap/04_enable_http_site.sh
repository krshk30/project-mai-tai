#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
SITE_SRC="$REPO_DIR/ops/nginx/project-mai-tai.live.http.conf"
SITE_DST="/etc/nginx/sites-available/project-mai-tai.live.conf"
SITE_LINK="/etc/nginx/sites-enabled/project-mai-tai.live.conf"

sudo cp "$SITE_SRC" "$SITE_DST"

if [[ -L "$SITE_LINK" || -e "$SITE_LINK" ]]; then
  sudo rm -f "$SITE_LINK"
fi

sudo ln -s "$SITE_DST" "$SITE_LINK"

if [[ -L /etc/nginx/sites-enabled/default || -e /etc/nginx/sites-enabled/default ]]; then
  sudo rm -f /etc/nginx/sites-enabled/default
fi

if command -v ufw >/dev/null 2>&1; then
  sudo ufw allow 'Nginx Full' >/dev/null 2>&1 || true
fi

sudo nginx -t
sudo systemctl reload nginx
