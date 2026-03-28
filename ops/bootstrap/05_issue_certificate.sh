#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <email>"
  exit 1
fi

EMAIL="$1"

sudo certbot --nginx \
  --non-interactive \
  --agree-tos \
  --email "$EMAIL" \
  -d project-mai-tai.live \
  -d www.project-mai-tai.live
