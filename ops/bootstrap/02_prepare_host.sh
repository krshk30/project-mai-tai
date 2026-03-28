#!/usr/bin/env bash
set -euo pipefail

sudo mkdir -p /etc/project-mai-tai
sudo mkdir -p /var/log/project-mai-tai
sudo mkdir -p /var/lib/project-mai-tai
sudo mkdir -p /home/trader/project-mai-tai

sudo chown -R trader:trader /var/log/project-mai-tai
sudo chown -R trader:trader /var/lib/project-mai-tai
sudo chown -R trader:trader /home/trader/project-mai-tai

sudo chmod 755 /var/log/project-mai-tai
sudo chmod 755 /var/lib/project-mai-tai
sudo chmod 755 /home/trader/project-mai-tai
sudo chmod 700 /etc/project-mai-tai

if [[ ! -f /etc/project-mai-tai/project-mai-tai.env ]]; then
  sudo cp /home/trader/project-mai-tai/ops/env/project-mai-tai.env.example /etc/project-mai-tai/project-mai-tai.env
  sudo chown root:root /etc/project-mai-tai/project-mai-tai.env
  sudo chmod 600 /etc/project-mai-tai/project-mai-tai.env
fi
