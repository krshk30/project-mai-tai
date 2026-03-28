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
