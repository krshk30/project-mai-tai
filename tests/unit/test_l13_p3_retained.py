from __future__ import annotations

from datetime import UTC, datetime, timedelta
import gzip
import hashlib
import json
import logging
import os

import pytest

from tools.l13_p3_retained import (
    RawBatch, canonical_alert, iter_raw_batches, log_time, match_log_group,
    parse_details, parse_strategy_log, qualification, ready_for_recorded_comparison, ScannerConsumer,
)


def packet(tmp_path, *, second=b"", expected_changes=None):
    item = {"event_id": "00000000-0000-0000-0000-000000000001",
            "event_type": "snapshot_batch", "source_service": "market-data-gateway",
            "produced_at": "2026-10-22T18:16:51.334452Z", "payload": {
                "snapshots": [], "reference_data": [], "completed_at": "2026-10-22T18:16:51.334320Z"}}
    raw = b"1792693011434-0\ndata\n" + json.dumps(item).encode() + b"\n" + second
    target = tmp_path / "controlled.raw.gz"
    target.write_bytes(gzip.compress(raw))
    expected = {"batches": 1, "snapshots_per_batch": 0, "references_per_batch": 0,
                "raw_bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "first_id": "1792693011434-0", "last_id": "1792693011434-0"}
    expected.update(expected_changes or {})
    return target, expected


def test_raw_redis_framing_and_eof_provenance(tmp_path):
    path, expected = packet(tmp_path)
    rows = list(iter_raw_batches(path, expected))  # ONE tiny synthetic parser fixture, not P3 input.
    assert len(rows) == 1
    assert rows[0].next_stream_id is None
    assert rows[0].envelope["event_id"].endswith("000001")


@pytest.mark.parametrize("key,value", [
    ("batches", 2), ("raw_bytes", 1), ("raw_sha256", "0" * 64),
    ("first_id", "1-0"), ("last_id", "1-0"),
    ("snapshots_per_batch", 1), ("references_per_batch", 1),
])
def test_raw_packet_drift_is_not_accepted(key, value, tmp_path):
    path, expected = packet(tmp_path, expected_changes={key: value})
    with pytest.raises(ValueError):
        list(iter_raw_batches(path, expected))


def test_stream_does_not_decode_second_batch_before_yielding_first(tmp_path):
    path, expected = packet(tmp_path, second=b"1792693018434-0\ndata\nNOT_JSON\n")
    reader = iter_raw_batches(path, expected)
    first = next(reader)
    assert first.next_stream_id == "1792693018434-0"
    with pytest.raises(json.JSONDecodeError):
        next(reader)


def test_wrong_redis_field_never_becomes_data(tmp_path):
    path, expected = packet(tmp_path)
    raw = gzip.decompress(path.read_bytes()).replace(b"\ndata\n", b"\nother\n")
    path.write_bytes(gzip.compress(raw))
    with pytest.raises(ValueError, match="exactly data"):
        list(iter_raw_batches(path, expected))


def test_recorded_log_details_numeric_boolean_and_string_are_preserved():
    assert parse_details("{'change_pct': 5.2, 'price_5min_ago': 8.985, 'catchup_seed': True, 'spike_type': 'relative'}") == {
        "change_pct": "5.2", "price_5min_ago": "8.985", "catchup_seed": True, "spike_type": "relative"}
    with pytest.raises(ValueError):
        parse_details("{'unrecognized': None}")


def test_observable_alert_canonicalization_does_not_invent_unlogged_fields():
    alert = {"type": "SQUEEZE_5MIN", "ticker": "ATGL", "price": 9.10,
             "details": {"change_pct": 6.2, "price_5min_ago": 8.57}}
    assert canonical_alert(alert) == {"type": "SQUEEZE_5MIN", "ticker": "ATGL", "price": "9.1",
                                      "details": {"change_pct": "6.2", "price_5min_ago": "8.57"}}


def test_actual_alert_and_summary_grouping(tmp_path):
    path = tmp_path / "controlled.log"
    path.write_text("2026-10-22 18:25:02,391 INFO [scanner] [ALERT] [SQUEEZE_5MIN] ATGL @ $9.45 | {'change_pct': 5.2}\n"
                    "2026-10-22 18:25:06,941 INFO [strategy-engine] snapshot batch processed | alerts=1 confirmed=0\n")
    group = parse_strategy_log(path)[0]
    assert group["confirmed"] == 0 and len(group["alerts"]) == 1
    assert group["decision_at"] == log_time("2026-10-22 18:25:02,391")
    assert group["decision_at"].tzinfo == UTC


def test_missing_alert_lines_cannot_be_favorable_zero(tmp_path):
    path = tmp_path / "controlled.log"
    path.write_text("2026-10-22 18:25:06,941 INFO [strategy-engine] snapshot batch processed | alerts=1 confirmed=0\n")
    with pytest.raises(ValueError, match="disagree"):
        parse_strategy_log(path)


@pytest.mark.parametrize("matches", [0, 2])
def test_timestamp_association_requires_unique_group(matches):
    batch = RawBatch(1, "1792693011434-0", "1792693018434-0", b"", {})
    groups = [{"decision_at": datetime.fromtimestamp(1792693012, UTC)}] * matches
    with pytest.raises(ValueError, match="ambiguous/missing"):
        match_log_group(batch, groups)


def test_five_minutes_is_not_a_synthetic_sixty_batch_seed():
    first = datetime(2026, 10, 22, 18, 16, 51, tzinfo=UTC)
    assert not ready_for_recorded_comparison(first, first + timedelta(seconds=300), {"squeeze_5min_ready": False})
    assert not ready_for_recorded_comparison(first, first + timedelta(seconds=299), {"squeeze_5min_ready": True})
    assert ready_for_recorded_comparison(first, first + timedelta(seconds=440), {"squeeze_5min_ready": True})


@pytest.mark.asyncio
async def test_new_consumer_binding_invokes_actual_service_and_scanner(monkeypatch):
    from project_mai_tai.events import SnapshotBatchEvent, SnapshotBatchPayload
    from project_mai_tai.services import strategy_engine_app

    monkeypatch.setattr(strategy_engine_app, "configure_logging",
                        lambda name, level="INFO": logging.getLogger(name))
    clock = [datetime(2026, 10, 22, 14, tzinfo=UTC)]
    consumer = ScannerConsumer(clock)
    event = SnapshotBatchEvent(source_service="market-data-gateway", produced_at=clock[0],
                               payload=SnapshotBatchPayload(snapshots=[], reference_data=[]))
    observed = await consumer.consume(event.model_dump_json())
    assert observed["warmup"]["history_cycles"] == 1
    assert observed["emitted_alerts"] == observed["confirmed"] == []
    assert not observed["warmup"]["squeeze_5min_ready"]


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("L13_G2_CLAIM") != "P3_RETAINED", reason="main exact retained-P3 claim required")
async def test_actual_retained_packet_real_worker_and_scanner_postwarmup():
    await qualification(os.environ["L13_G2_RETAINED_MANIFEST"], os.environ["L13_G2_RESULT"])
