"""MAIN SERIAL ONLY: identical old/new 60s real callback; offline ports.

Normal GC/logging/capture and real monotonic/perf clocks remain untouched.
Historical decision clocks are explicitly advanced for repeated controlled load;
this is not a tape replay, network measurement or installed/native order proof.
"""

import asyncio
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.momentum_live.admission import select_route
from project_mai_tai.momentum_live.handoff import LiveHandoff
from project_mai_tai.momentum_paper.models import MomentumTapeRecord
from project_mai_tai.services.momentum_paper_app import MomentumPaperService


RATE = 240
SECONDS = 60
EVENTS = RATE * SECONDS
SYNTHETIC_RATE_SHA256 = "98c0aa033cae510d6b2692844e649ed5591510a5b209fdf4cded9e40224592d4"
REQUIRED_CASES = {"FUFU-1016-1544", "EYPT-1019-0747", "EYPT-1019-0749",
                  "CRMT-1020-1502", "GIXI-1022-0819"}
ACTUAL_IDENTITIES = {
    "FUFU-1016-1544": ("FUFU", "2026-10-16 15:44", "0.79", "1.31"),
    "EYPT-1019-0747": ("EYPT", "2026-10-19 07:47", "0.7597", "2.99"),
    "EYPT-1019-0749": ("EYPT", "2026-10-19 07:49", "0.8355", "2.99"),
    "CRMT-1020-1502": ("CRMT", "2026-10-20 15:02", "0.8937", "1.07"),
    "GIXI-1022-0819": ("GIXI", "2026-10-22 08:19", "5.25", None),
}


def load_synthetic_rate_only():
    """Explicit controlled inputs: never route through historical validation."""
    path = Path(__file__).resolve().parents[1] / "fixtures/momneg1_synthetic_rate_only.json"
    raw = path.read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != SYNTHETIC_RATE_SHA256:
        raise ValueError("synthetic controlled input changed: main must review/pin both sides")
    data = json.loads(raw)
    if data["kind"] != "SYNTHETIC_RATE_ONLY":
        raise ValueError("explicit synthetic-only label required")
    cases = []
    for session in data["sessions"]:
        stamp = datetime.fromisoformat(session["observed_at"])
        observed = int(stamp.timestamp()) * 1_000_000_000
        end_ms = observed // 1_000_000
        for country in data["countries"]:
            for shape in data["shapes"]:
                index = len(cases)
                symbol = "RATE" + chr(65 + index // 26) + chr(65 + index % 26)
                identity = f"SYNTHETIC-{session['name']}-{country['name']}-{shape['name']}"
                route = select_route(stamp, market_open=True, is_us=country["is_us"])
                if route.route.value != country["broker"]:
                    raise ValueError("synthetic session/country route expectation mismatch")
                cases.append({"case_id": identity, "day_volume": data["day_volume"],
                    "is_us": country["is_us"], "market_open": True, "broker": country["broker"],
                    "record": {"event_key": identity, "logical_id": identity,
                        "strategy_code": "momentum_30s_warn" if index % 2 == 0 else "momentum_60s_warn",
                        "event_type": "DETECTED", "session_date": stamp.date().isoformat(),
                        "symbol": symbol, "observed_at": stamp.isoformat(), "payload": {
                            "prior_close": shape["prior_close"], "observed_decision_ns": observed,
                            "reference": {"end_ms": end_ms - 1000},
                            "detect": {"end_ms": end_ms, "price": shape["price"]}}}})
    return {"kind": data["kind"], "provenance": data["scope"], "cases": cases}, actual_sha


def load_packet(path, expected_sha):
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("exact reviewed packet SHA256 required")
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError("packet exceeds 64KiB")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("packet SHA256 mismatch")
    packet = json.loads(raw)
    if packet.get("kind") != "momneg1_retained_signal_prior_closes_v1":
        raise ValueError("wrong packet kind; no synthetic favorable fallback")
    if not packet.get("provenance") or not re.fullmatch(r"[0-9a-f]{40}", packet["source_head"]):
        raise ValueError("retained producer provenance/source head required")
    cases = packet["cases"]
    if len(cases) != len(REQUIRED_CASES) or {row["case_id"] for row in cases} != REQUIRED_CASES:
        raise ValueError("exact five real-case identities required")
    for row in cases:
        if not row.get("signal_evidence_ref") or not row.get("prior_close_evidence_ref"):
            raise ValueError("real signal and prior-close evidence references required")
        record = row["record"]
        payload = record["payload"]
        prior = Decimal(packet["prior_closes"][record["symbol"]])
        price = Decimal(payload["detect"]["price"])
        if not prior.is_finite() or prior <= 0 or not price.is_finite() or price <= 0:
            raise ValueError("actual finite positive signal and prior-close required")
        if Decimal(payload["prior_close"]) != prior:
            raise ValueError("record prior close differs from pinned memory export")
        if record["strategy_code"] not in {"momentum_30s_warn", "momentum_60s_warn"}:
            raise ValueError("real warning signal required")
        if record["event_type"] != "DETECTED":
            raise ValueError("real DETECTED record required")
        observed = payload["observed_decision_ns"]
        if type(observed) is not int or observed <= 0:
            raise ValueError("recorded decision clock missing")
        if type(row["day_volume"]) is not int or row["day_volume"] < 50000:
            raise ValueError("actual eligible cached day-volume required")
        if row["is_us"] is not None and type(row["is_us"]) is not bool:
            raise ValueError("country classification must be bool or explicit unknown")
        if row["market_open"] is not True:
            raise ValueError("recorded market-open decision required")
        stamp = datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("recorded observation timezone absent")
        symbol, minute, expected_price, expected_prior = ACTUAL_IDENTITIES[row["case_id"]]
        if (record["symbol"] != symbol or price != Decimal(expected_price)
                or stamp.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M") != minute
                or (expected_prior is not None and prior != Decimal(expected_prior))):
            raise ValueError("case identity differs from reviewer-reported actual inputs")
        if row["case_id"] == "GIXI-1022-0819" and price < prior:
            raise ValueError("actual GIXI prior-close evidence must establish up/equal day")
        route = select_route(stamp, market_open=row["market_open"], is_us=row["is_us"])
        if route.route.value != row["broker"]:
            raise ValueError("recorded country/session route mismatch")
    return packet


class NoRedis:
    """No emission worker runs: any unexpected signal-path I/O is a hard failure."""

    def __getattr__(self, name):
        raise AssertionError(f"unexpected Redis/native I/O on offer path: {name}")


def make_lane(row):
    raw = row["record"]
    stamp = datetime.fromisoformat(raw["observed_at"].replace("Z", "+00:00"))
    record = MomentumTapeRecord(raw["event_key"], raw["logical_id"], raw["strategy_code"],
        raw["event_type"], datetime.fromisoformat(raw["session_date"]).date(),
        raw["symbol"], stamp, raw["payload"])
    route_counts = Counter()

    def route_for(symbol, now):
        assert symbol == record.symbol
        decision = select_route(now, market_open=row["market_open"], is_us=row["is_us"])
        route_counts[decision.route.value] += 1
        return SimpleNamespace(broker=decision.route.value, reason=decision.reason)

    handoff = LiveHandoff(NoRedis(), SimpleNamespace(momentum_live_enabled=True), route_for=route_for)
    handoff.ledger.ready = True
    # Invoke the actual current service callback, not a replicated admission loop.
    service = object.__new__(MomentumPaperService)
    clock = [stamp]
    service._clock = lambda: clock[0]
    service._live_handoff = handoff
    service._live_day_volumes = {}
    return SimpleNamespace(row=row, record=record, service=service, handoff=handoff,
        clock=clock, routes=route_counts, offers=0, queue_high_water=0,
        queue_receipts=[], queued_ids=set())


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * fraction) - 1]


