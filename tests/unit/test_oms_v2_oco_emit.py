"""The v2 -> Schwab OCO emit: attaching bracket metadata to the entry (flag-gated, default off).

Pure metadata transform (`_apply_v2_oco_bracket_entry`), so tested directly on a __new__ service
with the two settings the helper reads. Off / non-v2 must be byte-identical (no mutation).
"""

from __future__ import annotations

import logging
from decimal import Decimal

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings


def _svc(enabled: bool, *, target=2.0, stop=5.0) -> OmsRiskService:
    s = OmsRiskService.__new__(OmsRiskService)
    s.settings = Settings(oms_v2_emit_native_oco_bracket_enabled=enabled)
    s.logger = logging.getLogger("test-v2-oco-emit")
    s._cw_target_pct = target
    s._cw_stop_pct = stop
    return s


def _event(**md) -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="test",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2", broker_account_name="live:schwab_1m_v2",
            symbol="KIDZ", side="buy", quantity=Decimal("2"), intent_type="open",
            reason="ENTRY_CW", metadata={"order_type": "market", **md},
        )
    )


def test_emit_attaches_bracket_metadata_with_cw_exit_geometry() -> None:
    ev = _event(entry_price="10.00")
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    md = ev.payload.metadata
    assert md["bracket"] == "true"
    assert md["native_oco_bracket"] == "true"
    assert md["bracket_entry_type"] == "MARKET"       # a market entry -> MARKET parent
    assert md["bracket_target_price"] == "10.2000"    # +2%
    assert md["bracket_stop_price"] == "9.5000"       # -5%


def test_emit_mirrors_a_limit_entry_to_a_limit_parent() -> None:
    ev = _event(entry_price="10.00", order_type="limit")
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert ev.payload.metadata["bracket_entry_type"] == "LIMIT"


def test_emit_uses_the_configured_cw_percentages() -> None:
    ev = _event(entry_price="4.00")
    _svc(True, target=3.0, stop=4.0)._apply_v2_oco_bracket_entry(event=ev)
    md = ev.payload.metadata
    assert md["bracket_target_price"] == "4.1200"     # +3%
    assert md["bracket_stop_price"] == "3.8400"       # -4%


def test_flag_off_does_not_mutate_the_event() -> None:
    ev = _event(entry_price="10.00")
    before = dict(ev.payload.metadata)
    _svc(False)._apply_v2_oco_bracket_entry(event=ev)
    assert ev.payload.metadata == before             # byte-identical


def test_non_v2_strategy_is_untouched() -> None:
    ev = _event(entry_price="10.00")
    ev.payload.strategy_code = "orb"
    before = dict(ev.payload.metadata)
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert ev.payload.metadata == before


def test_sell_or_close_is_untouched() -> None:
    ev = _event(entry_price="10.00")
    ev.payload.side = "sell"
    before = dict(ev.payload.metadata)
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert ev.payload.metadata == before


def test_no_entry_reference_falls_back_to_plain_entry() -> None:
    """No usable entry price -> do NOT emit a half-specified bracket (fall back to single-leg)."""
    ev = _event()   # no entry_price / reference_price
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert "bracket" not in ev.payload.metadata


def test_reference_price_is_accepted_as_the_entry_ref() -> None:
    ev = _event(reference_price="8.00")
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert ev.payload.metadata["bracket_target_price"] == "8.1600"
