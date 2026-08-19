"""The v2 -> Schwab OCO emit: attaching bracket metadata to the entry (flag-gated, default off).

Pure metadata transform (`_apply_v2_oco_bracket_entry`), so tested directly on a __new__ service
with the two settings the helper reads. Off / non-v2 must be byte-identical (no mutation).
"""

from __future__ import annotations

import logging
from decimal import Decimal
import pytest

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings


@pytest.fixture(autouse=True)
def _force_regular_hours(monkeypatch):
    """The emit is RTH-only. Force regular hours so the bracket-emitting tests are independent of
    the wall clock (CI runs at all hours). The RTH-gate tests override this to False."""
    import project_mai_tai.oms.service as svc

    monkeypatch.setattr(svc, "_is_regular_market_session", lambda now=None: True)


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
            strategy_code="schwab_1m_v2",
            broker_account_name="live:schwab_1m_v2",
            symbol="KIDZ",
            side="buy",
            quantity=Decimal("2"),
            intent_type="open",
            reason="ENTRY_CW",
            metadata={"order_type": "market", **md},
        ),
    )


def test_emit_attaches_bracket_metadata_with_cw_exit_geometry() -> None:
    ev = _event(entry_price="10.00")
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    md = ev.payload.metadata
    assert md["bracket"] == "true"
    assert md["native_oco_bracket"] == "true"
    assert md["bracket_entry_type"] == "MARKET"  # a market entry -> MARKET parent
    assert md["bracket_target_price"] == "10.20"  # +2%, >$1 -> 2dp
    assert md["bracket_stop_price"] == "9.50"  # -5%, >$1 -> 2dp


def test_emit_stop_limit_master_for_the_resting_flip_entry() -> None:
    """The resting flip-entry emits order_type=STOP_LIMIT with stop_price (the ATR line = trigger)
    and limit_price (line*(1+band) = cap). The emit passes both through, tick-rounded, as a
    STOP_LIMIT-master OTOCO; target/stop still price off the line (entry_price)."""
    ev = _event(
        order_type="STOP_LIMIT",
        entry_price="10.00",
        reference_price="10.00",
        stop_price="10.001",
        limit_price="10.05",
    )
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    md = ev.payload.metadata
    assert md["bracket_entry_type"] == "STOP_LIMIT"
    assert md["stop_price"] == "10.00"  # trigger, rounded to the >$1 tick
    assert md["limit_price"] == "10.05"  # cap, rounded
    assert md["bracket_target_price"] == "10.20"  # +2% off the line
    assert md["bracket_stop_price"] == "9.50"  # -5% off the line


def test_emit_mirrors_a_limit_entry_to_a_limit_parent() -> None:
    ev = _event(entry_price="10.00", order_type="limit")
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert ev.payload.metadata["bracket_entry_type"] == "LIMIT"


def test_emit_uses_the_configured_cw_percentages() -> None:
    ev = _event(entry_price="4.00")
    _svc(True, target=3.0, stop=4.0)._apply_v2_oco_bracket_entry(event=ev)
    md = ev.payload.metadata
    assert md["bracket_target_price"] == "4.12"  # +3%, >$1 -> 2dp
    assert md["bracket_stop_price"] == "3.84"  # -4%, >$1 -> 2dp


def test_flag_off_does_not_mutate_the_event() -> None:
    ev = _event(entry_price="10.00")
    before = dict(ev.payload.metadata)
    _svc(False)._apply_v2_oco_bracket_entry(event=ev)
    assert ev.payload.metadata == before  # byte-identical


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
    ev = _event()  # no entry_price / reference_price
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert "bracket" not in ev.payload.metadata


def test_reference_price_is_accepted_as_the_entry_ref() -> None:
    ev = _event(reference_price="8.00")
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert ev.payload.metadata["bracket_target_price"] == "8.16"  # 8.00*1.02, >$1 -> 2dp


def test_emit_rounds_exit_prices_to_schwab_tick_rule() -> None:
    """Schwab FIRM-REJECTS >2 decimals above $1 (ADVB 2026-07-22 CANCELED_BY_FIRM). Above $1 ->
    2 decimals; at/below $1 -> 4 decimals."""
    ev = _event(entry_price="11.11")  # >$1: +2%/-5% must round to 2dp
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    md = ev.payload.metadata
    assert md["bracket_target_price"] == "11.33"  # 11.11*1.02=11.3322 -> 2dp
    assert md["bracket_stop_price"] == "10.55"  # 11.11*0.95=10.5545 -> 2dp
    assert "." in md["bracket_target_price"] and len(md["bracket_target_price"].split(".")[1]) == 2

    ev2 = _event(entry_price="0.71")  # <=$1: up to 4 decimals allowed
    _svc(True)._apply_v2_oco_bracket_entry(event=ev2)
    assert len(ev2.payload.metadata["bracket_target_price"].split(".")[1]) == 4


