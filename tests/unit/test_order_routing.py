"""Characterization test for the extracted extended-hours order-routing leaf.

Restores the v2 entry extended-hours handoff by moving `order_routing_metadata`
+ deps into `strategy_core.order_routing`. These pin the EXACT metadata the
legacy macd_30s / schwab_1m entry path has always emitted, proving:
  * the extraction is byte-identical (the legacy module re-exports the SAME
    objects — one source of truth), and
  * the exit-side stop-guard routing is unchanged (no exit-path regression).

June 2026 is EDT (UTC-4): 07:00 ET = 11:00Z, 10:00 ET = 14:00Z, 17:00 ET = 21:00Z.
"""
from __future__ import annotations

from datetime import UTC, datetime

from project_mai_tai.services import strategy_engine_app as legacy
from project_mai_tai.strategy_core.order_routing import (
    _format_limit_price,
    extended_hours_session,
    order_routing_metadata,
)

PRE = datetime(2026, 6, 23, 11, 0, tzinfo=UTC)         # 07:00 ET premarket
RTH = datetime(2026, 6, 23, 14, 0, tzinfo=UTC)         # 10:00 ET regular
POST = datetime(2026, 6, 23, 21, 0, tzinfo=UTC)        # 17:00 ET post
OPEN_EDGE = datetime(2026, 6, 23, 13, 30, tzinfo=UTC)  # exactly 09:30 ET
CLOSE_EDGE = datetime(2026, 6, 23, 20, 0, tzinfo=UTC)  # exactly 16:00 ET


def test_single_source_of_truth():
    # Legacy module re-exports the SAME objects — extraction added no second copy.
    assert legacy.order_routing_metadata is order_routing_metadata
    assert legacy.extended_hours_session is extended_hours_session
    assert legacy._format_limit_price is _format_limit_price


def test_session_classification():
    assert extended_hours_session(RTH) is None
    assert extended_hours_session(OPEN_EDGE) is None    # 09:30 is regular session
    assert extended_hours_session(PRE) == "AM"
    assert extended_hours_session(POST) == "PM"
    assert extended_hours_session(CLOSE_EDGE) == "PM"   # 16:00 is no longer regular


def test_rth_is_empty_byte_identical():
    # Gate (a): RTH -> {} so the order stays market/NORMAL, unchanged from today.
    assert order_routing_metadata(price="2.95", side="buy", now=RTH) == {}
    assert order_routing_metadata(price="2.95", side="sell", now=RTH) == {}


def test_premarket_buy_exact_metadata():
    # Gate (b): the exact dict the proven 06-08 working entries carried.
    assert order_routing_metadata(price="2.95", side="buy", now=PRE) == {
        "session": "AM",
        "order_type": "limit",
        "time_in_force": "day",
        "extended_hours": "true",
        "limit_price": "2.95",
        "reference_price": "2.95",
        "price_source": "ask",
    }


def test_postmarket_sell_exact_metadata():
    assert order_routing_metadata(price="3.10", side="sell", now=POST) == {
        "session": "PM",
        "order_type": "limit",
        "time_in_force": "day",
        "extended_hours": "true",
        "limit_price": "3.10",
        "reference_price": "3.10",
        "price_source": "bid",
    }


def test_format_limit_price():
    assert _format_limit_price(2.9499) == "2.95"
    assert _format_limit_price("3.1") == "3.10"
    assert _format_limit_price(None) is None
    assert _format_limit_price("abc") is None


def test_stop_guard_routing_unchanged():
    # Gate (c): exit-side stop-guard routing is untouched by the extraction.
    # In-session -> limit WITHOUT a session key (native stop arms RTH);
    # extended hours -> same limit dict plus session=AM/PM.
    rth = legacy.stop_guard_order_routing_metadata(price="2.50", price_source="bid", now=RTH)
    assert rth == {
        "order_type": "limit",
        "time_in_force": "day",
        "limit_price": "2.50",
        "reference_price": "2.50",
        "price_source": "bid",
    }
    ext = legacy.stop_guard_order_routing_metadata(price="2.50", price_source="bid", now=PRE)
    assert ext["session"] == "AM"
    assert ext["extended_hours"] == "true"
    assert ext["order_type"] == "limit"
