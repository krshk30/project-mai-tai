"""Pinned persisted DB row -> explicitly derived PRICE1 payload projection.

NOT the original Redis envelope, native execution, or MOMNEG admission proof.
The original packet is never rewritten. No connections or application services.
"""

from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from project_mai_tai.buy_price_floor import buy_order_allowed
from project_mai_tai.events import TradeIntentPayload


ROOT = Path("/Users/velkris/.codex/task-queues/analysis/PRICE1-GIXI-HANDOFF-20261022")
PINS = {
    "SHA256SUMS": "9b4d7051d84fdad8599d214c3f8c7334774b68e7504e3d93503b14a040b223bb",
    "c1_gixi_intents.jsonl": "cb39f477bab18c3c68991243645abab1b14d94909462f1cb3ba8ed12911bd877",
    "c1_gixi_paper_events.jsonl": "9f381828fbf3f40aa3801684e8111dde341650aa980df4ee3ed61fd6e8fd69d9",
    "gixi.sql": "ac927ab1e09332d2bc5aa4ace0872be1a80d56380117ab1eb73ec1f58c265998",
}
BUY_ID = "dca929bb-b131-5c84-b1cc-16ee4db1142d"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        assert key not in result, ("duplicate_json_key", key)
        result[key] = value
    return result


def decode_copy_field(raw):
    """Exactly ONE COPY TEXT layer; accept paired backslashes ONLY.

    gixi.sql selects one row_to_json column using default TEXT. JSON encodes
    control characters itself; COPY doubles its backslashes. No repair/fallback.
    """
    assert b"\t" not in raw and b"\r" not in raw and b"\n" not in raw
    output = bytearray()
    index = 0
    while index < len(raw):
        value = raw[index]
        if value == 92:
            assert index + 1 < len(raw) and raw[index + 1] == 92, "unsupported_COPY_escape"
            index += 1
        output.append(value)
        index += 1
    return bytes(output)


def load_recorded_buy(record_property):
    for name in PINS:
        if not (ROOT / name).exists():
            pytest.skip(f"requires local rig artifact {ROOT / name}")
    originals = {name: (ROOT / name).read_bytes() for name in PINS}
    for name, expected in PINS.items():
        assert hashlib.sha256(originals[name]).hexdigest() == expected, name
    sql = originals["gixi.sql"].decode("utf-8")
    assert "\\copy (select row_to_json(t)" in sql
    assert "to '/tmp/c1_gixi_intents.jsonl'" in sql
    assert "format" not in sql.lower() and "csv" not in sql.lower()
    raw = originals["c1_gixi_intents.jsonl"]
    assert raw.endswith(b"\n")
    decoded = [decode_copy_field(line) for line in raw[:-1].split(b"\n")]
    assert len(decoded) == 2
    rows = [json.loads(line, object_pairs_hook=unique_object, parse_float=Decimal)
            for line in decoded]
    record_property("source_kind", "persisted_trade_intents_DB_postimage_NOT_Redis_wire")
    record_property("source_sha256", PINS["c1_gixi_intents.jsonl"])
    record_property("decoded_copy_once_sha256", hashlib.sha256(b"\n".join(decoded)+b"\n").hexdigest())
    matches = [row for row in rows if row["id"] == BUY_ID]
    assert len(matches) == 1
    row = matches[0]
    assert row["created_at"] == "2026-10-22T12:19:43.990982+00:00"
    assert (row["symbol"], row["side"], row["intent_type"], row["reason"]) == (
        "GIXI", "buy", "open", "MOMLIVE1_DETECTED")
    assert row["quantity"] == Decimal(19) and row["status"] == "filled"
    payload = row["payload"]
    assert set(payload) == {"event_id", "source_service", "momlive_parent", "momlive_episode",
                            "momlive_signal", "metadata", "momlive_runtime"}
    assert payload["event_id"] == BUY_ID and payload["source_service"] == "momentum-paper"
    assert payload["momlive_parent"] is True
    signal, metadata = payload["momlive_signal"], payload["metadata"]
    assert type(signal) is dict and signal["symbol"] == row["symbol"]
    assert signal["signal_price"] == metadata["signal_price"] == "5.25"
    assert metadata["limit_price"] == "5.26" and metadata["cap_price"] == "5.32875"
    assert metadata["episode_id"] == payload["momlive_episode"]["episode_id"] == BUY_ID
    assert metadata["momlive_strategy"] == "momentum_warn_live"
    assert payload["momlive_runtime"]["protection"]["account"] == "live:orb"
    return row, originals


def project_floor_payload(row):
    """Lossless field projection, NOT reconstruction of an original event.

    Core fields from DB row; strategy from persisted metadata; account from
    persisted protection postimage. Serialize the ORIGINAL root signal dict
    into the metadata location consumed by the canonical wire guard. No new
    price/clock/UUID/one_shot permission fields; retain all persisted metadata.
    """
    payload = row["payload"]
    metadata = dict(payload["metadata"])
    assert "momlive_signal" not in metadata
    metadata["momlive_signal"] = json.dumps(payload["momlive_signal"], separators=(",", ":"))
    return TradeIntentPayload(
        strategy_code=metadata["momlive_strategy"],
        broker_account_name=payload["momlive_runtime"]["protection"]["account"],
        symbol=row["symbol"], side=row["side"], quantity=row["quantity"],
        intent_type=row["intent_type"], reason=row["reason"], metadata=metadata)


def test_recorded_gixi_db_signal_525_allowed_without_mutating_original_payload(record_property):
    row, originals = load_recorded_buy(record_property)
    before = json.dumps(row, default=str, sort_keys=True)
    order = project_floor_payload(row)
    assert json.loads(order.metadata["momlive_signal"]) == row["payload"]["momlive_signal"]
    assert {k: v for k, v in order.metadata.items() if k != "momlive_signal"} == row["payload"]["metadata"]
    assert buy_order_allowed(order)
    assert json.dumps(row, default=str, sort_keys=True) == before
    assert all((ROOT / name).read_bytes() == data for name, data in originals.items())


@pytest.mark.parametrize("variant,allowed", [
    ("nested_only", True), ("nested_under1_padded_cap", False),
    ("missing_nested_price", False), ("conflicting_top", False)])
def test_recorded_gixi_derived_variants_prove_nested_selection_not_cap_or_top(variant, allowed, record_property):
    """Explicit DERIVED mutations, never labelled original recorded outcomes."""
    row, _ = load_recorded_buy(record_property)
    order = project_floor_payload(row)
    signal = json.loads(order.metadata["momlive_signal"])
    if variant in {"nested_only", "nested_under1_padded_cap", "missing_nested_price"}:
        order.metadata.pop("signal_price")
    if variant == "nested_under1_padded_cap":
        signal["signal_price"] = ".99"
    elif variant == "missing_nested_price":
        signal.pop("signal_price")
    elif variant == "conflicting_top":
        order.metadata["signal_price"] = "5.26"
    order.metadata["momlive_signal"] = json.dumps(signal)
    assert order.metadata["limit_price"] == "5.26" and order.metadata["cap_price"] == "5.32875"
    assert buy_order_allowed(order) is allowed
