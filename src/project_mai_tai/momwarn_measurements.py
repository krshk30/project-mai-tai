"""Bounded, read-only MOMWARN1 observations. No connections, orders or control loop."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
from collections import OrderedDict, deque
from datetime import date, datetime, time
from typing import TextIO
from zoneinfo import ZoneInfo

from project_mai_tai.momwarn_metric_contract import paper_metric_rows

NS = 1_000_000_000
STEP_NS = 10 * NS
FRESH_NS = 20 * NS
UNKNOWN = "UNKNOWN"
METRIC_SUFFIX = "momentum-warning-metrics"
MAX_BYTES = 16_384
MAX_READ = 512
EVENT_TYPES = {"gateway_sample", "consumer_sample", "decision", "snapshot_entry", "revert"}
SAMPLE_FIELDS = (
    "sample_ns", "sample_et", "record_kind", "schema_version", "read_status", "gateway_age_ms",
    "consumer_age_ms", "gateway_instance_id", "generation", "coverage_status",
    "consumer_coverage_status", "gateway_source", "consumer_source", "coverage",
    "clock_status", "clock_skew_bound_ns", "stream_retention_coverage",
    "A_received", "A_published", "A_mean_per_s", "A_counter_status",
    "A_counter_interval_s", "A_bin_p95_per_s", "A_bin_peak_per_s", "A_observed_bins",
    "A_bin_coverage", "queue_drops", "dropped_total", "drop_delta", "lag_ms",
    "max_publish_lag_ms",
    "warning_count", "subscribed_count", "owned_warning_symbols",
    "A_subscription_requested_ns", "A_first_provider_event_ns", "A_subscription_confirmed",
    "warning_trade_coverage", "gateway_cpu_pct", "system_per_core_cpu_pct",
    "new_error_count", "probe_status", "warning_lead_p50_ms", "warning_lead_p95_ms",
    "sip_decision_p50_ms", "sip_decision_p95_ms", "sip_decision_p99_ms",
    "decision_count", "qualified_sip_count", "bucket_signal_count", "latency_coverage",
    "watchdog_revert_reason", "watchdog_revert_ns", "revert_observed_ns",
    "invalid_records_total", "collector_evictions_total", "comparison_evictions_total",
    "late_records_total",
)
DETAIL_FIELDS = (
    "gateway_instance_id", "generation", "symbol", "window_seconds", "comparison_id",
    "pair_status", "record_kind",
) + tuple(
    f"{side}_{name}"
    for side in ("warning", "snapshot")
    for name in (
        "ledger", "lineage_id", "source", "version", "observed_ns", "coverage_status",
        "dropped_total",
        "status", "cap_status", "signal_basis", "signal_sip_ns", "signal_sip_start_ns",
        "signal_sip_end_ns", "observeddecision_ns", "warning_obs_ns", "entry_ns",
        "entry_price", "outcome", "exit_ns", "exit_price", "pnl", "clock_status",
        "entry_timestamp_precision", "entry_sip_start_ns", "entry_sip_end_ns",
        "exit_timestamp_precision", "exit_sip_start_ns", "exit_sip_end_ns",
        "clock_skew_bound_ns", "sip_latency_raw_ms", "sip_latency_lower_ms",
        "sip_latency_upper_ms",
    )
)


def nonnegative(value: object) -> bool:
    return integer(value) or type(value) is float and math.isfinite(value) and value >= 0


def integer(value: object) -> bool:
    return type(value) is int and 0 <= value < 2**63


def quantile(values: list[float], fraction: float) -> float | str:
    """Nearest-rank observed-sample statistic, never a missing-population estimate."""
    if not values:
        return UNKNOWN
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def value(event: dict | None, field: str) -> object:
    result = event.get(field) if event else None
    return UNKNOWN if result is None else result


def session_window(day: date) -> tuple[int, int]:
    tz = ZoneInfo("America/New_York")
    return tuple(
        int(datetime.combine(day, clock, tz).timestamp()) * NS
        for clock in (time(4, 11), time(9, 30))
    )


def epoch(event: dict) -> tuple[str, int]:
    return event["gateway_instance_id"], event["generation"]


def fresh(event: dict | None, now_ns: int) -> dict | None:
    if event and 0 <= now_ns - event["observed_ns"] <= FRESH_NS:
        return event
    return None


def latency(event: dict) -> dict:
    result = {}
    decision = event.get("observeddecision_ns")
    if not integer(decision):
        return result
    if event.get("signal_basis") == "raw_exact":
        sip = event.get("signal_sip_ns")
        if integer(sip) and 0 < sip <= decision <= event.get("_read_ns", event["observed_ns"]):
            result["sip_latency_raw_ms"] = (decision - sip) / 1_000_000
    elif event.get("signal_basis") in {"bucket_sufficient", "integer_ms_bounds"}:
        start, end = event.get("signal_sip_start_ns"), event.get("signal_sip_end_ns")
        if (
            integer(start) and integer(end)
            and 0 < start < end <= decision <= event.get("_read_ns", event["observed_ns"])
        ):
            result["sip_latency_lower_ms"] = (decision - end) / 1_000_000
            result["sip_latency_upper_ms"] = (decision - start) / 1_000_000
    return result


class MeasurementCollector:
    """No work is performed until the owner explicitly feeds records or samples."""

    def __init__(self, day: date, *, capacity: int = 8192, comparisons: int = 2048) -> None:
        if not 1 <= capacity <= 8192 or not 1 <= comparisons <= 2048:
            raise ValueError("invalid capacity")
        self.start_ns, self.end_ns = session_window(day)
        self.capacity, self.comparison_capacity = capacity, comparisons
        self.latest: dict[str, dict] = {}
        self.counter_baseline: dict | None = None
        self.decisions: deque[dict] = deque(maxlen=capacity)
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.seen_decisions: OrderedDict[tuple, None] = OrderedDict()
        self.details: OrderedDict[tuple, dict] = OrderedDict()
        self.revert: dict | None = None
        self.invalid = self.evictions = self.comparison_evictions = 0
        self.late = 0
        self.last_sample_ns: int | None = None

    def record(self, payload: object, *, read_ns: int, stream_id: str) -> bool:
        try:
            encoded = payload if isinstance(payload, (str, bytes)) else json.dumps(
                payload, allow_nan=False
            )
            if len(encoded.encode() if isinstance(encoded, str) else encoded) > MAX_BYTES:
                raise ValueError("oversize record")
            event = json.loads(encoded)
            if (
                not isinstance(event, dict) or type(event.get("version")) is not int
                or event["version"] != 1 or event.get("event_type") not in EVENT_TYPES
                or not isinstance(event.get("source"), str) or not event["source"]
                or not isinstance(event.get("gateway_instance_id"), str)
                or not event["gateway_instance_id"] or not integer(event.get("generation"))
                or not integer(event.get("observed_ns")) or not integer(read_ns)
                or not isinstance(stream_id, str) or not 0 < len(stream_id) <= 64
                or event["observed_ns"] > read_ns
            ):
                raise ValueError("invalid v1 envelope or future observation")
            # JSON accepts NaN by default; reject it anywhere, including nested coverage.
            json.dumps(event, allow_nan=False)
            paper_rows = paper_metric_rows(event)
        except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
            self.invalid += 1
            return False
        if paper_rows is not None:
            # Two native ledger rows at most. Stable child IDs keep Redis replay
            # deduplication without truncating/colliding long original IDs.
            results = [self.record(row, read_ns=read_ns, stream_id=hashlib.sha256(
                f"{stream_id}:{index}".encode()
            ).hexdigest()) for index, row in enumerate(paper_rows)]
            return all(results)
        if stream_id in self.seen:
            return False
        event["_read_ns"] = read_ns
        self.seen[stream_id] = None
        if len(self.seen) > self.capacity:
            self.seen.popitem(last=False)
        kind = event["event_type"]
        if kind in {"gateway_sample", "consumer_sample", "revert"}:
            key = "gateway_sample" if kind == "revert" else kind
            prior = self.latest.get(key)
            if not prior or event["observed_ns"] > prior["observed_ns"]:
                self.latest[key] = event
                if key == "gateway_sample" and event["observed_ns"] < self.start_ns:
                    self.counter_baseline = event
            if kind == "revert" and (
                not self.revert or event["observed_ns"] > self.revert["observed_ns"]
            ):
                self.revert = event
        else:
            if not self._detail(event):
                return False
            if self.last_sample_ns is not None and event["observed_ns"] <= self.last_sample_ns:
                self.late += 1
            if (kind == "decision" and not event.get("native_lifecycle_update")
                    and self.start_ns <= event["observed_ns"] < self.end_ns):
                capture = event.get("observeddecision_ns")
                key = (*epoch(event), event.get("ledger"), event.get("lineage_id"),
                       capture if integer(capture) else event["observed_ns"])
                if key in self.seen_decisions:
                    return True
                self.seen_decisions[key] = None
                if len(self.seen_decisions) > self.capacity:
                    self.seen_decisions.popitem(last=False)
                if len(self.decisions) == self.capacity:
                    self.evictions += 1
                self.decisions.append(event)
        return True

    def _detail(self, event: dict) -> bool:
        if not self.start_ns <= event["observed_ns"] <= self.end_ns:
            return True
        window, ledger = event.get("window_seconds"), event.get("ledger")
        if window not in (30, 60) or type(window) is not int:
            self.invalid += 1
            return False
        expected = f"momentum_{window}s"
        if ledger not in (expected, expected + "_warn") or not all(
            isinstance(event.get(key), str) and event[key] for key in ("symbol", "lineage_id")
        ):
            self.invalid += 1
            return False
        # Explicit pairing is the only cross-detector join. No symbol/time heuristic.
        comparison = event.get("comparison_id")
        join = ("paired", comparison) if isinstance(comparison, str) and comparison else (
            "unpaired", ledger, event["lineage_id"]
        )
        key = (*epoch(event), event["symbol"], window, *join)
        side = "warning" if ledger.endswith("_warn") else "snapshot"
        record = self.details.setdefault(key, {})
        prior = record.get(side)
        if prior and prior["lineage_id"] != event["lineage_id"]:
            self.invalid += 1
            return False
        if not prior or event["observed_ns"] > prior["observed_ns"]:
            if prior and event.get("native_lifecycle_update"):
                # Sparse native updates may retain known fields only within the
                # exact source epoch/ledger/logical ID, never a proximity pair.
                event = {**prior, **{key: item for key, item in event.items() if item is not None}}
            record[side] = event
        self.details.move_to_end(key)
        if len(self.details) > self.comparison_capacity:
            self.details.popitem(last=False)
            self.comparison_evictions += 1
        return True

    def sample(self, now_ns: int, *, read_status: str, probes: dict | None = None) -> dict:
        if (
            not integer(now_ns) or not self.start_ns <= now_ns <= self.end_ns
            or (now_ns - self.start_ns) % STEP_NS
            or self.last_sample_ns is not None and now_ns <= self.last_sample_ns
        ):
            raise ValueError("sample must advance on the session ten-second grid")
        row = dict.fromkeys(SAMPLE_FIELDS, UNKNOWN)
        row.update(
            sample_ns=now_ns,
            sample_et=datetime.fromtimestamp(now_ns / NS, ZoneInfo("America/New_York")).isoformat(),
            record_kind="OBSERVATION", schema_version=1, read_status=read_status,
            invalid_records_total=self.invalid, collector_evictions_total=self.evictions,
            comparison_evictions_total=self.comparison_evictions, late_records_total=self.late,
        )
        gateway = fresh(self.latest.get("gateway_sample"), now_ns)
        consumer = fresh(self.latest.get("consumer_sample"), now_ns)
        if read_status != "OK":
            gateway = consumer = None
        if gateway:
            row.update(gateway_age_ms=(now_ns - gateway["observed_ns"]) / 1_000_000,
                       gateway_instance_id=gateway["gateway_instance_id"],
                       generation=gateway["generation"], gateway_source=gateway["source"])
            for field in (
                "coverage_status", "owned_warning_symbols", "A_subscription_requested_ns",
                "A_first_provider_event_ns", "A_subscription_confirmed", "warning_trade_coverage",
                "clock_status", "clock_skew_bound_ns",
                "coverage",
            ):
                row[field] = value(gateway, field)
            for field in ("A_received", "A_published", "queue_drops", "dropped_total", "lag_ms",
                          "max_publish_lag_ms"):
                if nonnegative(gateway.get(field)):
                    row[field] = gateway[field]
            self._rates(row, gateway, now_ns)
        else:
            self.counter_baseline = None
        if consumer and gateway and epoch(consumer) == epoch(gateway):
            row.update(consumer_age_ms=(now_ns - consumer["observed_ns"]) / 1_000_000,
                       consumer_coverage_status=value(consumer, "coverage_status"),
                       consumer_source=consumer["source"])
            for field in ("warning_count", "subscribed_count"):
                if integer(consumer.get(field)):
                    row[field] = consumer[field]
        if probes:
            for field in ("gateway_cpu_pct", "new_error_count"):
                if nonnegative(probes.get(field)):
                    row[field] = probes[field]
            for field in ("system_per_core_cpu_pct", "probe_status"):
                row[field] = value(probes, field)
        lower = self.last_sample_ns if self.last_sample_ns is not None else now_ns - STEP_NS
        self._latencies(row, lower, now_ns, gateway)
        if self.revert:
            # Retained proof, not a fresh health sample and never a control decision.
            for field in ("watchdog_revert_reason", "watchdog_revert_ns"):
                row[field] = value(self.revert, field)
            row["revert_observed_ns"] = self.revert["observed_ns"]
        self.last_sample_ns = now_ns
        return row

    def _rates(self, row: dict, event: dict, now_ns: int) -> None:
        prior = self.counter_baseline
        row["A_counter_status"] = "UNKNOWN:baseline_or_epoch"
        interval = event["observed_ns"] - prior["observed_ns"] if prior else 0
        if prior and epoch(prior) == epoch(event) and 0 < interval <= FRESH_NS:
            for field, output in (("A_received", "A_mean_per_s"), ("dropped_total", "drop_delta")):
                current, old = event.get(field), prior.get(field)
                if integer(current) and integer(old) and current >= old:
                    row[output] = (current - old) * NS / interval if field == "A_received" else (
                        current - old
                    )
                    row["A_counter_interval_s"] = interval / NS
                    if field == "A_received":
                        row["A_counter_status"] = "observed_counter_delta"
                elif field == "A_received":
                    row["A_counter_status"] = "UNKNOWN:counter_reset_or_missing"
        if not prior or event["observed_ns"] > prior["observed_ns"]:
            self.counter_baseline = event
        bins = event.get("A_rate_bins")
        if isinstance(bins, list) and len(bins) <= 20:
            seconds = {}
            seen_seconds = set()
            for item in bins:
                if (
                    not isinstance(item, list) or len(item) != 2
                    or not integer(item[0]) or not integer(item[1]) or item[0] in seen_seconds
                ):
                    row["A_bin_coverage"] = "INVALID"
                    return
                seen_seconds.add(item[0])
                if now_ns // NS - 10 <= item[0] < now_ns // NS:
                    seconds[item[0]] = item[1]
            rates = list(seconds.values())
            row.update(A_observed_bins=len(rates), A_bin_coverage=f"observed:{len(rates)}/10",
                       A_bin_p95_per_s=quantile(rates, .95),
                       A_bin_peak_per_s=max(rates) if rates else UNKNOWN)

    def _latencies(self, row: dict, lower: int, upper: int, gateway: dict | None) -> None:
        events = [event for event in self.decisions if lower < event["observed_ns"] <= upper
                  and gateway and epoch(event) == epoch(gateway)]
        leads, qualified = [], []
        buckets = 0
        for event in events:
            decision, warning = event.get("observeddecision_ns"), event.get("warning_obs_ns")
            if (
                integer(decision) and integer(warning)
                and 0 < warning <= decision <= event["_read_ns"]
            ):
                leads.append((decision - warning) / 1_000_000)
            measured = latency(event)
            buckets += int("sip_latency_lower_ms" in measured)
            bound = event.get("clock_skew_bound_ns")
            if (
                "sip_latency_raw_ms" in measured and event.get("clock_status") == "verified"
                and integer(bound) and measured["sip_latency_raw_ms"] * 1_000_000 >= bound
            ):
                qualified.append(measured["sip_latency_raw_ms"])
        row.update(decision_count=len(events) if gateway else UNKNOWN,
                   qualified_sip_count=len(qualified) if gateway else UNKNOWN,
                   bucket_signal_count=buckets if gateway else UNKNOWN,
                   latency_coverage=f"retained:{len(events)};qualified:{len(qualified)};"
                                    f"evicted_total:{self.evictions}")
        for name, numbers, fraction in (
            ("warning_lead_p50_ms", leads, .5), ("warning_lead_p95_ms", leads, .95),
            ("sip_decision_p50_ms", qualified, .5), ("sip_decision_p95_ms", qualified, .95),
            ("sip_decision_p99_ms", qualified, .99),
        ):
            row[name] = quantile(numbers, fraction)

    def comparison_rows(self) -> list[dict]:
        rows = []
        for key, sides in self.details.items():
            row = dict.fromkeys(DETAIL_FIELDS, UNKNOWN)
            row.update(gateway_instance_id=key[0], generation=key[1], symbol=key[2],
                       window_seconds=key[3], comparison_id=key[5] if key[4] == "paired" else UNKNOWN,
                       pair_status="PAIRED" if len(sides) == 2 else "COUNTERPART_UNKNOWN",
                       record_kind="OBSERVATION")
            for side, event in sides.items():
                for field in DETAIL_FIELDS:
                    prefix = side + "_"
                    if field.startswith(prefix):
                        row[field] = value(event, field[len(prefix):])
                for field, number in latency(event).items():
                    row[f"{side}_{field}"] = number
            rows.append(row)
        return rows


class ReadOnlyMetricCursor:
    """One nonblocking XREAD, using a caller-owned client; never creates a connection."""

    def __init__(self, prefix: str, cursor: str) -> None:
        if not prefix or not isinstance(cursor, str) or not cursor or cursor == "$":
            raise ValueError("explicit stream cursor required")
        self.stream, self.cursor = f"{prefix}:{METRIC_SUFFIX}", cursor

    async def read_once(self, client: object, collector: MeasurementCollector, *, now_ns: int) -> str:
        try:
            async with asyncio.timeout(1):
                response = await client.xread({self.stream: self.cursor}, count=MAX_READ)
            count = 0
            invalid_before = collector.invalid
            for stream, entries in response:
                name = stream.decode() if isinstance(stream, bytes) else stream
                if name != self.stream:
                    raise ValueError("unexpected stream")
                for stream_id, fields in entries:
                    count += 1
                    if count > MAX_READ:
                        return "UNKNOWN:read_bound"
                    record_id = stream_id.decode() if isinstance(stream_id, bytes) else stream_id
                    collector.record(fields.get("data", fields.get(b"data")),
                                     read_ns=now_ns, stream_id=record_id)
                    self.cursor = record_id
            # Saturating the bound can hide retained records; never label full coverage.
            if count == MAX_READ:
                return "UNKNOWN:read_bound"
            return "UNKNOWN:invalid_record" if collector.invalid > invalid_before else "OK"
        except Exception as exc:
            return f"UNKNOWN:read_failure:{type(exc).__name__}"


def write_csv(handle: TextIO, rows: list[dict], fields: tuple[str, ...]) -> None:
    writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: json.dumps(item, separators=(",", ":"), allow_nan=False)
                         if isinstance(item, (dict, list)) else item for key, item in row.items()})


def replay(handle: TextIO, day: date, *, record_kind: str) -> tuple[list[dict], list[dict]]:
    """Offline, chronological JSONL envelopes; a missing ten-second read is UNKNOWN."""
    if record_kind not in {"CONTROLLED", "RECORDED"}:
        raise ValueError("explicit evidence kind required")
    collector = MeasurementCollector(day)
    rows, next_ns, last_read = [], collector.start_ns, -1
    pending_status, probes = "UNKNOWN:no_read", None
    while line := handle.readline(MAX_BYTES + 1):
        if len(line.encode()) > MAX_BYTES:
            raise ValueError("oversize offline packet")
        packet = json.loads(line)
        now = packet["read_ns"]
        if not integer(now) or now < last_read:
            raise ValueError("offline read timestamps must be ordered")
        last_read = now
        while next_ns < now and next_ns <= collector.end_ns:
            row = collector.sample(next_ns, read_status=pending_status, probes=probes)
            row["record_kind"] = record_kind
            rows.append(row)
            next_ns += STEP_NS
            pending_status, probes = "UNKNOWN:no_read", None
        # Reads outside the session can establish a counter/log baseline, not entries.
        invalid_before = collector.invalid
        if "data" in packet:
            collector.record(packet["data"], read_ns=now, stream_id=packet["stream_id"])
        pending_status = packet.get("read_status", "OK")
        if collector.invalid > invalid_before:
            pending_status = "UNKNOWN:invalid_record"
        probes = packet.get("probes", probes)
    while next_ns <= collector.end_ns:
        row = collector.sample(next_ns, read_status=pending_status, probes=probes)
        row["record_kind"] = record_kind
        rows.append(row)
        next_ns += STEP_NS
        pending_status, probes = "UNKNOWN:no_read", None
    details = collector.comparison_rows()
    for row in details:
        row["record_kind"] = record_kind
    return rows, details
