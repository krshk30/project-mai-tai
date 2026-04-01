#!/usr/bin/env bash
set -euo pipefail

sudo mkdir -p /etc/project-mai-tai
sudo mkdir -p /etc/redis/redis.conf.d
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

sudo tee /etc/redis/redis.conf.d/99-project-mai-tai.conf >/dev/null <<'EOF'
# Project Mai Tai uses Redis as a transient cache and event bus.
# Durable state lives in Postgres and dashboard snapshots.
#
# These limits prevent Redis from growing until the kernel OOM killer
# terminates it during restart or steady-state operation.
maxmemory 512mb
maxmemory-policy allkeys-lru

# Do not persist Redis cache state to disk. Oversized RDB files caused
# restart-time OOMs while loading old snapshot-batch history.
save ""
appendonly no
EOF
