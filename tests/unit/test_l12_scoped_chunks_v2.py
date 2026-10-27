"""CONTROLLED chunk data against actual delivered metadata; no broad export."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from tools.l12_close_export_restore import SOURCE_PINS
from tools.l12_scoped_chunks_v2 import (
    ACTUAL_METADATA_SHA256, CANDIDATE_HEAD, CLOSE_MS, REQUIRED, PROJECTIONS, ZERO,
    Limits, inventory_schema, keyset_query, validate_v2, parse_key,
    SQL_NULLS, JSON_TEXTS, TABLES,
)
from sqlalchemy import JSON
from tools.l12_required_copy_scope import scope_hash

METADATA = Path(__file__).parents[1] / "fixtures" / "l12_required23_metadata_20261023.jsonl"
STAMP = "2026-10-23T20:00:01+00:00"


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def operands():
    metadata = METADATA.read_bytes()
    shape = inventory_schema(metadata, reviewed_sha256=ACTUAL_METADATA_SHA256)
    limits = Limits(rows_per_chunk=2, chunk_bytes=65536, manifest_bytes=1048576, chunks=64)
    rows = {name: [] for name in REQUIRED}
    rows["broker_accounts"] = [dict(id=f"{index:08d}-1111-4111-8111-111111111111",
        name=f"live:CONTROLLED-{index}", provider="schwab", environment="CONTROLLED",
        external_account_id=None, is_active=True, created_at=STAMP, updated_at=STAMP) for index in (1, 2, 3)]
    rows["bot_control_states"] = [dict(strategy_id="11111111-1111-4111-8111-111111111111",
        execution_mode="live", generation=0, revision=0, desired_state="STOP",
        active_command_id=None, updated_at=STAMP)]
    for source in rows.values():
        for row in source:
            row[SQL_NULLS] = []
    for name, source in rows.items():
        for row in source:
            row[JSON_TEXTS] = {col.name: raw(row[col.name]).decode()
                for col in TABLES[name].columns if isinstance(col.type, JSON)}
    manifest = dict(version=2, purpose="L12_SCOPED_CHUNKED_CLOSE_COPY", schema_component_head=CANDIDATE_HEAD,
        source_pins=dict(SOURCE_PINS), scope_sha256=scope_hash(), schema_sha256=shape["schema_sha256"],
        metadata_sha256=ACTUAL_METADATA_SHA256, capture_head="c" * 40, snapshot_id="CONTROLLED",
        asof_ms=CLOSE_MS + 1000, limits=vars(limits),
        column_adapters=shape["required_column_adapters"],
        json_null_encoding="PER_ROW_SOURCE_IS_NULL_FLAGS_V1",
        json_text_encoding="ORIGINAL_SERVER_TEXT_V1",
        snapshot_policy="ONE_READONLY_REPEATABLE_READ_FULL_PK_NO_OTHER_PREDICATES", tables={})
    blobs = {}
    for name in REQUIRED:
        if name in PROJECTIONS:
            manifest["tables"][name] = dict(source_state="ABSENT_CANDIDATE_PROJECTION")
            continue
        keys = shape["schema"][name]["primary_key"]
        previous, prior_sha, chunks = None, ZERO, []
        for start in range(0, len(rows[name]), 2):
            batch = rows[name][start:start + 2]
            _, query_sha = keyset_query(name, after_key=previous, page_rows=2)
            part = dict(table=name, snapshot_id="CONTROLLED", asof_ms=CLOSE_MS + 1000,
                sequence=len(chunks) + 1, previous_sha256=prior_sha, query_sha256=query_sha, rows=batch)
            payload = raw(part)
            ref = f"CONTROLLED:{name}:{len(chunks) + 1}"
            blobs[ref] = payload
            descriptor = dict(reference=ref, sha256=sha256(payload).hexdigest(), bytes=len(payload),
                rows=len(batch), first_key=[batch[0][key] for key in keys], last_key=[batch[-1][key] for key in keys])
            chunks.append(descriptor)
            previous, prior_sha = descriptor["last_key"], descriptor["sha256"]
        _, query_sha = keyset_query(name, after_key=previous, page_rows=2)
        end = raw(dict(table=name, snapshot_id="CONTROLLED", asof_ms=CLOSE_MS + 1000,
            query_sha256=query_sha, after_key=previous, rows=[]))
        ref = f"CONTROLLED:{name}:END"
        blobs[ref] = end
        manifest["tables"][name] = dict(source_state="PRESENT_FULL_EXHAUSTED", chunks=chunks,
            end_probe=dict(reference=ref, sha256=sha256(end).hexdigest(), bytes=len(end)),
            exact_export_rows=len(rows[name]), exact_export_bytes=sum(chunk["bytes"] for chunk in chunks),
            rows_sha256=sha256(b"".join(raw(row) + b"\n" for row in rows[name])).hexdigest(),
            pk_sha256=sha256(b"".join(raw([row[key] for key in keys]) + b"\n" for row in rows[name])).hexdigest())
    approval = dict(scope_sha256=scope_hash(), missing_tables=sorted(PROJECTIONS),
        action="PRIVATE_CANDIDATE_ONLY_CANONICAL_INSTALL_DERIVATION", review_id="CONTROLLED",
        never_source_rows_or_boot_count_grant=True, source_inventory_sha256=ACTUAL_METADATA_SHA256,
        capture_root="c" * 40)
    return manifest, blobs, metadata, dict(reviewed_metadata_sha256=ACTUAL_METADATA_SHA256,
        expected_capture_head="c" * 40, expected_snapshot_id="CONTROLLED", expected_asof_ms=CLOSE_MS + 1000,
        limits=limits, projection_approval=approval, column_adapter_approval=dict(
            source_metadata_sha256=ACTUAL_METADATA_SHA256, capture_head="c" * 40,
            adapters=shape["required_column_adapters"], private_schema_only_preserve_source_nulls=True,
            review_id="CONTROLLED_EXPLICIT_SOURCE_NULLABILITY_REVIEW"))


def run(values):
    manifest, blobs, metadata, kwargs = values
    data = raw(manifest)
    return validate_v2(data, metadata, blobs.__getitem__, reviewed_manifest_sha256=sha256(data).hexdigest(), **kwargs)


def mutate_part(values, name, index, transform, *, end=False):
    manifest, blobs, _, _ = values
    descriptor = manifest["tables"][name]["end_probe"] if end else manifest["tables"][name]["chunks"][index]
    part = json.loads(blobs[descriptor["reference"]])
    transform(part)
    payload = raw(part)
    blobs[descriptor["reference"]] = payload
    descriptor.update(sha256=sha256(payload).hexdigest(), bytes=len(payload))


def test_actual_metadata_exact_schema_and_absence_not_estimated_zero():
    metadata = METADATA.read_bytes()
    plan = inventory_schema(metadata, reviewed_sha256=ACTUAL_METADATA_SHA256)
    assert plan["inventory"]["missing_candidate_projections"] == [
        "brokerdecides_dispatch_evidence", "brokerdecides_dispatch_retirements"]
    assert plan["schema"]["bot_control_states"]["primary_key"] == ["strategy_id", "execution_mode"]
    assert plan["schema"]["broker_accounts"]["source_state"] == "PRESENT"


def test_full_chunk_chain_exact_counts_without_whole_packet_row_cap():
    result = run(operands())
    assert result["status"] == "V2_BYTE_PK_CHAIN_BOUND_NOT_RESTORED_OR_NATIVE_PROOF"
    assert result["tables"]["broker_accounts"]["exact_export_rows"] == 3
    assert result["tables"]["bot_control_states"]["exact_export_rows"] == 1
    assert result["chunks"] == 3
    assert "exact_export_rows" not in result["tables"]["brokerdecides_dispatch_evidence"]
    assert result["permission_granted"] is False
    assert result["foreign_key_row_validation"] == "UNRUN_REQUIRES_PRIVATE_RESTORER"


def test_actual_keyset_query_all_columns_full_pk_only_no_filters_or_offset():
    query, _ = keyset_query("bot_control_states", after_key=["11111111-1111-4111-8111-111111111111", "live"], page_rows=128)
    sql = str(query.compile(dialect=postgresql.dialect()))
    assert "public.bot_control_states.strategy_id" in sql
    assert 'execution_mode COLLATE "C"' in sql
    assert "WHERE (" in sql and ") > (" in sql
    assert "OFFSET" not in sql and "status" not in sql and "account_id" not in sql


@pytest.mark.parametrize("fault", ["chunk_drop", "duplicate_key", "reverse_key", "snapshot", "query", "count",
    "digest", "previous_hash", "end_nonempty", "end_snapshot", "end_missing", "reference_reuse", "missing_column",
    "projection_empty", "projection_unapproved", "scope", "metadata", "capture_head", "future_clock", "byte_hash"])
def test_incomplete_membership_or_schema_never_partially_accepts(fault):
    values = operands()
    manifest, blobs, _, kwargs = values
    entry = manifest["tables"]["broker_accounts"]
    if fault == "chunk_drop":
        entry["chunks"].pop()
    elif fault == "duplicate_key":
        mutate_part(values, "broker_accounts", 0, lambda part: part["rows"][1].update(id=part["rows"][0]["id"]))
    elif fault == "reverse_key":
        mutate_part(values, "broker_accounts", 0, lambda part: part["rows"].reverse())
    elif fault in {"snapshot", "query", "previous_hash"}:
        key = {"snapshot": "snapshot_id", "query": "query_sha256", "previous_hash": "previous_sha256"}[fault]
        mutate_part(values, "broker_accounts", 0, lambda part: part.update({key: "CHANGED"}))
    elif fault == "count":
        entry["exact_export_rows"] += 1
    elif fault == "digest":
        entry["pk_sha256"] = "0" * 64
    elif fault == "end_nonempty":
        mutate_part(values, "broker_accounts", 0, lambda part: part.update(rows=[{}]), end=True)
    elif fault == "end_snapshot":
        mutate_part(values, "broker_accounts", 0, lambda part: part.update(snapshot_id="OTHER"), end=True)
    elif fault == "end_missing":
        entry.pop("end_probe")
    elif fault == "reference_reuse":
        entry["end_probe"] = deepcopy(entry["chunks"][0])
    elif fault == "missing_column":
        mutate_part(values, "broker_accounts", 0, lambda part: part["rows"][0].pop("updated_at"))
    elif fault == "projection_empty":
        manifest["tables"]["brokerdecides_dispatch_evidence"] = dict(source_state="ABSENT_CANDIDATE_PROJECTION", exact_export_rows=0)
    elif fault == "projection_unapproved":
        kwargs["projection_approval"] = None
    elif fault == "scope":
        manifest["scope_sha256"] = "0" * 64
    elif fault == "metadata":
        kwargs["reviewed_metadata_sha256"] = "0" * 64
    elif fault == "capture_head":
        kwargs["expected_capture_head"] = "0" * 40
    elif fault == "future_clock":
        mutate_part(values, "broker_accounts", 0, lambda part: part["rows"][0].update(updated_at="2026-10-24T00:00:00+00:00"))
    else:
        blobs[entry["chunks"][0]["reference"]] += b" "
    with pytest.raises((ValueError, KeyError)):
        run(values)


@pytest.mark.parametrize("key", [None, [], ["11111111-1111-4111-8111-111111111111"], [True, "live"]])
def test_composite_pk_cannot_be_truncated_or_coerced(key):
    with pytest.raises((ValueError, TypeError)):
        parse_key("bot_control_states", key)


@pytest.mark.parametrize("field", ["rows_per_chunk", "chunk_bytes", "manifest_bytes", "chunks"])
def test_resource_bounds_are_refusals_not_silent_page_truncation(field):
    kwargs = dict(rows_per_chunk=2, chunk_bytes=65536, manifest_bytes=1048576, chunks=64)
    kwargs[field] = 0
    with pytest.raises(ValueError):
        Limits(**kwargs)
