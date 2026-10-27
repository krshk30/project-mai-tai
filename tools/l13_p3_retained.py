"""Stream the delivered L13 Redis capture; imports application code only for replay.

Unknown pre-capture state is never reconstructed from favorable log outcomes.
The15:31:49 ruling requires OLD/NEW cold parity through all120 batches and raw
payload byte equality. Native historical equality is explicitly not claimed.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import copy
import gzip
import hashlib
import json
import logging
from pathlib import Path
import re
from unittest.mock import patch
from zoneinfo import ZoneInfo

MAX_LINE = 8 * 1024 * 1024
MAX_RAW = 760 * 1024 * 1024
MAX_LOG = 1024 * 1024
EASTERN = ZoneInfo("America/New_York")
ALERT = re.compile(r"^(.{23}) INFO \[[^]]+\] \[ALERT\] \[([A-Z_0-9]+)\] (\S+) @ \$([0-9.]+) \| (\{.*\})$")
SUMMARY = re.compile(r"^(.{23}) INFO \[strategy-engine\] snapshot batch processed \| alerts=(\d+) confirmed=(\d+)$")
DETAIL = re.compile(r"'([a-z_0-9]+)': (True|False|'[^']*'|-?\d+(?:\.\d+)?)")


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path):
    manifest = json.loads(Path(path).read_text())
    if manifest["kind"] != "L13_RETAINED_REDIS_RAW_20261022":
        raise ValueError("unrecognized retained packet")
    directory = Path(manifest["directory"])
    for name in ("archive", "strategy_log"):
        binding = manifest[name]
        target = directory / binding["file"]
        if target.stat().st_size != binding["bytes"] or digest_file(target) != binding["sha256"]:
            raise ValueError(f"retained {name} size/SHA256 differs")
    return manifest


def redis_id(value):
    text = value.decode("ascii").strip()
    if not re.fullmatch(r"[0-9]+-[0-9]+", text):
        raise ValueError("invalid raw Redis stream id")
    return text, tuple(map(int, text.split("-")))


@dataclass
class RawBatch:
    index: int
    stream_id: str
    next_stream_id: str | None
    raw_json: bytes
    envelope: dict


def iter_raw_batches(path, expected):
    """One JSON frame plus the next small Redis ID, never a 120-batch list."""
    digest = hashlib.sha256()
    raw_bytes = 0
    count = 0
    previous = None
    first_id = None
    last_id = None
    previous_time = None

    def read_line(source, bound):
        nonlocal raw_bytes
        line = source.readline(bound + 1)
        if len(line) > bound:
            raise ValueError("raw Redis line exceeds bounded frame")
        raw_bytes += len(line)
        if raw_bytes > MAX_RAW:
            raise ValueError("retained raw byte bound exceeded")
        digest.update(line)
        return line

    with gzip.open(path, "rb") as source:
        identifier = read_line(source, 128)
        while identifier:
            text, numeric = redis_id(identifier)
            if previous is not None and numeric <= previous:
                raise ValueError("Redis stream identity did not increase")
            if read_line(source, 128) != b"data\n":
                raise ValueError("raw Redis field is not exactly data")
            encoded = read_line(source, MAX_LINE)
            item = json.loads(encoded)
            if not {"event_id", "event_type", "source_service", "produced_at", "payload"} <= item.keys():
                raise ValueError("recorded envelope fields missing; no invented defaults")
            if item["event_type"] != "snapshot_batch":
                raise ValueError("non-snapshot event in retained stream")
            payload = item["payload"]
            if not {"snapshots", "reference_data", "completed_at"} <= payload.keys():
                raise ValueError("recorded payload fields missing")
            produced = datetime.fromisoformat(item["produced_at"].replace("Z", "+00:00"))
            if produced.tzinfo is None or (previous_time is not None and produced <= previous_time):
                raise ValueError("recorded produced_at absent/naive/out of order")
            if len(payload["snapshots"]) != expected["snapshots_per_batch"]:
                raise ValueError("actual snapshot width differs")
            if len(payload["reference_data"]) != expected["references_per_batch"]:
                raise ValueError("actual reference width differs")
            count += 1
            if count > expected["batches"]:
                raise ValueError("extra retained batch")
            following = read_line(source, 128)
            next_id = redis_id(following)[0] if following else None
            first_id = text if first_id is None else first_id
            last_id = text
            yield RawBatch(count, text, next_id, encoded, item)
            # Release the current decoded frame before decoding the next frame.
            del item, payload, encoded
            identifier = following
            previous = numeric
            previous_time = produced
    if (count != expected["batches"] or raw_bytes != expected["raw_bytes"]
            or digest.hexdigest() != expected["raw_sha256"]
            or first_id != expected["first_id"] or last_id != expected["last_id"]):
        raise ValueError("retained EOF count/bounds/raw SHA256 mismatch")


def number(value):
    return format(Decimal(str(value)).normalize(), "f")


def parse_details(text):
    result = {}
    body = text[1:-1]
    cursor = 0
    for match in DETAIL.finditer(body):
        if body[cursor:match.start()].strip(" ,"):
            raise ValueError("unrecognized recorded alert details")
        key, value = match.groups()
        if key in result:
            raise ValueError("duplicate recorded alert detail")
        if value in {"True", "False"}:
            result[key] = value == "True"
        elif value.startswith("'"):
            result[key] = value[1:-1]
        else:
            result[key] = number(value)
        cursor = match.end()
    if body[cursor:].strip(" ,"):
        raise ValueError("unparsed recorded alert detail suffix")
    return result


def log_time(text):
    # Packet's UTC log clock is explicit; 18:25Z is 14:25ET, not local UTC guessing.
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)


def parse_strategy_log(path):
    if Path(path).stat().st_size > MAX_LOG:
        raise ValueError("strategy log exceeds bounded evidence file")
    groups = []
    pending = []
    for line in Path(path).read_text().splitlines():
        alert = ALERT.fullmatch(line)
        summary = SUMMARY.fullmatch(line)
        if alert:
            clock, kind, symbol, price, details = alert.groups()
            pending.append({"at": log_time(clock), "type": kind, "ticker": symbol,
                            "price": number(price), "details": parse_details(details)})
        elif summary:
            clock, count, confirmed = summary.groups()
            if int(count) != len(pending):
                raise ValueError("logged alert count and alert lines disagree")
            end = log_time(clock)
            groups.append({"processed_at": end, "decision_at": pending[0]["at"] if pending else end,
                           "confirmed": int(confirmed), "alerts": pending})
            pending = []
        elif "[ALERT]" in line or "snapshot batch processed" in line:
            raise ValueError("unrecognized authoritative scanner log line")
    if pending:
        raise ValueError("unclosed final scanner alert group")
    if not groups:
        raise ValueError("no actual scanner summaries")
    return groups


def match_log_group(batch, groups):
    lower = datetime.fromtimestamp(int(batch.stream_id.split("-")[0]) / 1000, UTC)
    upper = (datetime.fromtimestamp(int(batch.next_stream_id.split("-")[0]) / 1000, UTC)
             if batch.next_stream_id else None)
    matches = [group for group in groups if group["decision_at"] >= lower
               and (upper is None or group["decision_at"] < upper)]
    if len(matches) != 1:
        raise ValueError("scanner log association ambiguous/missing; no native event-id claim")
    return matches[0]


def canonical_alert(alert):
    details = {}
    for key, value in alert["details"].items():
        details[key] = value if isinstance(value, (bool, str)) else number(value)
    return {"type": alert["type"], "ticker": alert["ticker"],
            "price": number(alert["price"]), "details": details}


def ready_for_recorded_comparison(first_produced, produced, warmup):
    # Retained as a history-readiness diagnostic, NOT native-state equivalence.
    return (produced - first_produced).total_seconds() >= 300 and warmup["squeeze_5min_ready"]


def payload_wire_bytes(encoded):
    """Return the original top-level JSON payload span, with NO re-encoding."""
    raw = encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded
    if len(raw.encode("utf-8")) > MAX_LINE:
        raise ValueError("encoded batch exceeds bound")
    decoder = json.JSONDecoder()
    offset = 0

    def whitespace(index):
        while index < len(raw) and raw[index] in " \t\r\n":
            index += 1
        return index

    offset = whitespace(offset)
    if offset >= len(raw) or raw[offset] != "{":
        raise ValueError("batch envelope must be an object")
    offset += 1
    seen = set()
    payload = None
    while True:
        offset = whitespace(offset)
        key, end = decoder.raw_decode(raw, offset)
        if not isinstance(key, str) or key in seen:
            raise ValueError("invalid/duplicate envelope key")
        seen.add(key)
        offset = whitespace(end)
        if offset >= len(raw) or raw[offset] != ":":
            raise ValueError("missing envelope separator")
        start = whitespace(offset + 1)
        value, end = decoder.raw_decode(raw, start)
        if key == "payload":
            if not isinstance(value, dict):
                raise ValueError("payload must be an object")
            payload = raw[start:end].encode("utf-8")
        offset = whitespace(end)
        if offset < len(raw) and raw[offset] == "}":
            if whitespace(offset + 1) != len(raw) or payload is None:
                raise ValueError("missing payload/trailing envelope data")
            return payload
        if offset >= len(raw) or raw[offset] != ",":
            raise ValueError("invalid envelope delimiter")
        offset += 1


def assert_cold_parity(old, new, old_raw, new_raw, stream_id):
    before = payload_wire_bytes(old_raw)
    after = payload_wire_bytes(new_raw)
    assert before == after, f"publisher payload bytes differ at {stream_id}"
    assert old == new, f"old/new real scanner outputs differ at {stream_id}"
    return hashlib.sha256(before).hexdigest()


def assert_full_coverage(result, expected_batches):
    assert result.get("retained_input_verified_through_eof") is True, "raw EOF provenance incomplete"
    assert len(result["comparisons"]) == expected_batches == 120, "all120 comparisons required"
    assert len({row["stream_id"] for row in result["comparisons"]}) == 120, "duplicate batch proof"
    assert all(row["scanner_parity"] and row["payload_byte_parity"]
               for row in result["comparisons"]), "incomplete batch proof"


class ScannerConsumer:
    """Real service dispatch; cold start is explicitly UNKNOWN historical state."""

    def __init__(self, clock):
        from project_mai_tai.services.strategy_engine_app import StrategyEngineService
        from tools.l13_g2_controls import TradeSink, offline_settings

        self.service = StrategyEngineService(settings=offline_settings(), redis_client=TradeSink(),
                                             now_provider=lambda: clock[0])
        self.emitted = []
        self.targets = []
        self.service._preload_manual_stop_state = lambda: None
        self.service._load_schwab_ineligible_symbols_by_strategy = lambda: {}
        self.service._load_scanner_blacklist_symbols = lambda: set()

        async def save_targets(**targets):
            self.targets.append(targets)

        async def no_persistence():
            return None

        self.service._sync_subscription_targets = save_targets
        self.service._publish_strategy_state_snapshot = no_persistence
        original = self.service.state.alert_engine.check_alerts

        def observe(*args, **kwargs):
            result = original(*args, **kwargs)
            self.emitted.append(result)
            return result

        self.service.state.alert_engine.check_alerts = observe

    async def consume(self, encoded):
        self.emitted.clear()
        self.targets.clear()
        await self.service._handle_stream_message("l13-controlled:snapshot-batches", {"data": encoded})
        assert len(self.emitted) == len(self.targets) == 1
        state = self.service.state
        return copy.deepcopy({"emitted_alerts": self.emitted[0], "confirmed": state.all_confirmed,
                "top_confirmed": state.current_confirmed, "warmup": state.alert_warmup,
                "alerts": state.today_alerts, "recent_alerts": state.recent_alerts,
                "watchlist": state.retained_watchlist, "subscriptions": self.targets[0]})


async def replay_retained(manifest_path, result):
    """MAIN ONLY. Never reads all raw envelopes or all event models into a list."""
    from project_mai_tai.events import SnapshotBatchEvent
    from tools.l13_g2_controls import (
        LEGACY_PUBLISHER_SHA256, MarketDataPublisher, ProductionWorkerAdapter,
        recorded_snapshot_completion, records_from_batch,
    )
    from project_mai_tai.market_data import publisher

    manifest = load_manifest(manifest_path)
    assert digest_file(publisher.__file__) == LEGACY_PUBLISHER_SHA256, "legacy publisher source pin changed"
    directory = Path(manifest["directory"])
    groups = parse_strategy_log(directory / manifest["strategy_log"]["file"])
    result.update(initial_state="UNKNOWN: not stored; cold reconstruction from actual batches only",
                  log_association="INFERRED unique Redis-publication/decision-time interval, not native event id",
                  packet=manifest, native_live_equivalence="UNKNOWN; comparison withdrawn by Claude15:31:49",
                  comparisons=[],
                  publication_worker_heartbeats=[],
                  actual_prior_log_groups=len(groups), completion_clock="recorded completed_at constructor input in BOTH publishers",
                  raw_payload_comparison="literal serialized JSON span, including completed_at; no normalization")
    clock = [None]  # Bound to the first actual log decision BEFORE constructing consumers.
    adapter = ProductionWorkerAdapter()
    with ExitStack() as bindings:
        for target in ("project_mai_tai.strategy_core.snapshot_utils.now_eastern",
                       "project_mai_tai.strategy_core.top_gainers.now_eastern",
                       "project_mai_tai.services.strategy_engine_app.now_eastern"):
            bindings.enter_context(patch(target, lambda: clock[0]))
        bindings.enter_context(patch("project_mai_tai.services.strategy_engine_app.configure_logging",
                                    lambda name, level="INFO": logging.getLogger(name)))
        before = after = None
        for batch in iter_raw_batches(directory / manifest["archive"]["file"], manifest["raw"]):
            original = SnapshotBatchEvent.model_validate(batch.envelope)
            group = match_log_group(batch, groups)
            assert group["confirmed"] == 0, "packet's confirmed=0 disposition contradicted"
            assert group["decision_at"] >= original.produced_at
            clock[0] = group["decision_at"].astimezone(EASTERN)
            if before is None:
                before, after = ScannerConsumer(clock), ScannerConsumer(clock)
            snapshots = records_from_batch(original)
            with recorded_snapshot_completion(original.payload.completed_at):
                raw_old = MarketDataPublisher(None, "l13-controlled", "market-data-gateway")._encode_snapshot_batch(
                    snapshots, original.payload.reference_data)
            raw_new = await adapter.publish_recorded_batch(
                snapshots=snapshots, reference_data=original.payload.reference_data,
                completed_at=original.payload.completed_at)
            result["publication_worker_heartbeats"].append({
                "source_stream_id": batch.stream_id, "receipts": adapter.last_recorded_heartbeats})
            # Compare actual wire spans BEFORE Pydantic could normalize numbers/order.
            assert payload_wire_bytes(raw_old) == payload_wire_bytes(raw_new), f"payload bytes differ at {batch.stream_id}"
            legacy = SnapshotBatchEvent.model_validate_json(raw_old)
            candidate = SnapshotBatchEvent.model_validate_json(raw_new)
            assert legacy.payload == candidate.payload
            assert candidate.payload.snapshots == original.payload.snapshots
            assert candidate.payload.reference_data == original.payload.reference_data
            assert candidate.payload.completed_at == original.payload.completed_at
            # Replay clock only: never claim the worker emitted this historical envelope.
            candidate = candidate.model_copy(update={"produced_at": original.produced_at})
            legacy = legacy.model_copy(update={"produced_at": original.produced_at})
            old = await before.consume(legacy.model_dump_json())
            new = await after.consume(candidate.model_dump_json())
            payload_sha = assert_cold_parity(old, new, raw_old, raw_new, batch.stream_id)
            row = {"stream_id": batch.stream_id, "source_event_id": str(original.event_id),
                   "produced_at": original.produced_at.isoformat(),
                   "recorded_decision_at": group["decision_at"].isoformat(),
                   "warmup": old["warmup"], "recorded_confirmed": group["confirmed"],
                   "scanner_parity": True, "payload_byte_parity": True, "payload_sha256": payload_sha,
                   "observed_alerts": [canonical_alert(alert) for alert in old["emitted_alerts"]],
                   "recorded_alerts_diagnostic_only": [{key: value for key, value in alert.items() if key != "at"}
                                                       for alert in group["alerts"]],
                   "observed_confirmed": len(old["confirmed"]), "watchlist": old["watchlist"],
                   "native_equivalence": "UNKNOWN; historical state not stored"}
            result["comparisons"].append(row)
            # No list of 120 original/candidate models is retained.
            del old, new, original, legacy, candidate, snapshots, raw_old, raw_new, batch
    result["retained_input_verified_through_eof"] = True
    assert_full_coverage(result, manifest["raw"]["batches"])
    result["actual_positive_confirmed"] = "NONE recorded; no positive witness claimed"
    result["actual_positive_ten_minute_alert"] = "NONE recorded; no positive witness claimed"


async def qualification(manifest_path, output):
    from tools.l13_g2_controls import write_fresh_result

    result = {"gate": "L13-P3-COLD-PARITY-153149", "gate_status": "FAILED_OR_INCOMPLETE"}
    try:
        await replay_retained(manifest_path, result)
        result["gate_status"] = "PASSED_120_BATCH_COLD_PUBLISHER_SCANNER_PARITY_NOT_NATIVE_LIVE_EQUIVALENCE"
    finally:
        write_fresh_result(output, result)
