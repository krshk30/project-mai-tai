"""Bounded read-only Redis checkpoint; no snapshot payload access."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import redis
from project_mai_tai.settings import Settings

OWNERS = {"strategy-engine", "schwab-1m-v2", "orb", "orb-schwab", "momentum-paper"}
FIELDS = OWNERS | {"_migration_complete", "_last_applied_id"}
STREAMS = ("heartbeats", "market-data", "market-data-subscriptions", "order-events",
           "runtime-controls", "snapshot-batches", "strategy-intents", "strategy-state",
           "strategy-state-isolated")
MEMORY_LIMIT = 1_600_000_000


def collect():
    settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True,
                                  socket_timeout=5, socket_connect_timeout=5)
    try:
        before = client.info("stats")  # Metadata reply <20KB; no stream materialization.
        memory = client.info("memory")  # Metadata reply <20KB.
        if memory["used_memory"] > MEMORY_LIMIT:
            raise RuntimeError("used_memory exceeds unchanged 1.6GB margin")
        key = settings.redis_stream_prefix + ":market-data-subscription-owners"
        if client.type(key) != "hash" or client.hlen(key) != len(FIELDS):  # Scalar replies.
            raise RuntimeError("owners hash missing or unknown consumer")
        if sum(client.hstrlen(key, field) for field in FIELDS) > 100_000:  # Scalar per field.
            raise RuntimeError("owners hash exceeds pre-read100KB bound")
        owners = client.hgetall(key)  # Pre-bounded100KB plus field-name framing.
        if set(owners) != FIELDS or owners["_migration_complete"] != "1":
            raise RuntimeError("owner fields or migration marker changed")
        sets = {name: json.loads(owners[name]) for name in OWNERS}
        if any(not isinstance(symbols, list) or len(symbols) > 1000 or
               any(not isinstance(symbol, str) or not symbol for symbol in symbols)
               for symbols in sets.values()):
            raise RuntimeError("malformed/unbounded owner set")
        if len(sets["momentum-paper"]) > 16:
            raise RuntimeError("paper owner cap breached")
        types = {name: client.type(settings.redis_stream_prefix + ":" + name)
                 for name in STREAMS}  # Nine scalar TYPE reads, no payloads.
        if any(value != "stream" for value in types.values()):
            raise RuntimeError("stream missing/type changed")
        after = client.info("stats")  # Metadata <20KB.
        if after["evicted_keys"] != before["evicted_keys"]:
            raise RuntimeError("eviction during checkpoint")
        return {"at_utc": datetime.now(timezone.utc).isoformat(),
                "evicted_keys": after["evicted_keys"], "used_memory": memory["used_memory"],
                "owners": owners, "sets": sets, "stream_types": types,
                "active_union": sorted(set().union(*map(set, sets.values())))}
    finally:
        client.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--baseline")
    a = p.parse_args()
    result = collect()
    if a.baseline:
        before = json.loads(Path(a.baseline).read_text())
        if result["evicted_keys"] != before["evicted_keys"]:
            raise RuntimeError("evictions changed versus pre-install baseline")
        if result["stream_types"] != before["stream_types"]:
            raise RuntimeError("stream presence changed versus baseline")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
