from __future__ import annotations

import copy
from datetime import UTC, datetime
import hashlib
import json

import pytest

from tools.l13_p3_retained import assert_cold_parity, assert_full_coverage, payload_wire_bytes
from tools.l13_g2_controls import (
    MarketDataPublisher, ProductionWorkerAdapter, SnapshotRecord, recorded_snapshot_completion,
)


RAW = '{"event_id":"controlled","payload":{"snapshots":[],"reference_data":[],"completed_at":"2026-10-22T18:16:51Z"}}'


def observed():
    return {"emitted_alerts": [], "alerts": [], "recent_alerts": [], "confirmed": [],
            "top_confirmed": [], "watchlist": [], "warmup": {"history_cycles": 1},
            "subscriptions": {"watchlist": []}}


def test_payload_extraction_preserves_literal_bytes_not_envelope_metadata():
    raw = b' {"other":{"payload":"not top-level"},"payload": { "x":"escaped \\" quote", "n":1.00 }} '
    assert payload_wire_bytes(raw) == b'{ "x":"escaped \\" quote", "n":1.00 }'
    assert payload_wire_bytes(RAW) == RAW[RAW.index('{"snapshots"'):-1].encode()
    different_envelope = RAW.replace('"controlled"', '"another-id"')
    assert assert_cold_parity(observed(), observed(), RAW, different_envelope, "1-0") == hashlib.sha256(
        payload_wire_bytes(RAW)).hexdigest()


@pytest.mark.parametrize("left,right", [
    ('{"x":1}', '{ "x":1}'),
    ('{"x":1,"y":2}', '{"y":2,"x":1}'),
    ('{"x":1}', '{"x":1.0}'),
    ('{"x":"\\u0041"}', '{"x":"A"}'),
    ('{"completed_at":"2026-10-22T18:16:51Z"}', '{"completed_at":"2026-10-22T18:16:52Z"}'),
])
def test_wire_drift_is_not_hidden_by_semantic_normalization(left, right):
    if "completed_at" not in left:
        assert json.loads(left) == json.loads(right)
    with pytest.raises(AssertionError, match="payload bytes differ"):
        assert_cold_parity(observed(), observed(), '{"payload":' + left + '}',
                           '{"payload":' + right + '}', "1-0")


@pytest.mark.parametrize("raw", [
    '[]', '{}', '{"other":1}', '{"payload":[]}', '{"payload":{},"payload":{}}',
    '{"payload":{},"id":1,"id":2}', '{"payload":{}} trailing', '{"payload":{},}',
])
def test_invalid_envelope_never_becomes_payload_byte_proof(raw):
    with pytest.raises(ValueError):
        payload_wire_bytes(raw)


@pytest.mark.parametrize("key", ["emitted_alerts", "alerts", "recent_alerts", "confirmed",
                                  "top_confirmed", "watchlist", "warmup", "subscriptions"])
def test_every_required_scanner_output_drift_fails(key):
    old = observed()
    new = copy.deepcopy(old)
    new[key] = ["controlled drift"]
    with pytest.raises(AssertionError, match="scanner outputs differ"):
        assert_cold_parity(old, new, RAW, RAW, "1-0")


def test_all_203_cold_spikes_remain_in_exact_parity():
    old = observed()
    old["emitted_alerts"] = [{"type": "VOLUME_SPIKE", "ticker": f"COLD{index}"} for index in range(203)]
    new = copy.deepcopy(old)
    assert assert_cold_parity(old, new, RAW, RAW, "1-0")
    new["emitted_alerts"].pop()
    with pytest.raises(AssertionError, match="scanner outputs differ"):
        assert_cold_parity(old, new, RAW, RAW, "1-0")


def coverage(count=120, eof=True):
    return {"retained_input_verified_through_eof": eof,
            "comparisons": [{"stream_id": f"{index + 1}-0", "scanner_parity": True,
                             "payload_byte_parity": True} for index in range(count)]}


