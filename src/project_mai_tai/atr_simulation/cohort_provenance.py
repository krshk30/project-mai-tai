"""Identity and retained-evidence guards; no trading or native-book inference."""

import hashlib
import json
from pathlib import Path
from uuid import UUID

from .repository import digest


def canonical_uuid(value):
    try:
        result = str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("physical_uuid_required") from error
    if str(value) != result:
        raise ValueError("canonical_physical_uuid_required")
    return result


def physical_identity(spec, plan, indexed):
    packet = indexed.get(spec.get("account_identity_packet_key"))
    if packet is None or packet["kind"] != "account_identity":
        raise ValueError("actual_account_strategy_mapping_required")
    row, identity = packet["row"], spec["identity"]
    account_id, strategy_id = canonical_uuid(row["account_id"]), canonical_uuid(row["strategy_id"])
    expected = {"account_id": account_id, "strategy_id": strategy_id,
        "account_name": row["account_name"], "broker": row["broker"], "strategy": row["strategy_code"]}
    if (canonical_uuid(identity["account_id"]) != account_id
            or canonical_uuid(spec["strategy_id"]) != strategy_id
            or identity["broker"] != row["broker"] or identity["strategy"] != row["strategy_code"]
            or spec["account_name"] != row["account_name"] or row["strategy_code"] != "schwab_1m_v2"):
        raise ValueError("plan_conflicts_with_actual_account_strategy_mapping")
    matching = [item for item in plan["accounts"] if all(item.get(key) == value for key, value in expected.items())
        and item.get("account_identity_packet_key") == packet["key"]]
    if len(matching) != 1:
        raise ValueError("roster_conflicts_with_actual_account_strategy_mapping")
    return row


def actual_fill_identity(row, physical, symbol):
    for prefix in ("fill", "order", "intent", "physical"):
        if (canonical_uuid(row.get(prefix + "_account_id")) != physical["account_id"]
                or canonical_uuid(row.get(prefix + "_strategy_id")) != physical["strategy_id"]):
            raise ValueError("actual_fill_order_intent_identity_mismatch:" + prefix)
    if (row.get("account_name") != physical["account_name"] or row.get("broker") != physical["broker"]
            or row.get("strategy_code") != physical["strategy_code"] or row.get("symbol") != symbol):
        raise ValueError("actual_fill_physical_namespace_mismatch")


def load_proof_packets(descriptors, evidence_root):
    """Read explicitly supplied immutable proofs, never create proof from an empty page."""
    if len(descriptors) > 256:
        raise ValueError("bounded_retained_proofs_required")
    root = Path(evidence_root).resolve(strict=True)
    packets, seen = [], set()
    for descriptor in descriptors:
        path = Path(descriptor["artifact"])
        path = (path if path.is_absolute() else root / path).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("bounded_contained_retained_proof_required")
        raw = path.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        if checksum != descriptor["sha256"] or descriptor.get("pointer") != "/":
            raise ValueError("retained_proof_file_identity_mismatch")
        if descriptor.get("kind") not in {"RETAINED", "CONTROLLED"}:
            raise ValueError("retained_or_controlled_proof_required")
        row = json.loads(raw, parse_float=str)
        if row.get("evidence_type") not in {"EPISODE_NO_BUY", "ACCOUNT_WATCH_RETENTION"}:
            raise ValueError("unsupported_retained_proof_type")
        if descriptor.get("asof_ns") != row.get("available_ns"):
            raise ValueError("proof_availability_conflicts_with_retained_record")
        from .engine_binding import verify_plan_evidence

        # A proof-file flag does not substitute for its actual underlying trace files.
        verify_plan_evidence(row, root)
        key = "proof:" + checksum
        if key in seen:
            raise ValueError("duplicate_retained_proof")
        seen.add(key)
        packets.append({"key": key, "kind": "retained_proof", "row": row, "source": dict(descriptor)})
    return tuple(packets)


def _in_scope(row, spec, physical):
    return (row.get("identity") == spec["identity"] and row.get("strategy_id") == physical["strategy_id"]
        and row.get("account_name") == physical["account_name"])


def _proof(indexed, key, kind, spec, physical, end_ns, observed_at_ns):
    packet = indexed.get(key)
    if packet is None or packet.get("kind") != "retained_proof":
        return None
    row, source = packet["row"], packet["source"]
    available = row.get("available_ns")
    if (row.get("evidence_type") != kind or not _in_scope(row, spec, physical)
            or row.get("complete") is not True or row.get("gaps") != []
            or isinstance(available, bool) or not isinstance(available, int)
            or not isinstance(observed_at_ns, int) or isinstance(observed_at_ns, bool)
            or not end_ns <= available <= observed_at_ns or source.get("asof_ns") != available):
        return None
    return packet


