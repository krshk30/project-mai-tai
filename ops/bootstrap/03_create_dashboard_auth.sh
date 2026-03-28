#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 <username> [password]"
  exit 1
fi

USERNAME="$1"
PASSWORD="${2:-}"

if [[ -n "$PASSWORD" ]]; then
  sudo htpasswd -bc /etc/nginx/.htpasswd-project-mai-tai "$USERNAME" "$PASSWORD"
else
  sudo htpasswd -c /etc/nginx/.htpasswd-project-mai-tai "$USERNAME"
fi

sudo chmod 640 /etc/nginx/.htpasswd-project-mai-tai
sudo chown root:www-data /etc/nginx/.htpasswd-project-mai-tai