def test_exact_120_and_eof_required_even_if_every_seen_batch_matches():
    assert_full_coverage(coverage(), 120)
    with pytest.raises(AssertionError, match="EOF"):
        assert_full_coverage(coverage(eof=False), 120)


@pytest.mark.parametrize("count", [0, 59, 60, 119, 121])
def test_partial_or_extra_batches_cannot_pass_all_120_gate(count):
    with pytest.raises(AssertionError, match="all120"):
        assert_full_coverage(coverage(count), 120)


@pytest.mark.parametrize("key", ["scanner_parity", "payload_byte_parity"])
def test_one_missing_batch_proof_keeps_whole_gate_failed(key):
    result = coverage()
    result["comparisons"][-1][key] = False
    with pytest.raises(AssertionError, match="incomplete"):
        assert_full_coverage(result, 120)


def test_duplicate_comparison_cannot_replace_last_batch():
    result = coverage()
    result["comparisons"][-1] = copy.deepcopy(result["comparisons"][-2])
    with pytest.raises(AssertionError, match="duplicate"):
        assert_full_coverage(result, 120)


@pytest.mark.asyncio
async def test_failure_exports_fresh_incomplete_journal_without_success_claim(monkeypatch, tmp_path):
    from tools import l13_p3_retained

    async def failed_replay(manifest, result):
        result["comparisons"] = coverage(count=60)["comparisons"]
        raise AssertionError("controlled failure at61; no EOF")

    monkeypatch.setattr(l13_p3_retained, "replay_retained", failed_replay)
    output = tmp_path / "fresh.json"
    with pytest.raises(AssertionError, match="controlled failure"):
        await l13_p3_retained.qualification("CONTROLLED-manifest", output)
    journal = json.loads(output.read_text())
    assert journal["gate_status"] == "FAILED_OR_INCOMPLETE"
    assert len(journal["comparisons"]) == 60
    assert not journal.get("retained_input_verified_through_eof", False)


def test_recorded_completion_restores_constructor_on_failure_and_default_is_untouched():
    from project_mai_tai.market_data import publisher

    constructor = publisher.SnapshotBatchPayload
    completed = datetime(2026, 10, 22, 18, 16, 51, tzinfo=UTC)
    with recorded_snapshot_completion(None):
        assert publisher.SnapshotBatchPayload is constructor
    with pytest.raises(RuntimeError, match="controlled cleanup"):
        with recorded_snapshot_completion(completed):
            payload = publisher.SnapshotBatchPayload(snapshots=[], reference_data=[])
            assert type(payload) is constructor
            assert payload.completed_at == completed
            raise RuntimeError("controlled cleanup")
    assert publisher.SnapshotBatchPayload is constructor
    with pytest.raises(ValueError, match="timezone-aware"):
        with recorded_snapshot_completion(completed.replace(tzinfo=None)):
            pytest.fail("naive completion accepted")
    assert publisher.SnapshotBatchPayload is constructor


@pytest.mark.asyncio
async def test_actual_spawned_worker_and_old_encoder_wire_bytes_use_same_recorded_completion():
    from project_mai_tai.market_data import publisher
    from project_mai_tai.events import SnapshotBatchEvent

    completed = datetime(2026, 10, 22, 18, 16, 51, 334320, tzinfo=UTC)
    snapshots = [SnapshotRecord(symbol="CONTROLLED", last_trade_price=2.5)]
    original_constructor = publisher.SnapshotBatchPayload
    with recorded_snapshot_completion(completed):
        old = MarketDataPublisher(None, "l13-controlled", "market-data-gateway")._encode_snapshot_batch(snapshots, [])
    assert publisher.SnapshotBatchPayload is original_constructor
    adapter = ProductionWorkerAdapter()
    new = await adapter.publish_recorded_batch(snapshots=snapshots, reference_data=[], completed_at=completed)
    assert payload_wire_bytes(old) == payload_wire_bytes(new)
    assert SnapshotBatchEvent.model_validate_json(new).payload.completed_at == completed
    assert publisher.SnapshotBatchPayload is original_constructor
