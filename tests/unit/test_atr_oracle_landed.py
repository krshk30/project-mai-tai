"""The canonical ATR-flip oracle (analysis/atr_flip.py::compute_atr_trail) is the
reference implementation that the LIVE v2 strategy's _update_atr_state is pinned
to (see test_schwab_1m_v2_atr_flip.py, which froze a verbatim copy because this
module previously lived only on a held branch). This test confirms the oracle is
now importable + functional ON MAIN, so the frozen copy has an in-repo
source-of-truth. The live-v2 == oracle parity itself is asserted by
test_atr_indicator_parity_vs_oracle (still passing, unchanged).
"""
from __future__ import annotations

from analysis.atr_flip import Bar, compute_atr_trail


def _bars(closes: list[float]) -> list[Bar]:
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        bars.append(Bar(ts=1_700_000_000_000 + i * 60_000, open=prev,
                        high=max(prev, c) + 0.5, low=min(prev, c) - 0.5,
                        close=c, volume=1000))
        prev = c
    return bars


def test_oracle_importable_and_returns_rows():
    rows = compute_atr_trail(_bars([100.0 + i * 0.1 for i in range(12)]))
    assert len(rows) == 12
    warmed = [r for r in rows if r.get("trail") is not None]
    assert warmed, "oracle never defined a trail"
    assert all(r["state"] in ("long", "short") for r in warmed)


def test_oracle_flips_short_then_long_on_v_shape():
    # warmup-up (stays long), then a clear fall (SELL flip -> short), then a clear
    # rise (BUY flip -> long). BUY = short->long is the live-v2 entry signal.
    closes = ([100.0 + i * 0.05 for i in range(10)]
             + [99.0 - i * 1.0 for i in range(10)]
             + [90.0 + i * 1.0 for i in range(12)])
    rows = compute_atr_trail(_bars(closes))
    flips = [r["flip"] for r in rows if r.get("flip")]
    assert "SELL" in flips and "BUY" in flips, f"expected a SELL then BUY, got {flips}"
    assert flips.index("SELL") < flips.index("BUY")
    assert rows[-1]["state"] == "long"
