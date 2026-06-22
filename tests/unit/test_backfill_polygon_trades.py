from __future__ import annotations

import importlib.util
import types
from decimal import Decimal
from pathlib import Path

# load the script module (scripts/ is not a package)
_spec = importlib.util.spec_from_file_location(
    "backfill_polygon_trades",
    Path(__file__).resolve().parents[2] / "scripts" / "backfill_polygon_trades.py",
)
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


def _trade(**kw):
    base = dict(price=2.01, size=200, exchange=11, conditions=[12, 37],
               sip_timestamp=1781827188401555372, participant_timestamp=1781827188401207053)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_normalize_ts_ns_handles_ns_and_ms():
    assert bf._normalize_ts_ns(1781827188401555372) == 1781827188401555372  # ns unchanged
    assert bf._normalize_ts_ns(1782135713372) == 1782135713372 * 1_000_000  # ms -> ns
    for junk in (None, 0, -1, "", "x"):
        assert bf._normalize_ts_ns(junk) is None


def test_trade_to_row_maps_fields_and_2026_ts():
    row = bf._trade_to_row(_trade(), "ehgo", "massive")
    provider, symbol, event_ts, price, size, exch, conds, cumvol = row
    assert provider == "massive"
    assert symbol == "ehgo"  # caller passes already-cased symbol
    assert event_ts.year == 2026  # never 1970
    assert price == Decimal("2.01")
    assert size == 200
    assert exch == "11"  # int -> text
    assert conds == "12,37"
    assert cumvol is None  # /v3/trades carries no per-trade cumulative volume


def test_trade_to_row_falls_back_to_participant_ts():
    row = bf._trade_to_row(_trade(sip_timestamp=None), "EHGO", "massive")
    assert row is not None and row[2].year == 2026


def test_trade_to_row_drops_bad_timestamp_or_price():
    assert bf._trade_to_row(_trade(sip_timestamp=None, participant_timestamp=None), "X", "massive") is None
    assert bf._trade_to_row(_trade(price="notnum"), "X", "massive") is None


def test_day_bounds_ns_intraday_window():
    import datetime as dt
    bounds = bf._day_bounds_ns(dt.date(2026, 6, 18), dt.time(4, 0), dt.time(16, 0))
    assert bounds is not None
    gte, lte = bounds
    # 04:00 ET = 08:00 UTC on 2026-06-18 (EDT, UTC-4); sanity: lte > gte, both ns-magnitude
    assert lte > gte and gte > 1_000_000_000_000_000_000
    assert bf._day_bounds_ns(dt.date(2026, 6, 18), None, None) is None  # full-day mode
