"""F3 fleet function-health — unit-tests the pure verdict logic of the independent
check script (ops/health/fleet_health_check.py). Loaded by path (the script is stdlib-only
and imports NO app code, so it stays independent/unhangable); we test only its decision
functions, not the psql/redis I/O. The load-bearing property proven here is the
NO-FALSE-ALARM discipline: stale bars are RED only when the upstream feed is simultaneously
live (a frozen loop) — never on a quiet market / feed outage."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parents[2] / "ops" / "health" / "fleet_health_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("fleet_health_check", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fhc = _load()


def test_fresh_bars_with_live_feed_is_green():
    level, _ = fhc.classify_bar_freshness(30, 3)
    assert level == "GREEN"


def test_stale_bars_with_LIVE_feed_is_red_frozen_loop():
    level, detail = fhc.classify_bar_freshness(300, 5)
    assert level == "RED"
    assert "FROZEN" in detail


def test_stale_bars_with_QUIET_feed_is_green_no_false_alarm():
    # THE no-false-alarm guarantee: bars stale but the upstream feed is quiet/stale is a
    # quiet market or a feed outage — NOT a strategy fault. Must never RED.
    assert fhc.classify_bar_freshness(600, 400)[0] == "GREEN"   # feed stale
    assert fhc.classify_bar_freshness(600, None)[0] == "GREEN"  # no recent trades at all


def test_slowing_bars_with_live_feed_is_amber():
    assert fhc.classify_bar_freshness(150, 5)[0] == "AMBER"


def test_no_bars_is_amber_not_red():
    # Can't assess (no data) is AMBER (look), never RED (don't cry wolf).
    assert fhc.classify_bar_freshness(None, 5)[0] == "AMBER"


# --- check #2: oms-order-lifecycle (alive-but-not-executing) ------------------ #

def test_no_stuck_intents_is_green_quiet_or_executing():
    # THE no-false-alarm guard: no stuck intents -> GREEN, whether the market is quiet
    # (no intents) or the OMS is executing normally.
    assert fhc.classify_order_lifecycle(0, None)[0] == "GREEN"


def test_stuck_intents_is_red_alive_but_not_executing():
    level, detail = fhc.classify_order_lifecycle(3, 12)
    assert level == "RED"
    assert "not-executing" in detail or "not executing" in detail


def test_unreadable_intents_is_amber_not_red():
    assert fhc.classify_order_lifecycle(None, None)[0] == "AMBER"


# --- check #3: stops-armed (every OMS-owned open position has an armed stop) --- #

def test_owned_position_with_stop_is_green():
    # 2 OMS-owned open, 0 unprotected → all armed → GREEN.
    assert fhc.classify_stops_armed(0, 2)[0] == "GREEN"


def test_owned_position_without_stop_is_red_naked():
    level, detail = fhc.classify_stops_armed(1, 1)
    assert level == "RED"
    assert "NAKED" in detail


def test_flat_is_green_nothing_to_protect():
    # No OMS-owned open positions → nothing to protect → GREEN, never RED.
    assert fhc.classify_stops_armed(0, 0)[0] == "GREEN"


def test_manual_position_is_ignored_green():
    # SCOPING INVARIANT: a manual holding has no virtual_positions row, so the query never
    # counts it → unprotected stays 0 → GREEN. (The virtual_positions-only source is what
    # enforces this; live-validated. Here we assert the verdict for that count state.)
    assert fhc.classify_stops_armed(0, 0)[0] == "GREEN"


def test_unreadable_stops_is_amber_not_red():
    assert fhc.classify_stops_armed(None, None)[0] == "AMBER"
