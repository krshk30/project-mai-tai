"""Separate-process Redis capture and explicit sealed-cohort engine boundary."""

import re

from .read_model import result_rows
from .repository import digest
from .service_contract import ENGINE_CONTRACT_VERSION, SealedCohort


INPUT_STREAM_SUFFIXES = ("strategy-state", "market-data", "bot-state")
MAX_EVENT_BYTES = 262144


def stream_position(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+-[0-9]+", value):
        raise ValueError("explicit_redis_stream_position_required")
    return tuple(map(int, value.split("-")))


def publish_cohort(repository, simulator, cohort: SealedCohort):
    if (not cohort.source_id or not cohort.policy_version or not cohort.source_refs
            or not cohort.episodes or cohort.all_overlapping_accounts_included is not True):
        raise ValueError("sealed_all_account_cohort_required")
    # One invocation is essential: calling per account would mint printed volume.
    results = simulator.simulate_many(cohort.episodes)
    if len(results) != len(cohort.episodes) or any(
            result.identity != episode.identity or result.contract_version != ENGINE_CONTRACT_VERSION
            for episode, result in zip(cohort.episodes, results)):
        raise ValueError("engine_result_identity_or_denominator_changed")
    return repository.publish(source_id=cohort.source_id, policy_version=cohort.policy_version,
        input_sha256=digest(cohort.episodes), source_refs=cohort.source_refs, coverage=cohort.coverage,
        result_rows=tuple(row for result in results for row in result_rows(result)))


class SimulationConsumer:
    """XREAD only: no consumer group, ACK, subscription or source-stream writes."""

    def __init__(self, redis, repository, *, consumer_id, streams):
        if not 1 <= len(streams) <= 3 or len(set(streams)) != len(streams) or any(
                not any(name.endswith(":" + suffix) for suffix in INPUT_STREAM_SUFFIXES) for name in streams):
            raise ValueError("existing_input_stream_allowlist_required")
        self.redis, self.repository, self.consumer_id = redis, repository, consumer_id
        self.streams = tuple(streams)

    async def run_once(self):
        checkpoint = self.repository.checkpoint(self.consumer_id)
        revision = checkpoint["revision"] if checkpoint else 0
        cursors = dict(checkpoint["cursors"]) if checkpoint else {name: "0-0" for name in self.streams}
        if set(cursors) != set(self.streams):
            raise ValueError("consumer_stream_scope_changed")
        for cursor in cursors.values():
            stream_position(cursor)
        response = await self.redis.xread(cursors, count=200, block=1000)
        events, advanced = [], dict(cursors)
        for stream, entries in response:
            if stream not in advanced:
                raise ValueError("foreign_stream_response")
            for identity, fields in entries:
                if stream_position(identity) <= stream_position(advanced[stream]):
                    raise ValueError("noncausal_stream_response")
                if (not isinstance(fields, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                        for key, value in fields.items()) or sum(len(key.encode()) + len(value.encode())
                        for key, value in fields.items()) > MAX_EVENT_BYTES):
                    raise ValueError("bounded_raw_stream_fields_required")
                events.append({"stream": stream, "stream_id": identity, "fields": fields})
                advanced[stream] = identity
        self.repository.capture(self.consumer_id, revision, events, advanced,
            issues=("REDIS_RETENTION_PREFIX_UNKNOWN", "FLIP_EPISODE_ASSEMBLER_UNBOUND",
                    "NO_MISSING_FILL_TO_NO_BUY_INFERENCE"))
        return len(events)

