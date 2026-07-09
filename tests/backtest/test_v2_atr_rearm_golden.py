"""(c) REAL-DATA golden for the ATR re-arm fix — truth, not coder-intent (a synthetic-only suite is how
parity-to-a-bug happened). Fixture = NVVE 2026-07-08 real Polygon 1-min bars (09:00-16:30 ET). The BUY
flips computed by OUR compute_atr_trail(5,3.5) are HAND-VERIFIED against the operator's TOS
ATRTrailingStop(5,3.5,WILDERS) chart: 12:26 / 13:29 / 16:18. The re-arm golden pins that (1) our ATR
matches the chart on real data, and (2) the 13:29 flip is preceded by a real graze — the exact at-risk
pattern the shipped guard mis-fires on.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail

_ET = ZoneInfo("America/New_York")
_FIX = Path(__file__).parent / "fixtures" / "nvve_20260708_bars.json"


def _et(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(_ET).strftime("%H:%M")


def _bars():
    return [Bar(*r) for r in json.load(open(_FIX))]


def test_golden_nvve_flips_match_the_TOS_chart():
    """Chart-verified: our ATR BUY flips == the operator's TOS ATRTrailingStop flips (12:26/13:29/16:18).
    Guards against parity certifying a mis-timed flip — the failure that hid this bug."""
    bars = _bars()
    tr = compute_atr_trail(bars, period=5, factor=3.5)
    buys = [_et(bars[i].ts) for i, r in enumerate(tr) if r["flip"] == "BUY"]
    assert buys == ["12:26", "13:29", "16:18"], f"ATR BUY flips must match the chart; got {buys}"


def test_golden_1329_flip_is_preceded_by_a_real_graze():
    """The 13:29 real flip has an EARLIER graze in its short segment (the operator's ~13:19 poke): the
    shipped one-touch-per-segment guard fires on that graze and misses 13:29; the fix must re-arm."""
    bars = _bars()
    tr = compute_atr_trail(bars, period=5, factor=3.5)
    flip = next(i for i, r in enumerate(tr) if r["flip"] == "BUY" and _et(bars[i].ts) == "13:29")
    seg_start = max(i for i in range(flip) if tr[i]["flip"] == "SELL")   # the 12:50 SELL
    grazes = [i for i in range(seg_start + 1, flip)
              if tr[i - 1]["state"] == "short" and tr[i - 1]["trail"] is not None
              and bars[i].high >= tr[i - 1]["trail"]]
    assert grazes, "expected a real graze before the 13:29 flip (the at-risk pattern)"
    assert _et(bars[grazes[0]].ts) < "13:29", "the graze fires before the real flip (variant-B first)"
