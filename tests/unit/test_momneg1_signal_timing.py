"""Run this IDENTICAL driver on pinned old/new sources; main owns execution.

This is local offer latency, NOT end-to-end broker or installed VPS latency.
No old implementation is simulated/rewritten inside this driver.
"""

import json
import math
import time
from collections import Counter
from datetime import date
from types import SimpleNamespace

import pytest

from project_mai_tai.momentum_live.handoff import LiveHandoff
from project_mai_tai.momentum_paper.models import MomentumTapeRecord


@pytest.mark.parametrize("price,prior", [("5.25", "5"), ("0.79", "1.31"), ("5.25", None)])
def test_momneg1_actual_offer_timing(price, prior, record_property, capsys):
    latencies = []
    outcomes = Counter()
    for index in range(1000):
        # Construction and reset are outside the timed signal decision.
        target = LiveHandoff(None, SimpleNamespace(momentum_live_enabled=True),
            route_for=lambda symbol, now: SimpleNamespace(broker="webull", reason="controlled"))
        target.ledger.ready = True
        row = MomentumTapeRecord("event", f"signal-{index}", "momentum_30s_warn", "DETECTED",
            date(2026, 10, 22), "TEST", None, {"prior_close": prior,
                "observed_decision_ns": 100, "reference": {"end_ms": 1},
                "detect": {"end_ms": 3, "price": price}})
        started = time.perf_counter_ns()
        outcome = target.offer(row, day_volume=50000, now_ns=100)
        elapsed = time.perf_counter_ns() - started
        latencies.append(elapsed / 1_000_000)
        outcomes[outcome] += 1
        assert target.queue.qsize() <= 1
    ordered = sorted(latencies)
    report = {"scope": "local_actual_LiveHandoff.offer_not_native", "price": price,
              "prior_close": prior, "signals": len(ordered), "outcomes": dict(outcomes),
              "p50_ms": ordered[499], "p99_ms": ordered[math.ceil(.99 * len(ordered)) - 1],
              "max_ms": ordered[-1]}
    record_property("MOMNEG1_signal_timing", json.dumps(report, sort_keys=True))
    with capsys.disabled():
        print(json.dumps(report, sort_keys=True))
    assert ordered[-1] < 50
