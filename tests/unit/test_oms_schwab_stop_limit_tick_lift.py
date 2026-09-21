"""Schwab resting STOP_LIMIT: cent-rounding must not fold the limit onto the stop.

v2 sends limit = stop * 1.005. Below $2.00 that band is under one cent, and the OMS rounds stop and
limit independently, so 47 of 145 resting entries on $1-$2 names went to Schwab with limit == stop
between 2026-09-01 and 2026-09-18 (0 of 406 elsewhere) - see
docs/review-artifacts/schwab-limit-eq-stop/ANALYSIS.md. Every price pair below marked REAL is an
intent that produced a collapsed Schwab order in that window.

Behavioural: each test runs `_apply_v2_oco_bracket_entry` and reads the metadata the adapter gets.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

ADJUSTED = "[SCHWAB-BUY-STOP-LIMIT-TICK-ADJUSTED]"
FLAG = "schwab_buy_stop_limit_tick_adjusted"


@pytest.fixture(autouse=True)
def _force_regular_hours(monkeypatch):
    import project_mai_tai.oms.service as svc

    monkeypatch.setattr(svc, "_is_regular_market_session", lambda now=None: True)


def _svc() -> OmsRiskService:
    s = OmsRiskService.__new__(OmsRiskService)
    s.settings = Settings(oms_v2_emit_native_oco_bracket_enabled=True)
    s.logger = logging.getLogger("test-schwab-tick-lift")
    s._cw_target_pct = 5.0
    s._cw_stop_pct = 8.0
    return s


def _resting_entry(stop: str, limit: str, **md) -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="test",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name="live:schwab_1m_v2",
            symbol="GIPR",
            side="buy",
            quantity=Decimal("2"),
            intent_type="open",
            reason="schwab_1m_v2 ATR Flip CW-v2-resting",
            metadata={
                "order_type": "STOP_LIMIT",
                "entry_price": stop,
                "reference_price": stop,
                "stop_price": stop,
                "limit_price": limit,
                **md,
            },
        ),
    )


def _wire(stop: str, limit: str, caplog=None, **md) -> tuple[dict, str]:
    ev = _resting_entry(stop, limit, **md)
    if caplog is not None:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="test-schwab-tick-lift"):
            _svc()._apply_v2_oco_bracket_entry(event=ev)
        return ev.payload.metadata, caplog.text
    _svc()._apply_v2_oco_bracket_entry(event=ev)
    return ev.payload.metadata, ""


@pytest.mark.parametrize(
    ("raw_stop", "raw_limit", "wire_stop", "wire_limit"),
    [
        ("1.2166", "1.2227", "1.22", "1.23"),  # REAL - GIPR 2026-09-18 (Webull got 1.22 / 1.23)
        ("1.6355", "1.6437", "1.64", "1.65"),  # REAL - VIOT 2026-09-02
        ("1.0780", "1.0834", "1.08", "1.09"),  # REAL - NCPL 2026-09-02
        ("1.7362", "1.7449", "1.74", "1.75"),  # REAL - KXIN 2026-09-17
    ],
)
def test_a_band_that_rounding_removed_comes_back_as_one_tick(
    raw_stop: str, raw_limit: str, wire_stop: str, wire_limit: str, caplog
) -> None:
    md, log = _wire(raw_stop, raw_limit, caplog)

    assert (md["stop_price"], md["limit_price"]) == (wire_stop, wire_limit)
    assert md[FLAG] == "true"
    assert ADJUSTED in log
    assert f"raw_stop={raw_stop} raw_limit={raw_limit}" in log
    assert f"wire_stop={wire_stop} wire_limit={wire_limit}" in log
    # the trigger is NOT moved, and the exits still price off the line
    assert md["bracket_entry_type"] == "STOP_LIMIT"
    assert Decimal(md["bracket_stop_price"]) < Decimal(md["stop_price"])


@pytest.mark.parametrize(
    ("raw_stop", "raw_limit", "wire_stop", "wire_limit"),
    [
        ("1.1904", "1.1964", "1.19", "1.20"),  # REAL - GIPR 09-18 12:44, already one cent apart
        ("2.4500", "2.4623", "2.45", "2.46"),  # above $2.00 the band is always >= one cent
        ("10.0010", "10.0510", "10.00", "10.05"),
        ("0.8512", "0.8555", "0.8512", "0.8555"),  # at/below $1.00: four decimals, nothing folds
    ],
)
def test_control_a_band_that_survived_rounding_is_left_exactly_alone(
    raw_stop: str, raw_limit: str, wire_stop: str, wire_limit: str, caplog
) -> None:
    md, log = _wire(raw_stop, raw_limit, caplog)

    assert (md["stop_price"], md["limit_price"]) == (wire_stop, wire_limit)
    assert FLAG not in md
    assert ADJUSTED not in log


def test_a_raw_limit_at_or_below_the_raw_stop_is_not_papered_over(caplog) -> None:
    # An intent whose own limit is not above its stop is an upstream defect. The lift exists for
    # "raw valid, wire collapsed" only - it must not turn a broken intent into a plausible order.
    md, log = _wire("1.2200", "1.2200", caplog)
    assert (md["stop_price"], md["limit_price"]) == ("1.22", "1.22")
    assert FLAG not in md and ADJUSTED not in log

    md, log = _wire("1.2300", "1.2200", caplog)
    assert (md["stop_price"], md["limit_price"]) == ("1.23", "1.22")
    assert FLAG not in md and ADJUSTED not in log


def test_one_tick_up_from_exactly_one_dollar_is_a_cent_not_a_four_decimal_price() -> None:
    # Schwab firm-rejects more than two decimals above $1.00, so 1.0000 + one tick must be 1.01.
    md, _ = _wire("1.00001", "1.00004")
    assert (md["stop_price"], md["limit_price"]) == ("1.00", "1.01")
    assert md[FLAG] == "true"


def test_below_one_dollar_the_tick_is_a_hundredth_of_a_cent() -> None:
    md, _ = _wire("0.99991", "0.99994")
    assert (md["stop_price"], md["limit_price"]) == ("0.9999", "1.0000")
    assert md[FLAG] == "true"


def test_the_webull_stop_limit_leg_is_untouched_by_the_schwab_lift() -> None:
    # The Webull mirror is left BARE by this function and tick-adjusted by its own adapter.
    md, _ = _wire("1.2166", "1.2227", fanout_leg="webull")
    assert (md["stop_price"], md["limit_price"]) == ("1.2166", "1.2227")
    assert FLAG not in md
