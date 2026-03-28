#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

sudo apt-get update
sudo apt-get install -y \
  nginx \
  certbot \
  python3-certbot-nginx \
  apache2-utils \
  postgresql \
  postgresql-contrib \
  redis-server \
  python3-venv \
  python3-pip

sudo systemctl enable nginx
sudo systemctl enable postgresql
sudo systemctl enable redis-server
