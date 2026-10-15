"""Offline retained-input model acceptance, never a WS burst/live ACK proof."""

from decimal import Decimal
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
INPUT = Path("/Users/velkris/.codex/task-queues/analysis")
TRADES = INPUT / "momentum-detector-timing-20261012/momentum-wff-rest-trades-20261012.json"
SESSIONS = INPUT / "momentum-stop-study-20261012/momentum-stop-study-events-20261012.json"
pytestmark = pytest.mark.skipif(not (TRADES.exists() and SESSIONS.exists()),
                               reason="retained inputs absent; no substitute fixture")


@pytest.fixture(scope="module")
def replay():
    spec = importlib.util.spec_from_file_location("wff_causal_replay", ROOT / "scripts/momwarn1_wff_causal_replay.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    session = next(row["payload"] for row in json.loads(SESSIONS.read_text())["rows"]
                   if row["event_type"] == "SESSION_READY" and row["session_date"] == "2026-10-12")
    trades = json.loads(TRADES.read_text())
    return lambda **kwargs: module.run(trades, session, **kwargs)


def test_retained_M60_causal_target_and_whole_reference_without_historical_ACK(replay):
    result = replay()
    assert result["transport_latency"] == "UNMEASURED"
    assert result["provider_ACK"] == "NOT_RECORDED_OR_INVENTED"
    assert result["input_rows"] == 9793
    target, = result["target_reached"]
    assert target["strategy_code"] == "momentum_60s_warn"
    assert target["signal_ms"] == 1791809480927 and target["signal_price"] == "13.35"
    assert target["fill_ms"] == 1791809480928 and Decimal(target["fill_price"]) == Decimal("13.40")
    detect = next(row for row in result["records"] if row["logical_id"] == target["logical_id"]
                  and row["kind"] == "DETECTED")
    fill = next(row for row in result["records"] if row["logical_id"] == target["logical_id"]
                and row["kind"] == "FILLED")
    assert detect["callback_sip_ns"] == 1791809480927834426
    assert fill["callback_sip_ns"] == 1791809480928360516 > detect["observed_ns"]
    assert detect["callback_trade_id"] == detect["payload"]["detect"]["trade_id"] == "13273"
    assert fill["callback_trade_id"] == fill["payload"]["fill"]["trade_id"] == "5205"
    ref = next(row for row in result["target_reference_windows"] if row["seconds"] == 60)
    assert ref["input_covers_time_bounds"]
    assert ref["reference"] == dict(low="11.12", start_ms=1791809422000, end_ms=1791809423000)
    assert ref["window_start_ms"] <= ref["reference"]["start_ms"] < ref["reference"]["end_ms"] <= ref["window_end_ms"]
    request, applied = result["owner_events"]
    assert request["observed_ns"] == applied["observed_ns"] == 1791809456000000000
    assert applied["observed_ns"] < detect["observed_ns"]
    m30 = next(row for row in result["records"] if row["strategy_code"] == "momentum_30s_warn"
               and row["kind"] == "DETECTED")
    assert m30["payload"]["detect"]["sip_ts_ms"] == 1791809484080
    assert m30["payload"]["detect"]["price"] == "14.44"  # Do not relabel this as target parity.


@pytest.mark.parametrize("delays", [{"owner_delay_ns": 26_000_000_000}, {"trade_delay_ns": 1_000_000}])
def test_controlled_owner_or_decision_delay_cannot_backdate_target_fill(replay, delays):
    result = replay(**delays)
    assert not result["target_reached"]
    assert result["transport_latency"] == "UNMEASURED"


def test_negative_delay_cannot_make_future_buckets_available_early(replay):
    with pytest.raises(ValueError, match="nonnegative CONTROLLED"):
        replay(aggregate_delay_ns=-1)