@pytest.mark.asyncio
async def test_real_signal_callback_240_per_second_60_seconds(record_property):
    """Explicit P2 synthetic mode needs no historical packet; actual mode is strict."""
    output_path = Path(os.environ["MOMNEG1_NATIVE_RESULT"])
    if output_path.exists():
        raise FileExistsError(output_path)
    result = {"scope": "controlled actual service callback and handoff, offline routing; NOT live wire",
              "status": "FAILED_OR_INCOMPLETE", "rate": RATE, "required_seconds": SECONDS,
              "decision_clock_policy": "advance selected controlled/retained clocks by real monotonic elapsed; prices/prior unchanged"}
    watcher = None
    stop = asyncio.Event()
    samples, stalls, schedule_lags = [], [], []
    lanes = []
    try:
        mode = os.environ["MOMNEG1_RATE_MODE"]
        if mode == "SYNTHETIC_RATE_ONLY":
            packet, packet_sha = load_synthetic_rate_only()
            result.update(input_mode=mode, input_sha256=packet_sha,
                          input_scope=packet["provenance"], producer_source_head=None,
                          historical_acceptance="NOT_APPLICABLE_NOT_P1_NOT_GIXI_NOT_NATIVE_PROOF")
        elif mode == "ACTUAL_PACKET_RATE":
            packet_path = os.environ["MOMNEG1_NATIVE_PACKET"]
            packet_sha = os.environ["MOMNEG1_NATIVE_PACKET_SHA256"]
            packet = load_packet(packet_path, packet_sha)
            result.update(input_mode=mode, input_sha256=packet_sha,
                          input_scope=packet["provenance"], producer_source_head=packet["source_head"],
                          historical_acceptance="RATE_CONTROL_ONLY_NOT_TAPE_REPLAY")
        else:
            raise ValueError("explicit SYNTHETIC_RATE_ONLY or ACTUAL_PACKET_RATE required")
        lanes = [make_lane(row) for row in packet["cases"]]
        result["source_sha256"] = {
            "service": hashlib.sha256(Path(__import__(MomentumPaperService.__module__, fromlist=["x"]).__file__).read_bytes()).hexdigest(),
            "handoff": hashlib.sha256(Path(__import__(LiveHandoff.__module__, fromlist=["x"]).__file__).read_bytes()).hexdigest(),
            "driver": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }

        async def monitor():
            while not stop.is_set():
                due = time.monotonic_ns() + 1_000_000
                await asyncio.sleep(.001)
                stalls.append(max(0, time.monotonic_ns() - due) / 1e6)

        watcher = asyncio.create_task(monitor())
        await asyncio.sleep(0)
        started = time.monotonic_ns()
        count = 0
        for index in range(EVENTS):
            due = started + index * 1_000_000_000 // RATE
            await asyncio.sleep(max(0, (due - time.monotonic_ns()) / 1e9))
            schedule_lags.append(max(0, time.monotonic_ns() - due) / 1e6)
            lane = lanes[index % len(lanes)]
            elapsed_ns = time.monotonic_ns() - started
            original = lane.record.payload
            observed_ns = original["observed_decision_ns"] + elapsed_ns
            shift_ms = elapsed_ns // 1_000_000
            payload = {**original, "observed_decision_ns": observed_ns,
                       "detect": {**original["detect"]}, "reference": {**original["reference"]}}
            for operand in (payload["detect"], payload["reference"]):
                for key in ("sip_ts_ms", "start_ms", "end_ms"):
                    if key in operand and operand[key] is not None:
                        operand[key] += shift_ms
            lane.clock[0] = lane.record.observed_at + timedelta(microseconds=elapsed_ns // 1000)
            row = replace(lane.record, payload=payload)
            lane.service._live_day_volumes[row.symbol] = (lane.row["day_volume"], lane.clock[0])
            before = time.perf_counter_ns()
            lane.service._offer_live_signals((row,), observed_ns)
            samples.append((time.perf_counter_ns() - before) / 1e6)
            count += 1
            lane.offers += 1
            size = lane.handoff.queue.qsize()
            lane.queue_high_water = max(lane.queue_high_water, size)
            assert size <= 1 and len(lane.handoff.ledger.episodes) <= 1
            if size:
                identity, signal = lane.handoff.queue._queue[0]
                assert signal.symbol == row.symbol and signal.signal_price == Decimal(original["detect"]["price"])
                assert signal.country_route == lane.row["broker"]
                if identity not in lane.queued_ids:
                    lane.queued_ids.add(identity)
                    lane.queue_receipts.append({"episode_id": identity, "symbol": signal.symbol,
                        "price": str(signal.signal_price), "route": signal.country_route,
                        "source_id": signal.source_id})
        await asyncio.sleep(max(0, (started + SECONDS * 1_000_000_000 - time.monotonic_ns()) / 1e9))
        duration = (time.monotonic_ns() - started) / 1e9
        result.update(seconds=duration, offers=count, offers_per_second=count / duration,
                      handler_p50_ms=percentile(samples, .50), handler_p99_ms=percentile(samples, .99),
                      max_handler_ms=max(samples), max_loop_stall_ms=max(stalls),
                      max_schedule_delay_ms=max(schedule_lags))
        assert count == EVENTS and duration >= SECONDS and count / duration >= 200
        assert max(samples) < 50 and max(stalls) < 50
        for lane in lanes:
            # Old and new outcomes may differ. No new reason label is hardcoded.
            assert sum(lane.handoff.counts.values()) == lane.offers
            assert sum(lane.routes.values()) <= lane.offers
            assert set(lane.routes) <= {lane.row["broker"]}
        assert sum(sum(lane.routes.values()) for lane in lanes) > 0
        assert sum(len(lane.queue_receipts) for lane in lanes) > 0
        result["status"] = "CONTROL_PASS_NOT_LIVE_QUALIFICATION"
    finally:
        stop.set()
        if watcher is not None:
            await watcher
        result.update(handler_samples_ms=samples, loop_stall_samples_ms=stalls,
                      schedule_delay_samples_ms=schedule_lags,
                      lanes=[{"case_id": lane.row["case_id"], "offers": lane.offers,
                              "actual_outcomes": dict(lane.handoff.counts),
                              "route_counts": dict(lane.routes), "queue_high_water": lane.queue_high_water,
                              "queue_receipts": lane.queue_receipts} for lane in lanes])
        with output_path.open("x", encoding="utf-8") as output:
            json.dump(result, output, sort_keys=True, indent=2, allow_nan=False)
            output.write("\n")
        summary = {key: value for key, value in result.items()
                   if not key.endswith("samples_ms") and key != "source_sha256"}
        record_property("MOMNEG1_NATIVE_SIGNAL", json.dumps(summary, sort_keys=True))
        print("MOMNEG1_NATIVE_SIGNAL=" + json.dumps(summary, sort_keys=True))
