"""MAIN-only input binding, not execution or historical-cache reconstruction."""

import csv
from collections import Counter, defaultdict
import hashlib
from pathlib import Path
import re


SIGNAL_HEADERS = (
    "row_kind,session_date,utc_ts,et_ts,symbol,signal_strategy,signal_logical_id,"
    "signal_detect_sip_utc,signal_price,cap,limit_saved,qty_saved,route,account,"
    "outcome,reason,episode_id,client_order_id,broker_order_id,order_status,"
    "order_prepared_at_utc,order_submitted_at_utc,fill_qty,fill_avg_price,"
    "first_fill_at_utc,refusal_log_ask,refusal_log_ask_age_ms,refusal_log_limit,source,source_line"
).split(",")
QUOTE_HEADERS = (
    "episode_id,episode_outcome,signal_price,signal_observed_utc,window_start_utc,"
    "window_end_utc,row_type,sip_timestamp_ns,sip_utc,ms_from_signal_observed,"
    "participant_timestamp_ns,bid,ask,bid_size,ask_size,bid_exchange,ask_exchange,"
    "conditions,indicators,tape,sequence_number"
).split(",")
SIGNALS_SHA256 = "3f283ba2ba040f8525392130f2855dd369a867b26a6c4003bf556b5b633dab69"


def read_csv(path, headers):
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != headers:
            raise ValueError("export headers changed: " + path.name)
        rows = []
        for ordinal, row in enumerate(reader, 1):
            if None in row or any(value is None for value in row.values()):
                raise ValueError("malformed CSV record: " + path.name)
            rows.append({"source_pointer": {"file": path.name, "csv_record": ordinal}, "raw": row})
    return rows


def bind_exports(directory):
    """Keep all 94 signals; only the 27 primary signals own episode rows.

    csv_record is a one-based DATA-record ordinal, not a physical line number:
    quoted multiline log/payload fields make physical-line inference unsafe.
    """
    directory = Path(directory)
    manifest = directory / "MOMBUFFER1-EXPORT-MANIFEST-20261021.md"
    declared = dict(re.findall(r"\| (MOMBUFFER1-EXPORT-[^ |]+\.csv) \|[^\n]*?\| ([0-9a-f]{64}) \|",
                               manifest.read_text()))
    source = directory / "MOMBUFFER1-EXPORT-SIGNALS-20261021.csv"
    if hashlib.sha256(source.read_bytes()).hexdigest() != SIGNALS_SHA256:
        raise ValueError("signals export differs from accepted 20261021 packet")
    rows = read_csv(source, SIGNAL_HEADERS)
    signals = [row for row in rows if row["raw"]["row_kind"] == "SIGNAL"]
    orders = [row for row in rows if row["raw"]["row_kind"] == "ORDER"]
    if Counter(row["raw"]["row_kind"] for row in rows) != {"SIGNAL": 94, "LOG": 365, "ORDER": 62}:
        raise ValueError("accepted packet denominator changed")
    ids = [row["raw"]["signal_logical_id"] for row in signals]
    if len(set(ids)) != 94 or not all(ids):
        raise ValueError("duplicate/missing signal identity")
    grouped, quote_rows, hashes = defaultdict(list), defaultdict(list), {}
    for row in signals:
        if row["raw"]["episode_id"]:
            grouped[row["raw"]["episode_id"]].append(row)
    quote_files = sorted(directory.glob("MOMBUFFER1-EXPORT-QUOTES-*.csv"))
    if len(quote_files) != 21:
        raise ValueError("expected all 21 accepted quote CSVs")
    for path in quote_files:
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if declared.get(path.name) != hashes[path.name]:
            raise ValueError("quote hash differs from accepted manifest: " + path.name)
        for row in read_csv(path, QUOTE_HEADERS):
            raw = row["raw"]
            if raw["episode_id"] not in grouped:
                raise ValueError("unmatched quote episode: " + path.name)
            if raw["row_type"] not in {"IN_WINDOW", "PREVAILING_BEFORE_WINDOW"}:
                raise ValueError("unknown quote row type")
            # Never reinterpret sip_timestamp_ns as local received_at.
            quote_rows[raw["episode_id"]].append(row)
    episodes = []
    for identity, members in grouped.items():
        primary = [row for row in members if row["raw"]["outcome"] != "merged_into_episode"]
        if len(primary) != 1:
            raise ValueError("episode has no unique primary signal: " + identity)
        original = primary[0]["raw"]
        quotes = quote_rows[identity]
        bounds = {(r["raw"]["signal_observed_utc"], r["raw"]["window_start_utc"],
                   r["raw"]["window_end_utc"]) for r in quotes}
        if len(bounds) != 1 or any(r["raw"]["signal_price"] != original["signal_price"] for r in quotes):
            raise ValueError("missing/mismatched episode quote window: " + identity)
        buys = [row for row in orders if row["raw"]["episode_id"] == identity
                and row["raw"]["outcome"].startswith("buy:")]
        if len(buys) > 1 or buys and buys[0]["raw"]["client_order_id"] != original["client_order_id"]:
            raise ValueError("duplicate/mismatched entry order: " + identity)
        episodes.append({"episode_id": identity, "primary": primary[0],
                         "merged_signals": [r for r in members if r not in primary],
                         "entry_order": buys, "quote_rows": quotes,
                         "quote_counts": dict(Counter(r["raw"]["row_type"] for r in quotes)),
                         "historical_replay": "UNKNOWN", "cache_read_identity": "UNKNOWN",
                         "local_receipt_and_recheck_clocks": "UNKNOWN"})
    if len(episodes) != 27 or sum(len(row["entry_order"]) for row in episodes) != 21:
        raise ValueError("episode/entry identity denominator mismatch")
    for row in signals:
        raw = row["raw"]
        if not raw["episode_id"] and raw["outcome"] != "refused_at_handoff":
            raise ValueError("unattributed offered signal")
        row["counterfactual_status"] = "UNKNOWN"
    return {"scope": "ACTUAL_EXPORT_BINDING_NOT_EXECUTOR_REPLAY", "denominator": 94,
            "signals": signals, "episodes": episodes, "episode_denominator": 27,
            "outcomes": dict(Counter(row["raw"]["outcome"] for row in signals)),
            "entry_order_denominator": 21, "all_order_rows": orders,
            "order_sides": dict(Counter(row["raw"]["outcome"].split(":")[0] for row in orders)),
            "signal_sha256": SIGNALS_SHA256, "quote_sha256": hashes,
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "manifest_discrepancy": "text says 20 durable / 22 buys / 40 sells; raw identities are 21 / 21 / 41",
            "no_double_episode_proof": "UNKNOWN: static identity uniqueness is not counterfactual execution",
            "unrun_timing_estimate": "UNKNOWN until measured; max two price commits plus guarded submitting save"}
