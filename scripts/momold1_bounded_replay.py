"""Offline MOMOLD1 comparison of reconstructed inputs; NEVER original-feed parity.

Run only in the integrator's released serial slot. No provider, database or Redis
connection is opened. Captured trades generate synthetic one-second bars and
five-second snapshots. All transport/coverage clocks below are model assumptions.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import csv
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
import gzip
import hashlib
import heapq
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from project_mai_tai.events import (
    MarketSnapshotPayload, SnapshotBatchEvent, SnapshotBatchPayload,
    TradeTickEvent, TradeTickPayload,
)
from project_mai_tai.momentum_paper.conditions import condition_snapshot_from_payload
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import MOMENTUM_WARNING_STRATEGIES
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.momentum_paper.warning_window import AggregateObservation, CoverageEvidence
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings


START_NS = 1792483200 * 1_000_000_000  # 2026-10-20 08:00Z
END_NS = START_NS + 16 * 3600 * 1_000_000_000
EPOCH = ("SYNTH_RECONSTRUCTED_NOT_ORIGINAL", 1)


def ns(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("timezone required")
    delta = stamp.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_events(path):
    with path.open() as handle:
        for line in handle:
            yield json.loads(line)


def spool(root, target, conditions, symbols):
    """Bound memory to one bucket; merge symbol spools by synthetic event time.

    Equal-time input rows retain CSV order. That order is explicitly UNKNOWN as
    an original receipt tie-break. received_at is audited, never used as A timing.
    """
    paths, counts = {}, Counter()
    previous_symbol, previous_ns = None, 0
    handle, bucket = None, None

    def emit_bar():
        if bucket is not None:
            handle.write(json.dumps([bucket["end_ms"] * 1_000_000, 1,
                                     previous_symbol, "aggregate", bucket]) + "\n")

    try:
        with gzip.open(root / "trades_1020_massive.csv.gz", "rt", newline="") as source:
            reader = csv.DictReader(source)
            fields = list(reader.fieldnames or [])
            for row in reader:
                symbol, stamp = row["symbol"], ns(row["event_ts"])
                if row["provider"] != "massive" or symbol not in symbols:
                    raise ValueError("provider or symbol outside supplied manifest")
                if not START_NS <= stamp < END_NS:
                    raise ValueError("trade outside bounded window")
                if symbol != previous_symbol:
                    emit_bar()
                    if handle is not None:
                        handle.close()
                    if symbol in paths:
                        raise ValueError("input must be grouped by symbol")
                    paths[symbol] = target / f"symbol-{len(paths)}.jsonl"
                    handle = paths[symbol].open("w")
                    previous_symbol, previous_ns, bucket = symbol, 0, None
                if stamp < previous_ns:
                    raise ValueError("per-symbol event time regressed")
                counts["event_ts_ties"] += stamp == previous_ns
                previous_ns = stamp
                codes = tuple(int(code) for code in row["conditions"].split(",") if code)
                eligible, _ = conditions.classify(codes)
                price, size = Decimal(row["price"]), int(row["size"])
                start_ms = stamp // 1_000_000_000 * 1000
                if bucket is not None and start_ms != bucket["start_ms"]:
                    emit_bar()
                    bucket = None
                # The aggregate reconstruction uses the retained engine's condition
                # classifier; equivalence to actual provider A construction is UNKNOWN.
                if eligible:
                    if bucket is None:
                        bucket = dict(symbol=symbol, start_ms=start_ms, end_ms=start_ms + 1000,
                                      open=str(price), high=str(price), low=str(price),
                                      close=str(price), volume=0)
                    bucket.update(high=str(max(Decimal(bucket["high"]), price)),
                                  low=str(min(Decimal(bucket["low"]), price)), close=str(price),
                                  volume=bucket["volume"] + size)
                handle.write(json.dumps([stamp, 2, symbol, "trade", {
                    "price": str(price), "size": size, "conditions": codes,
                }]) + "\n")
                counts["trades"] += 1
                counts["eligible_trades"] += eligible
                counts["received_at_present"] += bool(row.get("received_at"))
                counts["cumulative_volume_present"] += bool(row.get("cumulative_volume"))
            emit_bar()
    finally:
        if handle is not None:
            handle.close()
    return paths, {"trade_fields": fields, **counts}


class FakeRedis:
    def __init__(self):
        self.owners = {}
        self.requests = 0
        self.trace = hashlib.sha256()

    async def xadd(self, stream, fields, **kwargs):
        if stream.endswith(":market-data-subscriptions"):
            row = json.loads(fields["data"])["payload"]
            self.owners[row["consumer_name"]] = set(row["symbols"])
            if len(set().union(*self.owners.values())) > 16:
                raise AssertionError("SYNTH shared subscription cap exceeded")
            self.trace.update(json.dumps(row, sort_keys=True).encode())
            self.requests += 1
        return "0-0"


def make_service(ready, conditions, *, legacy):
    settings = Settings(_env_file=None, momentum_paper_enabled=True,
                        momentum_live_enabled=False, market_data_momentum_warning_enabled=True)
    service = MomentumPaperService(settings, redis_client=FakeRedis())
    # Exercise production Q/T mirroring without constructing a LIVE handoff.
    service.settings.momentum_live_enabled = True
    closes = {key: Decimal(value) for key, value in ready["prior_closes"].items()}
    service._engine = MomentumPaperEngine(
        prior_closes=closes, condition_version=conditions.version,
        coverage_started_ms=START_NS // 1_000_000,
        strategy_windows=None if legacy else {}, validation_window=service._validation_window,
    )
    service._warning = MomentumWarningConsumer(
        prior_closes=closes, condition_version=conditions.version,
        coverage_started_ms=START_NS // 1_000_000, validation_window=service._validation_window,
    )
    service._condition_snapshot = conditions
    service._session_date = datetime.fromtimestamp(START_NS // 1_000_000_000, UTC).date()
    service.replay_records = []

    async def persist(records):
        service.replay_records.extend(asdict(row) for row in records
                                      if row.event_type != "PATH_PRINT"
                                      and row.strategy_code in MOMENTUM_WARNING_STRATEGIES)

    async def metrics(**fields):
        pass

    service._persist, service._warning_metrics = persist, metrics
    return service


async def replay(paths, ready, conditions):
    services = [make_service(ready, conditions, legacy=value) for value in (True, False)]
    snapshots, sequence = {}, 0
    now = START_NS
    pulses = ([stamp, 0, "", "pulse", None]
              for stamp in range(START_NS, END_NS + 1, 1_000_000_000))
    merged = heapq.merge(pulses, *(read_events(path) for path in paths.values()),
                        key=lambda row: (row[0], row[1], row[2]))
    with patch("project_mai_tai.services.momentum_paper_app.wall_time.time_ns", lambda: now):
        for now, _, symbol, kind, payload in merged:
            clock = datetime.fromtimestamp(now // 1_000_000_000, UTC).replace(
                microsecond=(now % 1_000_000_000) // 1000)
            for service in services:
                service._clock = lambda clock=clock: clock
                service._warning.set_coverage(CoverageEvidence(
                    EPOCH, START_NS // 1_000_000, now // 1_000_000, 0, True,
                    lineage="SYNTH_RECONSTRUCTED_ASSUMED_CONTINUOUS",
                ))
            if kind == "trade":
                snapshots[symbol] = MarketSnapshotPayload(
                    symbol=symbol, last_trade_price=Decimal(payload["price"]),
                    last_trade_timestamp_ns=now,
                )
                event = TradeTickEvent(source_service="market-data-gateway", payload=TradeTickPayload(
                    symbol=symbol, price=Decimal(payload["price"]), size=payload["size"],
                    timestamp_ns=now, conditions=[str(code) for code in payload["conditions"]],
                    conditions_present=True,
                ))
                for service in services:
                    await service._handle_gateway_event(event.model_dump_json())
            elif kind == "aggregate":
                bar = AggregateObservation(
                    **{key: Decimal(value) if key in {"open", "high", "low", "close"} else value
                       for key, value in payload.items()},
                    received_utc_ns=now, published_utc_ns=now,
                    gateway_instance_id=EPOCH[0], generation=EPOCH[1], sequence=sequence, dropped_total=0,
                )
                sequence += 1
                encoded = json.dumps({**asdict(bar), "version": 1, "event_type": "aggregate"}, default=str)
                for service in services:
                    await service._handle_warning_event(encoded)
            else:
                for service in services:
                    await service._persist(service._engine.advance_clock(now // 1_000_000))
                    await service._persist(service._warning.engine.advance_clock(now // 1_000_000))
                    service._warning.expire_quiet(observed_ms=now // 1_000_000,
                                                  legacy_active=service._engine.active_symbols)
                    await service._sync_gateway_subscriptions()
                if (now - START_NS) % 5_000_000_000 == 0:
                    event = SnapshotBatchEvent(source_service="market-data-gateway",
                        payload=SnapshotBatchPayload(snapshots=list(snapshots.values()), completed_at=clock))
                    for service in services:
                        await service._handle_gateway_event(event.model_dump_json())
        for service in services:
            await service._persist(service._warning.engine.close_session(END_NS // 1_000_000))
    return services, sequence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.export
    hashes = {}
    for line in (root / "SHA256SUMS").read_text().splitlines():
        expected, name = line.split(maxsplit=1)
        name = name.lstrip("*")
        if Path(name).name != name:
            raise ValueError("manifest must contain basenames only")
        hashes[name] = digest(root / name)
        if hashes[name] != expected:
            raise ValueError(f"hash mismatch: {name}")
    if set(hashes) != {
        "session_ready_1020.json", "momentum_paper_events_1020.csv",
        "captured_symbols_superset_1020.txt", "trades_1020_massive.csv.gz",
        "quotes_1020_massive.csv.gz", "counts.txt",
    }:
        raise ValueError("unexpected or incomplete checksum manifest")
    ready = json.loads((root / "session_ready_1020.json").read_text())
    conditions = condition_snapshot_from_payload(ready["condition_snapshot"])
    symbols = set((root / "captured_symbols_superset_1020.txt").read_text().splitlines())
    if len(symbols) != 52 or len(ready["prior_closes"]) != 12197:
        raise ValueError("unexpected export universe")
    with (root / "momentum_paper_events_1020.csv").open(newline="") as handle:
        actual = list(csv.DictReader(handle))
    if len(actual) != 98:
        raise ValueError("unexpected retained event count")
    with gzip.open(root / "quotes_1020_massive.csv.gz", "rt", newline="") as handle:
        quotes = csv.DictReader(handle)
        quote_fields = quotes.fieldnames
        quote_count = sum(1 for _ in quotes)
    with tempfile.TemporaryDirectory(prefix="momold1-reconstructed-") as temporary:
        paths, census = spool(root, Path(temporary), conditions, symbols)
        if census["trades"] != 1_836_118 or quote_count != 752_660:
            raise ValueError("unexpected retained market data row counts")
        services, bar_count = asyncio.run(replay(paths, ready, conditions))
    before, after = [service.replay_records for service in services]
    detections = [Counter(row["event_type"] for row in records)["DETECTED"] for records in (before, after)]
    same = before == after
    result = "MATCH_BOUNDED_RECONSTRUCTION" if same and min(detections) else (
        "INCONCLUSIVE_NO_WARNING_DETECTIONS" if same else "DIFFERENT_BOUNDED_RECONSTRUCTION")
    report = {
        "contract": "52_CAPTURED_SYMBOLS_RECONSTRUCTED_MARKET_DATA_ONLY",
        "result": result, "original_full_universe_parity": "UNKNOWN",
        "original_transport_parity": "UNKNOWN", "reviewer_acceptance": "REQUIRED_NOT_GRANTED_BY_SCRIPT",
        "input_sha256": hashes, "census": census, "captured_manifest": sorted(symbols),
        "harness_sha256": digest(Path(__file__)),
        "model_parameters": {
            "validation_window": asdict(services[0]._validation_window),
            "condition_version": conditions.version, "shared_owner_cap": 16,
            "window_symbol_bound": services[0]._warning.max_window_symbols,
            "live_subscription_mirroring": True, "live_handoff": "ABSENT",
            "legacy_strategy_windows": services[0]._engine.strategy_windows,
            "dormant_strategy_windows": services[1]._engine.strategy_windows,
        },
        "symbols_with_trades": sorted(paths), "session_universe": len(ready["prior_closes"]),
        "captured_symbols_without_trades": sorted(symbols - set(paths)),
        "session_symbols_without_captured_market_data": sorted(set(ready["prior_closes"]) - set(paths)),
        "retained_detected_symbols_without_trades": sorted({row["symbol"] for row in actual
            if row["event_type"] == "DETECTED"} - set(paths)),
        "quotes": {"rows": quote_count, "fields": quote_fields, "use": "CENSUS_ONLY_NO_LIVE_EXECUTION"},
        "synthetic_bars": bar_count,
        "assumptions": [
            "One-second OHLC built from captured condition-eligible trades, no empty buckets fabricated",
            "Provider A aggregate construction, corrections and original snapshot values UNKNOWN",
            "Event-time callbacks; fresh A receipt/publication/decision clocks and continuous coverage SYNTH",
            "CSV order for per-symbol timestamp ties; aggregate then trade; lexical cross-symbol tie order SYNTH",
            "One-second expiry pulses before bars/trades, five-second snapshot cadence/phase SYNTH",
            "Captured received_at retained as raw field only; not original warning callback clocks",
            "No subscription ACK, original raw subscription intervals, LIVE orders or LIVE outcomes modeled",
            "Legacy-default engine versus dormant engine through current service callbacks; not two source-head qualification",
            "Day volume/cumulative snapshot volumes UNKNOWN; real LIVE volume gate not evaluated",
        ],
        "unknown_fields": ["original_A_OHLC", "gateway_instance_id", "generation", "sequence",
                           "dropped_total", "A_received_utc_ns", "A_published_utc_ns", "consumer_observed_ns",
                           "redis_offsets", "subscription_ack_ns", "original_all_symbol_receipt_order",
                           "10_20_effective_warning_settings"],
        "covered_fields": ["SESSION_READY.prior_closes", "SESSION_READY.prior_close_date",
                           "SESSION_READY.condition_snapshot", "retained_event_payloads",
                           "trade.id", "trade.provider", "trade.symbol", "trade.event_ts",
                           "trade.received_at", "trade.price", "trade.size", "trade.exchange",
                           "trade.conditions", "quote_schema_and_row_count"],
        "retained_actual_events": dict(Counter(row["event_type"] for row in actual)),
        "retained_warning_events": [
            {**{key: row[key] for key in ("event_key", "logical_id", "strategy_code", "event_type", "symbol", "observed_at")},
             "payload": json.loads(row["payload"])}
            for row in actual if row["strategy_code"] in MOMENTUM_WARNING_STRATEGIES
        ],
        "RUMBW": {"scope": "EVENT_ONLY_NO_CAPTURE_NO_MARKET_DATA_PARITY", "captured": "RUMBW" in symbols,
                  "events": [row for row in actual if row["symbol"] == "RUMBW"]},
        "reconstructed_warning_records": {"legacy_enabled": before, "legacy_dormant": after},
        "subscription_requests": [service.redis.requests for service in services],
        "subscription_trace_sha256": [service.redis.trace.hexdigest() for service in services],
        "warning_detections": detections, "exact_warning_records_equal": same,
        "actual_warning_identity_comparison": "NOT_PARITY: retained identities listed separately; reconstructed lineage differs",
        "synthetic_cap_eviction_controls": "SEPARATE tests/unit/test_momold1_retirement.py; not executed by harness",
    }
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2, default=str)
        handle.write("\n")
    print(json.dumps({"result": result, "output": str(args.output), "sha256": digest(args.output)}))
    return 0 if result == "MATCH_BOUNDED_RECONSTRUCTION" else 1


if __name__ == "__main__":
    raise SystemExit(main())
