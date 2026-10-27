"""Decode retained line events; do not invent coverage or rewrite event times."""

from datetime import datetime
from hashlib import sha256
import math

from project_mai_tai.strategy_core.session_line_restore import SessionCoverage
from tools.l12_close_asof_contract import CANDIDATE_HEAD, is_sha
from tools.l12_close_export_restore import strict_json


def decode_line_restore(raw, *, reviewed_sha256, reader_source_sha256, snapshot_id, asof_ms):
    if not isinstance(raw, bytes) or not is_sha(reviewed_sha256) or sha256(raw).hexdigest() != reviewed_sha256:
        raise ValueError("line_export_hash_unbound")
    packet = strict_json(raw)
    if (not isinstance(packet, dict) or packet.get("purpose") != "L12_PERSISTED_LINE_RESTORE"
            or packet.get("candidate_head") != CANDIDATE_HEAD
            or not is_sha(reader_source_sha256) or packet.get("reader_source_sha256") != reader_source_sha256
            or packet.get("snapshot_id") != snapshot_id or packet.get("asof_ms") != asof_ms
            or type(asof_ms) is not int or type(packet.get("asof_ms")) is not int
            or not isinstance(packet.get("symbol"), str) or not packet["symbol"]
            or packet["symbol"] != packet["symbol"].upper()):
        raise ValueError("line_export_identity_unknown")
    events = packet.get("events")
    if not isinstance(events, list) or not events or len(events) > 10_000:
        raise ValueError("line_events_missing_or_over_bound")
    records, previous = [], 0
    for ordinal, event in enumerate(events, 1):
        if not isinstance(event, dict) or event.get("ordinal") != ordinal or type(event.get("ordinal")) is not int:
            raise ValueError("line_source_ordinal_unknown")
        stamp, bar = event.get("available_at_ms"), event.get("bar")
        if (type(stamp) is not int or not 0 < stamp <= asof_ms or stamp < previous
                or not isinstance(event.get("source_pointer"), str) or not event["source_pointer"]
                or not isinstance(bar, dict) or set(bar) != {"symbol", "bar_time", "open_price",
                    "high_price", "low_price", "close_price", "volume"}
                or bar["symbol"] != packet["symbol"] or type(bar["volume"]) is not int or bar["volume"] < 0):
            raise ValueError("line_event_clock_or_shape_unknown")
        instant = datetime.fromisoformat(bar["bar_time"])
        if instant.utcoffset() is None or int(instant.timestamp() * 1000) + 60_000 > stamp:
            raise ValueError("line_bar_not_closed_at_recorded_availability")
        values = [float(bar[key]) for key in ("open_price", "high_price", "low_price", "close_price")]
        if any(type(bar[key]) is bool for key in ("open_price", "high_price", "low_price", "close_price")):
            raise ValueError("boolean_bar_price")
        if (not all(math.isfinite(value) and value > 0 for value in values)
                or not values[2] <= min(values[0], values[3]) <= max(values[0], values[3]) <= values[1]):
            raise ValueError("invalid_bar_prices")
        records.append((ordinal, event["source_pointer"], dict(bar)))
        previous = stamp
    coverage = packet.get("coverage")
    if coverage is not None:
        if (not isinstance(coverage, dict) or set(coverage) != {"source", "start_ms", "end_ms", "closed_ids",
                "complete", "bars_sha256", "prefix_complete", "empty_minutes"}
                or type(coverage["complete"]) is not bool or type(coverage["prefix_complete"]) is not bool
                or any(type(coverage[key]) is not int for key in ("start_ms", "end_ms"))
                or not 0 < coverage["start_ms"] < coverage["end_ms"] <= asof_ms
                or not is_sha(coverage["bars_sha256"])
                or any(not isinstance(coverage[key], list) or any(type(value) is not int for value in coverage[key])
                       for key in ("closed_ids", "empty_minutes"))):
            raise ValueError("coverage_receipt_unknown")
        coverage = SessionCoverage(**{**coverage, "closed_ids": tuple(coverage["closed_ids"]),
                                     "empty_minutes": tuple(coverage["empty_minutes"])})

    async def restore(case):
        if case.symbol != packet["symbol"] or case.now_ms <= asof_ms:
            raise ValueError("line_restore_case_or_boot_clock_unknown")
        boot = case.now_ms
        for _, _, bar in records:
            # Ingestion occurs at boot. Original candle and availability times
            # above are retained, not projected to fresh native timestamps.
            await case.feed_bar(bar, now_ms=boot, phase="replay", source="callback")
        if coverage is not None:
            case.attest(coverage)
        await case.rebuild(now_ms=boot)

    return {"restore": restore, "raw_sha256": reviewed_sha256, "source_ordinals": tuple(
        (ordinal, pointer) for ordinal, pointer, _ in records),
        "coverage_supplied": coverage is not None, "native_completeness": "UNQUALIFIED"}
