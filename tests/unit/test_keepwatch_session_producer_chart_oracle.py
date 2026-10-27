"""Four actual-input mathematical gates; separate Main claim, NOT case replay.

The independent oracle is immutable pre-fix source on retained Massive candles.
The target is the real ledger/build_session_line projection with both retained
producer candidates at the approved reconstructed clocks. No native permission,
owner/entry outcome, original HTTP arrival or 30-card acceptance follows.
"""

import csv
from datetime import datetime
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.market_data.keepwatch_producer import SessionProducerPins
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.strategy_core.session_line_restore import (
    RebuildInput, RebuildResult, SessionLineRestoration, build_session_line,
)

ORACLE = Path("/private/tmp/oct24-l11-daic-chart-oracle-20261024/src/project_mai_tai/backtest/atr_oracle.py")
ORACLE_SHA = "22ead27b69dc89cafc75e6dc1c97238a9cd8f82d48b4e4c2665bdb4889fa60d7"
PACKET = Path("/private/tmp/OCT23-L11-C124-OLD17-INPUT.json")
PACKET_SHA = "680dcd4a073f47549de3ec5eb79f2790cd30ed4341d20bff6fb85c8c52adb334"
DATA = Path("/Users/velkris/.codex/task-queues/analysis/L11-MASSIVE-1M-AGGS-20261023")
CASES = (
    ("IZM", "2026-10-13", "2026-10-13/IZM/1791901980000/1791902100000",
     "ea37f613da96c556996ec8e4aac296db4e4961ba7bec426419d12afe642ba365"),
    ("CLWT", "2026-10-12", "2026-10-12/CLWT/1791817140000/1791817620000",
     "153025afcfd7d97f7e3d515b83b33b561e9b660a79cfaf90fff995dc923fb8e1"),
    ("DAIC", "2026-10-12", "2026-10-12/DAIC/1791808680000/1791808800000",
     "e5f3631c52d0dfce091780b118d676767d0c97bd42d439efb8c8ac5f8da66bd7"),
    ("LNKS", "2026-10-12", "2026-10-12:LNKS:07:02",
     "d804c3268a5a563851ca6d4d6fce9c5037fb631e251bfd4eacfccc25c449fd82"),
)


def checked(path, expected):
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected, str(path)
    return raw


def decimal_text(value):
    if not isinstance(value, Decimal):
        raise TypeError("unsupported oracle artifact value")
    return str(value)


