#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <username>"
  exit 1
fi

USERNAME="$1"

sudo htpasswd -c /etc/nginx/.htpasswd-project-mai-tai "$USERNAME"
sudo chmod 640 /etc/nginx/.htpasswd-project-mai-tai
sudo chown root:www-data /etc/nginx/.htpasswd-project-mai-tai
