"""v2/ATR SHARED-ORACLE CI gate — pins the vendored `backtest.atr_oracle` against the original
`analysis/atr_flip` so the ATR trail the v2 REPLAY (and the R&D scripts) key off cannot silently
drift from the charted signal.

P4 note: the old v2 backtest re-implementation (`backtest/v2_sim.py` — `simulate_v2` +
`detect_atr_touches*`) was DELETED (docs/backtest-replay-engine-design.md P4: one replay, one
truth). The trade-shape / touch-parity gates that covered that dead code went with it; the v2
entry+exit is now covered by the REPLAY suite (tests/unit/test_backtest_replay.py). What remains
here is the SHARED-oracle parity, which the replay and every ATR script still depend on.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from project_mai_tai.backtest.atr_oracle import compute_atr_trail
from project_mai_tai.backtest.data import FixtureMarketDataSource

FIX = Path(__file__).parent / "fixtures"
UTC = timezone.utc
_SRC = FixtureMarketDataSource(FIX)


def _load_kidz_bars():
    obs, end = datetime(2026, 7, 6, 8, 0, tzinfo=UTC), datetime(2026, 7, 7, 0, 0, tzinfo=UTC)
    return _SRC.schwab_bars("KIDZ", obs, end)


def test_v2_vendored_oracle_pinned():
    """The vendored compute_atr_trail must match the original analysis/atr_flip (no silent drift)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    orig = pytest.importorskip("analysis.atr_flip")
    sb = _load_kidz_bars()
    assert compute_atr_trail(sb) == orig.compute_atr_trail(sb)
