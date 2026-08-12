"""The Webull FAN-OUT leg still chases in regular hours — #674 capped only the Schwab primary.

⛔⭐⭐ THE GAP. `_v2_rth_reactive_limit_applies` says it in the source: *"the fan-out leg is
deliberately untouched here"*, and excludes it with `md.get("fanout_leg", "") == ""`. Meanwhile the
strategy builds that leg as `order_type: "limit" if session_is_eh else "market"`
(`schwab_1m_v2.py:2236`) — so in RTH it is an UNCAPPED MARKET order, on BOTH fan-out sources
(`reactive` AND `rth_resting`).

MEASURED COST: market entries own the +610 bps (WXM 08-11) and +684 bps (AMIX) outliers. On
2026-08-12 BAOS the Schwab primary decided 1.1702 under its #674 cap while this leg sent MARKET and
paid 1.1800 — then lost 5.08%.

⭐ AND THE CAP IS FREE HERE. Probe W (2026-08-12, CORE/RTH): Webull ACCEPTS a LIMIT master with
STOP_PROFIT + STOP_LOSS attached (HTTP 200, placed live) and REFUSES a STOP_LIMIT master (417). So a
band-capped LIMIT keeps the broker-side bracket 174 live fan-out entries depend on.
"""
from __future__ import annotations

from types import SimpleNamespace

from project_mai_tai.oms import service as svc


def _event(**md_overrides) -> SimpleNamespace:
    md = {
        "path": "ATR Flip",
        "atr_variant": "CW-v2-fanout",
        "entry_price": "1.1702",
        "reference_price": "1.1702",
        "fanout_leg": "webull",
        "fanout_source": "reactive",
        "order_type": "market",
    }
    md.update(md_overrides)
    return SimpleNamespace(
        payload=SimpleNamespace(
            strategy_code="schwab_1m_v2", intent_type="open", side="buy",
            symbol="BAOS", metadata=md,
        )
    )


def _svc(enabled: bool = True) -> svc.OmsRiskService:
    s = object.__new__(svc.OmsRiskService)
    s.settings = SimpleNamespace(oms_v2_rth_fanout_limit_enabled=enabled)
    return s


def _in_rth(monkeypatch, value: bool = True) -> None:
    monkeypatch.setattr(svc, "_is_regular_market_session", lambda *a, **k: value)


# ----------------------------------------------------------------------------- the gate itself
def test_the_rth_fanout_leg_is_in_scope(monkeypatch) -> None:
    """THE REGRESSION THIS CLOSES: a webull fan-out MARKET leg in RTH must now be re-priced."""
    _in_rth(monkeypatch)
    assert _svc()._v2_rth_fanout_limit_applies(_event()) is True


def test_it_covers_the_RESTING_fanout_source_too(monkeypatch) -> None:
    """⛔ Both fan-out sources send MARKET in RTH. Keying on the SOURCE would have caught only one;
    the gate keys on `fanout_leg` + `order_type` precisely so `rth_resting` is covered as well."""
    _in_rth(monkeypatch)
    assert _svc()._v2_rth_fanout_limit_applies(_event(fanout_source="rth_resting")) is True


def test_flag_off_is_byte_identical(monkeypatch) -> None:
    _in_rth(monkeypatch)
    assert _svc(enabled=False)._v2_rth_fanout_limit_applies(_event()) is False


def test_default_setting_is_off() -> None:
    from project_mai_tai.settings import Settings

    assert Settings().oms_v2_rth_fanout_limit_enabled is False


# ------------------------------------------------------------------------------ what it must NOT touch
def test_an_already_priced_leg_is_out_of_scope(monkeypatch) -> None:
    """⭐ Self-limiting by design: once the leg carries a price it is out of scope, so this can
    never double-price an order."""
    _in_rth(monkeypatch)
    assert _svc()._v2_rth_fanout_limit_applies(_event(order_type="limit")) is False


def test_the_schwab_primary_is_NOT_touched_by_this(monkeypatch) -> None:
    """⛔ The primary is #674's job. Two builders re-pricing one order is the double-price bug."""
    _in_rth(monkeypatch)
    primary = _event(fanout_leg="", atr_variant="CW-v2")
    assert _svc()._v2_rth_fanout_limit_applies(primary) is False


def test_EH_is_out_of_scope(monkeypatch) -> None:
    """The EH fan-out leg is already a limit and is priced by the EH builder."""
    _in_rth(monkeypatch, value=False)
    assert _svc()._v2_rth_fanout_limit_applies(_event()) is False


def test_non_v2_and_sells_are_out_of_scope(monkeypatch) -> None:
    _in_rth(monkeypatch)
    other = _event()
    other.payload.strategy_code = "orb"
    assert _svc()._v2_rth_fanout_limit_applies(other) is False
    sell = _event()
    sell.payload.side = "sell"
    assert _svc()._v2_rth_fanout_limit_applies(sell) is False


# --------------------------------------------------------------------------------------- the wiring
def test_the_applier_is_actually_CALLED(monkeypatch) -> None:
    """Pins the WIRING. A correct builder nobody calls is worthless — the trap
    `test_oms_cancel_intent_terminal` was written for."""
    import inspect

    src = inspect.getsource(svc.OmsRiskService.process_trade_intent)
    assert "_apply_v2_rth_fanout_limit(" in src


def test_the_two_builders_are_mutually_exclusive() -> None:
    """⛔ The primary builder EXCLUDES fanout legs; this one REQUIRES them. If that ever converges,
    one order gets priced twice."""
    import inspect

    primary = inspect.getsource(svc.OmsRiskService._v2_rth_reactive_limit_applies)
    fanout = inspect.getsource(svc.OmsRiskService._v2_rth_fanout_limit_applies)
    assert 'str(md.get("fanout_leg", "")) == ""' in primary
    assert 'str(md.get("fanout_leg", "")).lower() == "webull"' in fanout


def test_reference_price_is_not_overwritten() -> None:
    """⛔ Every slippage study measures fill-vs-DECISION. Overwriting the reference silently turns
    that into fill-vs-fill and reports ~0 — on the leg whose slippage we most want to see."""
    import inspect

    src = inspect.getsource(svc.OmsRiskService._apply_v2_rth_fanout_limit)
    assert 'md["reference_price"]' not in src
    assert 'md["limit_price"] = limit_s' in src