def _spans(coverage, start_ns, end_ns, observed_at_ns):
    return (coverage.complete is True and not coverage.gaps and coverage.start_ns <= start_ns
        and coverage.end_ns >= end_ns and bool(coverage.sources)
        and isinstance(observed_at_ns, int) and not isinstance(observed_at_ns, bool)
        and all(source.asof_ns is not None and source.asof_ns <= observed_at_ns for source in coverage.sources))


def _row_spans(row, start_ns, end_ns):
    start, end = row.get("start_ns"), row.get("end_ns")
    return (isinstance(start, int) and not isinstance(start, bool)
        and isinstance(end, int) and not isinstance(end, bool) and start <= start_ns and end >= end_ns)


def _trace_sources_available(sources, proof_available_ns, observed_at_ns):
    if not isinstance(sources, list) or not sources:
        return False
    for source in sources:
        if not isinstance(source, dict) or not {"artifact", "sha256", "pointer", "kind", "asof_ns"} <= source.keys():
            return False
        available = source["asof_ns"]
        # Availability is explicit evidence, never an epoch-zero default.
        if (isinstance(available, bool) or not isinstance(available, int)
                or not 0 < available <= proof_available_ns <= observed_at_ns):
            return False
    return True


def proven_no_buy(spec, physical, indexed, buy_ns, end_ns, coverage, source, observed_at_ns):
    proof = _proof(indexed, spec.get("no_buy_packet_key"), "EPISODE_NO_BUY",
        spec, physical, end_ns, observed_at_ns)
    if (proof is None or source is None or end_ns <= buy_ns
            or not _spans(coverage, buy_ns, end_ns, observed_at_ns)):
        return False
    row, actual = proof["row"], proof["source"]
    if (row.get("bought") is not False or row.get("proof_scope") != "RETAINED_EPISODE_ENTRY_LEDGER"
            or not _trace_sources_available(row.get("ledger_sources"), row["available_ns"], observed_at_ns)
            or not _row_spans(row, buy_ns, end_ns)):
        return False
    # A digest-shaped reference to a different file cannot supply this proof.
    return all(getattr(source, key) == actual[key] for key in (
        "artifact", "sha256", "pointer", "asof_ns", "kind"))


def account_membership(spec, physical, indexed, buy_ns, coverage, observed_at_ns, timestamp_ns):
    anchor = indexed.get(spec.get("watched_packet_key"))
    if anchor is None or anchor["kind"] != "snapshot":
        return None, None
    row, event = anchor["row"], anchor["row"]["event"]
    payload = event.get("payload", {})
    if (event.get("event_type") != "isolated_bot_state"
            or payload.get("account_name") != physical["account_name"]
            or payload.get("strategy_code") != physical["strategy_code"]
            or not row.get("stream", "").endswith(":strategy-state-isolated")):
        return None, None
    available = int(row["stream_id"].split("-", 1)[0]) * 1_000_000 + 999999
    if timestamp_ns(event["produced_at"]) > available or available > buy_ns:
        return None, None
    if not _spans(coverage, available, buy_ns, observed_at_ns):
        return None, None
    proof = _proof(indexed, spec.get("watched_retention_packet_key"), "ACCOUNT_WATCH_RETENTION",
        spec, physical, buy_ns, observed_at_ns)
    if proof is None:
        return None, None
    retention = proof["row"]
    if (retention.get("stream") != row["stream"] or not _row_spans(retention, available, buy_ns)
            or not _trace_sources_available(retention.get("retention_sources"),
                retention["available_ns"], observed_at_ns)):
        return None, None
    actual = {}
    for key, packet in indexed.items():
        if packet["kind"] != "snapshot" or packet["row"].get("stream") != row["stream"]:
            continue
        snapshot = packet["row"]["event"]
        data = snapshot.get("payload", {})
        if (snapshot.get("event_type") != "isolated_bot_state" or data.get("account_name") != physical["account_name"]
                or data.get("strategy_code") != physical["strategy_code"]):
            continue
        at = int(packet["row"]["stream_id"].split("-", 1)[0]) * 1_000_000 + 999999
        if available <= at <= buy_ns:
            if timestamp_ns(snapshot["produced_at"]) > at:
                return None, None
            actual[key] = digest(packet)
            if spec["identity"]["symbol"] not in data.get("watchlist", []):
                return None, None
    declared = retention.get("snapshots", [])
    if (not actual or anchor["key"] not in actual or not isinstance(declared, list)
            or any(not isinstance(item, dict) or set(item) != {"packet_key", "sha256"} for item in declared)
            or len({item["packet_key"] for item in declared}) != len(declared)
            or {item["packet_key"]: item["sha256"] for item in declared} != actual):
        return None, None
    return True, proof
