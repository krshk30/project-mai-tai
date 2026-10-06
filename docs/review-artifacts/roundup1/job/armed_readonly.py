"""One bounded isolated-state read. Missing fields are never an empty armed set."""
from datetime import datetime, timezone
import json
import re
from uuid import UUID

from release_policy import canonical, digest, moment, need

MAX_BYTES = 262_144
MAX_ENTRIES = 25
# EVAL_RO forbids writes. Only one newest entry is examined, and its reply is
# bounded before crossing the client socket; no snapshot-batches are read.
READ = """
local rows = redis.call('XREVRANGE', KEYS[1], ARGV[1] or '+', '-', 'COUNT', 1)
if #rows ~= 1 then return redis.error_reply('state missing') end
local fields = rows[1][2]
if #fields ~= 2 or fields[1] ~= 'data' then return redis.error_reply('state fields') end
if string.len(fields[2]) > 262144 then return redis.error_reply('state oversized') end
return {rows[1][1], fields[2]}
"""


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, "duplicate published-state JSON key")
        result[key] = value
    return result


def envelope(reply, now):
    need(isinstance(reply, list) and len(reply) == 2, "missing/malformed state reply")
    entry, raw = reply
    need(isinstance(entry, str) and re.fullmatch(r"[0-9]+-[0-9]+", entry), "state entry id malformed")
    need(isinstance(raw, str) and 0 < len(raw.encode()) <= MAX_BYTES, "state body missing/oversized")
    event = json.loads(raw, object_pairs_hook=unique)
    need(isinstance(event, dict) and event.get("event_type") == "isolated_bot_state"
         and event.get("source_service") in {"schwab-1m-v2", "orb", "orb-schwab"},
         "published-state producer/type mismatch")
    UUID(event["event_id"])
    produced = moment(event["produced_at"])
    age = (now - produced).total_seconds()
    need(0 <= age <= 60, "published-state stale/future")
    entry_age = now.timestamp() - int(entry.split("-")[0]) / 1000
    need(0 <= entry_age <= 60 and abs(entry_age - age) <= 10, "stream/event timestamp mismatch")
    return entry, raw, event, age


def proof(reply, now):
    entry, raw, event, age = envelope(reply, now)
    need(event["source_service"] == "schwab-1m-v2", "published-state producer/type mismatch")
    payload = event.get("payload")
    need(isinstance(payload, dict) and payload.get("strategy_code") == "schwab_1m_v2"
         and payload.get("account_name") == "live:schwab_1m_v2", "published-state account/strategy mismatch")
    need("cw_armed_segments" in payload and type(payload["cw_armed_segments"]) is list,
         "armed field absent/malformed; cannot assume zero")
    need(payload["cw_armed_segments"] == [], "armed segments block restart")
    return dict(entry_id=entry, raw_sha256=digest(raw.encode()), event=event,
                verified_at_utc=now.isoformat(), age_s=age, armed_count=0, completeness="explicit-field-present")


def collect(client, key, now=None):
    previous = None
    discarded = []
    for index in range(MAX_ENTRIES):
        args = ("EVAL_RO", READ, 1, key)
        if previous is not None:
            args += ("(" + previous,)
        reply = client.execute_command(*args)
        observed = now or datetime.now(timezone.utc)
        entry, _, event, _ = envelope(reply, observed)
        if previous is not None:
            need(tuple(map(int, entry.split("-"))) < tuple(map(int, previous.split("-"))),
                 "published-state cursor did not advance")
        if event["source_service"] == "schwab-1m-v2":
            result = proof(reply, observed)
            result.update(entries_examined=index + 1, discarded_other_producers=discarded)
            return result
        discarded.append(dict(entry_id=entry, source_service=event["source_service"]))
        previous = entry
    need(False, "v2 state absent within bounded shared-stream scan")


def main():
    import redis
    from project_mai_tai.settings import Settings
    settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    need(settings.redis_stream_prefix == "mai_tai", "unexpected state stream prefix")
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True,
                                  socket_timeout=5, socket_connect_timeout=5)
    try:
        # Evaluate age after receipt, not before a potentially slow read.
        result = collect(client, "mai_tai:strategy-state-isolated")
    finally:
        client.close()
    print(canonical(result).decode(), end="")


if __name__ == "__main__":
    main()
