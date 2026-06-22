from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "gather_polygon_universe",
    Path(__file__).resolve().parents[2] / "scripts" / "gather_polygon_universe.py",
)
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)


def test_normalize_ts_ns_ladder():
    assert g._normalize_ts_ns(1781827188401555372) == 1781827188401555372  # ns
    assert g._normalize_ts_ns(1782135713372) == 1782135713372 * 1_000_000  # ms -> ns
    assert g._normalize_ts_ns(1782135713372000) == 1782135713372000 * 1_000  # us -> ns
    for junk in (None, 0, -3, "", "x"):
        assert g._normalize_ts_ns(junk) is None


def test_ts_yields_2026_not_1970():
    # both ns (historical) and ms (live/agg) must resolve to 2026
    assert g._ts(1781827188401555372).year == 2026   # ns
    assert g._ts(1782135713372).year == 2026          # ms (list_aggs timestamp)
    assert g._ts(None) is None


def test_conditions_join():
    assert g._conditions([12, 37]) == "12,37"
    assert g._conditions([]) is None
    assert g._conditions(None) is None
    assert g._conditions("12") == "12"


def test_bounds_and_window_intraday_vs_fullday():
    day = dt.date(2026, 6, 18)
    # full-day mode
    assert g._bounds(day, None, None) is None
    lo, hi = g._window_utc(day, None)
    assert (hi - lo) == dt.timedelta(days=1)
    # intraday ET window -> ns bounds; 04:00 ET = 08:00 UTC (EDT)
    b = g._bounds(day, dt.time(4, 0), dt.time(16, 0))
    assert b is not None and b[1] > b[0] and b[0] > 1_000_000_000_000_000_000
    wlo, whi = g._window_utc(day, b)
    assert wlo.hour == 8 and whi.hour == 20  # 04:00 ET / 16:00 ET in UTC


def test_dec_and_int_coercion():
    from decimal import Decimal
    assert g._dec("2.01") == Decimal("2.01")
    assert g._dec(None) is None and g._dec("x") is None
    assert g._int("500") == 500 and g._int(None) is None and g._int("x") is None
