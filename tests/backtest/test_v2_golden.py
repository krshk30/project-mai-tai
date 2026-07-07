"""v2/ATR golden-case CI gate — the engine refuses ATR conclusions unless these pass.

Runs on committed Schwab (bars + LEVELONE quotes) + massive fixtures, no DB. v2 is feed-limited
(sparse Schwab LEVELONE, coverage gaps) — trustworthy for SHAPE + directional P&L, so the anchor
asserts the trade SHAPE + honest-conservative P&L band, not a penny (see docs/backtest design).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from project_mai_tai.backtest.atr_oracle import compute_atr_trail
from project_mai_tai.backtest.data import FixtureMarketDataSource
from project_mai_tai.backtest.v2_sim import (
    detect_atr_touches,
    detect_atr_touches_independent,
    simulate_v2,
)

FIX = Path(__file__).parent / "fixtures"
UTC = timezone.utc
_SRC = FixtureMarketDataSource(FIX)


def _load_kidz():
    obs, end = datetime(2026, 7, 6, 8, 0, tzinfo=UTC), datetime(2026, 7, 7, 0, 0, tzinfo=UTC)
    return (_SRC.schwab_bars("KIDZ", obs, end), _SRC.schwab_quotes("KIDZ", obs, end),
            _SRC.quotes("KIDZ", obs, end))


def test_v2_kidz_anchor_gate():
    """Reproduce the real v2 KIDZ trade SHAPE with honest-conservative P&L (real broker -$0.167)."""
    sb, sq, mq = _load_kidz()
    trades = simulate_v2(sb, sq, mq, qty=10, mode="intrabar")
    assert trades, "the 09:17 ATR touch must enter"
    t = trades[0]
    assert 1.17 <= t.entry_price <= 1.22            # honest Schwab ask (~1.20; real 1.1887, sparse-feed)
    assert t.exit_reason == "HARD_STOP"             # immediate hard stop shape
    assert -0.30 <= t.pnl <= -0.10                  # modeled ~-$0.20 honest-conservative (real -$0.167)


def test_v2_touch_parity_gate():
    """Two independent touch-detectors (oracle-derived vs single-pass) must agree on the SIGNAL:
    same bar, same timestamp, same touch level to 4dp. (Sub-4dp differs only because the
    oracle-derived one reads _row's 4dp-rounded trail vs the single-pass raw float — 0.03bps on a
    $1 stock, and touch_price is not the fill; negligible.)"""
    sb, _, _ = _load_kidz()

    def norm(touches):
        return [(i, ts, round(p, 4)) for i, ts, p in touches]

    assert norm(detect_atr_touches(sb)) == norm(detect_atr_touches_independent(sb))


def test_v2_vendored_oracle_pinned():
    """The vendored compute_atr_trail must match the original analysis/atr_flip (no silent drift)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    orig = pytest.importorskip("analysis.atr_flip")
    sb, _, _ = _load_kidz()
    assert compute_atr_trail(sb) == orig.compute_atr_trail(sb)
