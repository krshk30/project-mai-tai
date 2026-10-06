#!/usr/bin/env python3
"""Systemd fallback: guard death can stop only momentum paper."""
from datetime import UTC, datetime
from pathlib import Path

from daily_guard import ET, load_guard
from project_mai_tai.settings import Settings
from redis import Redis

guard = load_guard()
now = datetime.now(UTC)
output = Path("/home/trader/after-hours") / now.astimezone(ET).date().isoformat() / "option-a-daily" / now.strftime("failure-%Y%m%dT%H%M%S%fZ")
output.mkdir(parents=True, mode=0o700)
settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
client = Redis.from_url(settings.redis_url, decode_responses=True,
                        socket_timeout=5, socket_connect_timeout=5)
try:
    raise SystemExit(guard.stop_paper("daily_guard_process_died", client, settings,
                                     output / "option-a-guard.jsonl"))
finally:
    client.close()
