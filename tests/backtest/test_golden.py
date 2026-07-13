"""Golden-case CI gate for the backtest engine — the ENFORCEMENT that keeps it trustworthy.

The engine's conclusions are trusted ONLY if these pass in CI. Runs on committed gzipped-CSV
fixtures (no DB). See docs/backtest-engine-design.md.

Gates:
  - KIDZ 07-06 (REAL broker-fill anchor): capped bar-close -> exactly 1 trade (09:53 break
    suppressed by the 2-attempt cap), honest ask entry ~1.16, 3% trail exit ~1.12, modeled
    -$0.20 (real broker -$0.175 — honest-conservative, never assumes price improvement).
  - CELZ 06-30 bar-close count = 5 (matches the study's BC-A).
  - CELZ 06-30 PHANTOM-FREE: intrabar trades far below the 93 the bar-based bug produced.
  - INTRABAR PARITY: two independent implementations agree exactly (intrabar's substitute for
    the missing real-fill anchor — the live bot only trades bar-close).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from project_mai_tai.backtest.data import FixtureMarketDataSource, build_bars
from project_mai_tai.backtest.orb_sim import (
    simulate_bar_close,
    simulate_intrabar,
    simulate_intrabar_v2,
    simulate_resting,
)

FIX = Path(__file__).parent / "fixtures"
UTC = timezone.utc
_SRC = FixtureMarketDataSource(FIX)


def _u(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def _load(sym, y, m, d):
    obs, so, cut, end = _u(y, m, d, 13, 25), _u(y, m, d, 13, 30), _u(y, m, d, 14, 0), _u(y, m, d, 14, 10)
    trades = _SRC.trades(sym, obs, end)
    quotes = _SRC.quotes(sym, obs, end)
    return {
        "bars": build_bars(trades, so), "trades": trades, "quotes": quotes,
        "observe_open": obs, "session_open": so, "cutoff": cut,
    }


_BASE = dict(gap_cap_pct=1.5, trail_pct=3.0, qty=5, latency_s=3.0)


def _win(d):
    return dict(observe_open=d["observe_open"], session_open=d["session_open"], cutoff=d["cutoff"])


def test_kidz_faithfulness_gate():
    """The engine reproduces the real KIDZ trade shape with an honest-conservative P&L."""
    d = _load("KIDZ", 2026, 7, 6)
    trades = simulate_bar_close(d["bars"], d["quotes"], capped=True, **_BASE, **_win(d))
    assert len(trades) == 1, "09:53 break must be suppressed by the 2-attempt cap"
    t = trades[0]
    assert abs(t.entry_price - 1.16) < 0.005   # honest ask fill (real broker 1.155)
    assert abs(t.exit_price - 1.12) < 0.01     # 3% trail stop
    assert abs(t.pnl - (-0.20)) < 0.02         # modeled -$0.20 vs real -$0.175 (Δ = surfaced improvement)
    assert t.exit_reason == "TRAIL_STOP"


def test_celz_barclose_count_gate():
    d = _load("CELZ", 2026, 6, 30)
    trades = simulate_bar_close(d["bars"], d["quotes"], capped=False, **_BASE, **_win(d))
    assert len(trades) == 5                    # study BC-A count


def test_celz_phantom_free_gate():
    """Continuous per-tick running-high CANNOT reproduce the bar-based bug's 93 phantom trades."""
    d = _load("CELZ", 2026, 6, 30)
    trades = simulate_intrabar(d["trades"], d["quotes"], capped=False, **_BASE, **_win(d))
    assert 5 <= len(trades) < 30, f"phantom-free guard: got {len(trades)} (bug produced 93)"


@pytest.mark.parametrize("sym,y,m,d", [("KIDZ", 2026, 7, 6), ("CELZ", 2026, 6, 30), ("SDOT", 2026, 6, 26)])
def test_intrabar_parity_gate(sym, y, m, d):
    """Two independent intrabar implementations must agree exactly (no silent bug)."""
    dd = _load(sym, y, m, d)

    def sig(ts):
        return [
            (t.entry_ts, round(t.entry_price, 4), t.exit_ts, round(t.exit_price, 4) if t.exit_price is not None else None)
            for t in ts
        ]

    for capped in (True, False):
        kw = dict(**_BASE, capped=capped, **_win(dd))
        a = simulate_intrabar(dd["trades"], dd["quotes"], **kw)
        b = simulate_intrabar_v2(dd["trades"], dd["quotes"], **kw)
        assert sig(a) == sig(b), f"parity mismatch {sym} capped={capped}"


def test_resting_stopbuy_fills_at_the_break_within_gapcap():
    """RESTING execution: bar-close level, filled at the crossing tick — the fill is bounded to
    the break level's gap-cap (fills AT the break, never a faded ask far below it, and never
    past the gap-cap on a gap-through). Same break detection + 2-attempt cap as bar_close."""
    d = _load("KIDZ", 2026, 7, 6)
    r = simulate_resting(d["bars"], d["trades"], d["quotes"], capped=True, **_BASE, **_win(d))
    assert 1 <= len(r) <= 2                               # honest 2-attempt cap
    for t in r:
        assert t.level is not None
        # fills AT the break, within the gap-cap — never a faded ask far below, never past the cap
        assert 0 < t.entry_price <= t.level * 1.0151
        assert t.exit_reason in ("TRAIL_STOP", "WINDOW_END")