@pytest.mark.parametrize("symbol,day,case_id,csv_sha", CASES, ids=[row[0] for row in CASES])
def test_retained_dual_producer_projection_matches_independent_raw_chart_line(
        tmp_path, symbol, day, case_id, csv_sha):
    raw_oracle = checked(ORACLE, ORACLE_SHA)
    module = ModuleType("_unchanged_c1_chart_oracle")
    module.__file__ = str(ORACLE)
    sys.modules[module.__name__] = module
    exec(compile(raw_oracle, str(ORACLE), "exec", dont_inherit=True), module.__dict__)
    source = DATA / f"massive_1m_{day}_{symbol}.csv"
    raw_csv = checked(source, csv_sha)
    rows = list(csv.DictReader(io.StringIO(raw_csv.decode("utf-8"))))
    anchor = int(datetime.fromisoformat(day + "T04:00:00").replace(
        tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    stamps = [int(row["start_ms"]) for row in rows]
    assert stamps == sorted(set(stamps)) and all(row["symbol"] == symbol for row in rows)
    assert all(anchor <= stamp < anchor + 57_600_000 for stamp in stamps)
    oracle_bars = [module.Bar(int(row["start_ms"]), *(float(row[key]) for key in (
        "open", "high", "low", "close")), Decimal(row["volume"])) for row in rows]
    formatter = module._row

    def observed(bar, tr, loss, trail, state, flip, age=None):
        # Delegating formatter observer only; never changes the recurrence/state.
        return dict(formatter(bar, tr, loss, trail, state, flip, age), raw_trail=trail)

    with patch.object(module, "_row", observed):
        expected = module.compute_atr_trail(oracle_bars, period=5, factor=3.5, seed="sma5")
    packet = json.loads(checked(PACKET, PACKET_SHA))
    matches = [case for case in packet["cases"] if case["case"]["case_id"] == case_id]
    assert len(matches) == 1
    retained = matches[0]
    config = retained["configuration"]
    assert config["strategy_schwab_1m_v2_atr_flip_period"] == 5
    assert config["strategy_schwab_1m_v2_atr_flip_factor"] == 3.5
    events = []
    provenance = []
    for row in retained["combined"]:
        stamp = row["stamp"]
        massive = row if row["origin"] == "C1_MASSIVE_POSTHOC_REFETCH" else row.get("massive_reference")
        if massive is not None:
            events.append((stamp + 60_000, 0, ChartBar(symbol,
                *(float(massive[key]) for key in ("open", "high", "low", "close")),
                float(massive["volume"]), stamp, producer="massive")))
            provenance.append({"bar_ms": stamp, "producer": "massive", "row_id": massive["row_id"]})
        if row["origin"] == "strategy_bar_history":
            # This is explicitly reconstructed transport provenance, not evidence
            # that the stored table/provider=NULL is original native Schwab HTTP.
            available = row.get("available_at_ms")
            available = stamp + 60_001 if available is None else available
            events.append((available, 1, ChartBar(symbol,
                *(float(row[key]) for key in ("open", "high", "low", "close")),
                float(row["volume"]), stamp, producer="schwab")))
            provenance.append({"bar_ms": stamp, "producer": "schwab_FILE_PROTOCOL",
                "row_id": row["row_id"], "original_available_at_ms": row.get("available_at_ms"),
                "delivery_ms": available})
    pins = SessionProducerPins(symbol, anchor)
    now = [anchor]
    ledger = SessionLineRestoration(symbol, anchor, 1, producer_pins=pins, clock=lambda: now[0])
    for available, rank, bar in sorted(events, key=lambda event: (event[0], event[1], event[2].timestamp_ms)):
        now[0] = available
        canonical = pins.accept(bar, now_ms=available)
        ledger.observe(canonical)
    actual_stamps = sorted(ledger._bars)
    comparisons = []
    previous = None
    expected_by_stamp = {row["ts"]: row for row in expected}
    for index, stamp in enumerate(actual_stamps):
        prefix_ids = tuple(actual_stamps[:index + 1])
        prefix = tuple(ledger._bars[ts] for ts in prefix_ids)
        # Mathematical request only, NOT a manufactured provider/coverage proof.
        # Native/HALT admission and the real owner path remain separate gates.
        pairs = tuple((left, right) for left, right in zip(prefix_ids, prefix_ids[1:])
                      if right - left > 90_000)
        request = RebuildInput(symbol, 1, index + 1, anchor, stamp, prefix, pairs)
        snapshot = build_session_line(request, 5, 3.5, previous=previous)
        previous = RebuildResult(request, snapshot)
        indicator, signal = dict(snapshot.indicator), dict(snapshot.signal)
        reference = expected_by_stamp.get(stamp)
        delta = (None if reference is None or reference["raw_trail"] is None
                 or indicator["atr_trail"] is None else indicator["atr_trail"] - reference["raw_trail"])
        comparisons.append({"bar_ms": stamp, "oracle": reference,
            "actual_raw_trail": indicator["atr_trail"], "delta": delta,
            "actual_state": indicator["atr_state"], "actual_age": indicator["atr_state_age"],
            "actual_flip": signal.get("flip"), "pinned_producer": pins.producers[stamp]})
    report = {"scope": "ACTUAL_INPUT_RECONSTRUCTED_MATH_PROJECTION_NOT_CASE_OR_NATIVE_PASS",
        "case_id": case_id, "oracle_sha256": ORACLE_SHA, "csv_sha256": csv_sha,
        "packet_sha256": PACKET_SHA, "period": 5, "factor": 3.5, "seed": "sma5",
        "tolerance_proposal": "existing test_schwab_1m_v2_atr_flip parity: abs(raw_delta)<1e-9; review required",
        "timestamps_exact": actual_stamps == stamps, "native": "UNKNOWN",
        "producer_provenance": provenance, "rows": comparisons}
    output = tmp_path / f"{symbol}-producer-chart.json"
    with open(output, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
        json.dump(report, handle, separators=(",", ":"), allow_nan=False, default=decimal_text)
    print("CHART_MATH_ARTIFACT", output, hashlib.sha256(output.read_bytes()).hexdigest())
    assert actual_stamps == stamps, "Source minute sets differ; no dropping or inventing bars"
    for row in comparisons:
        reference = row["oracle"]
        if reference["raw_trail"] is None:
            assert row["actual_raw_trail"] is None, row
        else:
            assert row["delta"] is not None and abs(row["delta"]) < 1e-9, row
            assert row["actual_state"] == reference["state"], row
            assert row["actual_age"] == reference["state_age"], row
            assert row["actual_flip"] == reference["flip"], row