def test_emit_skipped_outside_regular_hours(monkeypatch) -> None:
    """RTH-only: the native OCO is a regular-session construct. Pre/post-market entries must NOT
    get a bracket -- they fall back to the plain single-leg entry (software ladder owns the exit).
    v2 enters from 07:00 ET, so this is the pre-market path."""
    import project_mai_tai.oms.service as svc

    monkeypatch.setattr(svc, "_is_regular_market_session", lambda now=None: False)
    ev = _event(entry_price="10.00")
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert "bracket" not in ev.payload.metadata  # no bracket emitted pre-market


def test_emit_active_during_regular_hours(monkeypatch) -> None:
    import project_mai_tai.oms.service as svc

    monkeypatch.setattr(svc, "_is_regular_market_session", lambda now=None: True)
    ev = _event(entry_price="10.00")
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert ev.payload.metadata["bracket"] == "true"  # bracket emitted in RTH


# ==================================================================================================
# ⛔⭐⭐ BROKER SCOPE (2026-08-19). This decorator is SCHWAB-SHAPED and had no broker gate, so it
# stamped a Schwab bracket onto the WEBULL fan-out legs of the same signal. On the bare
# `rth_resting_mirror` leg that converted a shape Webull ACCEPTS (stop-limit master STANDALONE, Probe
# W 200) into the one it REFUSES (combo master, 417) — and our own adapter guard then aborted it
# CLIENT-SIDE, so it never reached the broker.
#
# MEASURED: 570 of 572 mirror orders carried these keys and were refused; the only 2 that ever
# FILLED are the 2 that escaped the stamping. Five sessions, zero mirror fills.
# ==================================================================================================

_BRACKET_KEYS = (
    "bracket",
    "bracket_entry_type",
    "native_oco_bracket",
    "bracket_target_price",
    "bracket_stop_price",
)


def _mirror_event() -> TradeIntentEvent:
    """The bare Webull resting mirror, exactly as the strategy emits it (no bracket_* keys)."""
    ev = _event(
        order_type="STOP_LIMIT",
        fanout_leg="webull",
        fanout_source="rth_resting_mirror",
        stop_price="2.4500",
        limit_price="2.4600",
        entry_price="2.4487",
        reference_price="2.4487",
    )
    ev.payload.broker_account_name = "live:orb"
    return ev


def test_webull_stop_limit_master_is_left_BARE() -> None:
    """⛔⭐⭐ THE FIX. The leg the strategy sends bare must reach the adapter bare."""
    ev = _mirror_event()
    before = dict(ev.payload.metadata)
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert ev.payload.metadata == before, "the bare mirror leg must not be decorated at all"
    for key in _BRACKET_KEYS:
        assert key not in ev.payload.metadata


def test_the_webull_stop_limit_skip_is_logged_with_symbol_and_account(caplog) -> None:
    """A silent skip is how this class of defect hides. It must announce itself."""
    with caplog.at_level(logging.INFO):
        _svc(True)._apply_v2_oco_bracket_entry(event=_mirror_event())
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "[V2-OCO-EMIT]" in line and "webull stop-limit master" in line
    assert "KIDZ" in line and "live:orb" in line


def test_the_SCHWAB_primary_stop_limit_is_STILL_bracketed() -> None:
    """⛔ The scope is by BROKER, not by order type. v2's own resting STOP_LIMIT keeps its bracket —
    Schwab accepts the identical shape (previewOrder 200, same session)."""
    ev = _event(
        order_type="STOP_LIMIT", entry_price="10.00", stop_price="10.00", limit_price="10.05"
    )
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert ev.payload.metadata["bracket_entry_type"] == "STOP_LIMIT"
    assert ev.payload.metadata["native_oco_bracket"] == "true"


def test_the_webull_LIMIT_fanout_KEEPS_its_bracket() -> None:
    """⛔⭐ 174 live fan-out brackets in 14 days depend on the LIMIT-master shape. Excluding all
    Webull legs instead of just the stop-limit master would strip protection from every one."""
    ev = _event(
        order_type="limit", fanout_leg="webull", fanout_source="reactive", entry_price="5.00"
    )
    ev.payload.broker_account_name = "live:orb"
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert ev.payload.metadata["bracket_entry_type"] == "LIMIT"
    assert ev.payload.metadata["native_oco_bracket"] == "true"


def test_the_webull_MARKET_fanout_KEEPS_its_bracket() -> None:
    ev = _event(
        order_type="market", fanout_leg="webull", fanout_source="reactive", entry_price="5.00"
    )
    ev.payload.broker_account_name = "live:orb"
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert ev.payload.metadata["bracket_entry_type"] == "MARKET"


def test_the_scope_matches_the_LEG_not_the_account_name() -> None:
    """⛔ Keyed on `fanout_leg`, not on the account string. The account is a config value that can be
    renamed; the leg marker is what the strategy actually stamps."""
    ev = _event(order_type="STOP_LIMIT", fanout_leg="webull", entry_price="2.44")
    ev.payload.broker_account_name = "live:schwab_1m_v2"  # deliberately the WRONG account name
    _svc(True)._apply_v2_oco_bracket_entry(event=ev)
    assert "native_oco_bracket" not in ev.payload.metadata
