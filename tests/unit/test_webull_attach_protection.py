"""A Webull resting entry fills BARE — this is what puts a real stop at the broker afterwards.

⛔⭐⭐ WHY IT MUST EXIST. Webull refuses a stop-limit master carrying a bracket (Probe W shape B,
2026-08-13: HTTP 417 `invalid order_type`). So a resting Webull entry cannot bring its protection
with it. Without this attach step the position runs on SOFTWARE-ONLY stops for its entire life —
nothing sitting at the broker at all.

⭐ THE SHAPE IS BROKER-PROVEN (Probe W4, CORE/RTH, live:orb, preview_order):
    [STOP_PROFIT, STOP_LOSS] with NO master      -> HTTP 200  ✅   <- what we send
    [OCO, OCO]                                   -> 417 invalid combo_type
    [STOP_LOSS_PROFIT] (one leg, both prices)    -> 417 invalid combo_type

⛔ TWO WAYS THIS LOSES MONEY QUIETLY, both pinned below:
   1. the attach fails and nobody is told  -> holding with no stop
   2. the two legs are not linked          -> stop fills, target survives, account goes SHORT
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.oms import service as svc


# ------------------------------------------------------------------ the payload the broker accepts
def _req(**md):
    meta = {"bracket_target_price": "5.10", "bracket_stop_price": "4.75"}
    meta.update(md)
    return SimpleNamespace(
        client_order_id="coid-1", symbol="TEST", side="sell", quantity=Decimal("2"),
        metadata=meta, time_in_force="day",
    )


def _adapter() -> WebullBrokerAdapter:
    a = object.__new__(WebullBrokerAdapter)
    return a


def test_the_pair_has_NO_master_leg() -> None:
    """The whole point: it protects a position we already hold, so there is no entry to lead it."""
    legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), _req())
    assert len(legs) == 2
    assert {leg["combo_type"] for leg in legs} == {"STOP_PROFIT", "STOP_LOSS"}
    assert all(leg["combo_type"] != "MASTER" for leg in legs)


def test_it_uses_the_EXACT_combo_tags_the_broker_accepted() -> None:
    """⛔ `OCO`/`OCO` and a single `STOP_LOSS_PROFIT` were both 417'd by Webull on 2026-08-13.
    Only STOP_PROFIT + STOP_LOSS passed. Changing these tags breaks every attach."""
    legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), _req())
    tags = [leg["combo_type"] for leg in legs]
    assert "OCO" not in tags and "STOP_LOSS_PROFIT" not in tags


def test_both_legs_are_SELLs_with_the_right_prices() -> None:
    legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), _req())
    by = {leg["combo_type"]: leg for leg in legs}
    assert by["STOP_PROFIT"]["side"] == "SELL"
    assert by["STOP_PROFIT"]["order_type"] == "LIMIT"
    assert by["STOP_PROFIT"]["limit_price"] == "5.10"
    assert by["STOP_LOSS"]["side"] == "SELL"
    assert by["STOP_LOSS"]["order_type"] == "STOP_LOSS"
    assert by["STOP_LOSS"]["stop_price"] == "4.75"


def test_neither_leg_id_can_exceed_the_40_CHAR_BROKER_CAP() -> None:
    """⛔ Webull 417s a client_order_id over 40 chars, and the attach's own base is
    `<strategy>-<SYM>-protect-<12hex>` = 39 for a 5-char symbol. A bare f"{coid}T" lands EXACTLY on
    the cap and goes OVER it for anything longer -- so the pair could never place, on the one path
    that is a bare position's only protection. Pin the cap, not the arithmetic that happens to fit
    today.
    """
    for base in ("schwab_1m_v2-XHG-protect-0123456789ab",       # 3-char symbol
                 "schwab_1m_v2-ABCDE-protect-0123456789ab",     # 5-char symbol -> 39
                 "x" * 40,                                      # already AT the cap
                 "y" * 80):                                     # absurd, must still be bounded
        req = _req()
        req.client_order_id = base
        legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), req)
        ids = [leg["client_order_id"] for leg in legs]
        for cid in ids:
            assert len(cid) <= 40, f"{cid!r} is {len(cid)} chars — the broker will 417 it"
        assert ids[0] != ids[1], "the two legs must never collide on one id"


def test_half_a_pair_is_REFUSED_rather_than_sent() -> None:
    """⛔ One leg alone is an unpaired sell reserving the shares — the E5/NXTC oversell shape."""
    for missing in ("bracket_target_price", "bracket_stop_price"):
        req = _req()
        del req.metadata[missing]
        with pytest.raises(RuntimeError, match="missing metadata"):
            WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), req)


def test_the_legs_share_ONE_combo_id_so_the_broker_links_them() -> None:
    """⛔ THE SHORT-POSITION GUARD. One `client_combo_order_id` is what makes the broker cancel the
    survivor when one leg fills. Without it a filled stop leaves the target working against shares
    we no longer own."""
    src = inspect.getsource(WebullBrokerAdapter._submit_exit_pair_blocking)
    assert "client_combo_order_id=request.client_order_id" in src


def test_the_pair_routes_to_the_combo_endpoint_only_when_asked() -> None:
    """Every existing path must be byte-identical unless the caller sets the flag."""
    src = inspect.getsource(WebullBrokerAdapter.submit_order)
    assert "_is_exit_only_pair(request)" in src
    gate = inspect.getsource(WebullBrokerAdapter._is_exit_only_pair)
    assert "webull_exit_only_pair" in gate


# ----------------------------------------------------------------------------- the OMS attach path
class _Adapter:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def submit_order(self, request):
        self.calls.append(request)
        o = self.outcomes.pop(0) if self.outcomes else "accepted"
        if isinstance(o, Exception):
            raise o
        return [SimpleNamespace(event_type=o, reason="scripted")]


def _svc(adapter):
    s = object.__new__(svc.OmsRiskService)
    s.settings = SimpleNamespace(
        oms_v2_cw_target_pct=2.0, oms_v2_cw_hard_stop_pct=5.0,
        oms_webull_protect_attempts=3, oms_webull_protect_interval_seconds=0.0,
    )
    s.logger = logging.getLogger("test-attach")
    s.broker_adapter = adapter
    s._webull_protect_base = {}
    return s


def _run(s):
    return asyncio.run(s._attach_webull_protection(
        broker_account_name="live:orb", symbol="TEST", quantity=1,
        entry_price=5.0, strategy_code="schwab_1m_v2"))


def test_it_attaches_and_stops(caplog: pytest.LogCaptureFixture) -> None:
    a = _Adapter(["accepted"])
    with caplog.at_level(logging.INFO):
        _run(_svc(a))
    assert len(a.calls) == 1
    assert "[WEBULL-PROTECT-ATTACHED]" in caplog.text
    md = a.calls[0].metadata
    assert md["bracket_target_price"] == "5.1000"   # +2%
    assert md["bracket_stop_price"] == "4.7500"     # -5%


def test_it_REMEMBERS_the_base_id_so_the_pair_can_later_be_RELEASED() -> None:
    """⛔ THE LEGS ARE UNQUERYABLE. They are broker-created and never land in `broker_orders`, so
    this coid is the only handle that will ever exist on them. Forget it and the pair can be placed
    but never cancelled — which means the software ladder can never sell into it, and we are back to
    the 58-reject XHG storm with no way to tell from the outside.

    A mutation that dropped this line left every other attach test green.
    """
    a = _Adapter(["accepted"])
    s = _svc(a)
    _run(s)
    assert s._webull_protect_base[("live:orb", "TEST")] == a.calls[0].client_order_id


def test_a_FAILED_attach_records_NO_base_id() -> None:
    """Nothing is resting, so there is nothing to release. A recorded id here would send cancels at
    an order that never existed and then latch the release as done."""
    a = _Adapter(["rejected", "rejected", "rejected"])
    s = _svc(a)
    _run(s)
    assert ("live:orb", "TEST") not in s._webull_protect_base


def test_it_RETRIES_a_refusal() -> None:
    a = _Adapter(["rejected", "rejected", "accepted"])
    _run(_svc(a))
    assert len(a.calls) == 3, "must keep trying — an unprotected position is the harm"


def test_a_raise_does_not_end_it() -> None:
    a = _Adapter([RuntimeError("network"), "accepted"])
    _run(_svc(a))
    assert len(a.calls) == 2


def test_total_failure_WARNS_and_says_the_position_is_unprotected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⛔ Silence here means holding with no stop and nobody knowing."""
    a = _Adapter(["rejected", "rejected", "rejected"])
    with caplog.at_level(logging.WARNING):
        _run(_svc(a))
    assert "[WEBULL-PROTECT-FAILED]" in caplog.text
    assert "NO BROKER-SIDE STOP" in caplog.text
    assert "TEST" in caplog.text


def test_only_a_BARE_fill_triggers_it() -> None:
    """⛔ A bracketed entry already has protection live at the fill; a second pair would reserve the
    shares twice and draw an oversell refusal."""
    whole = inspect.getsource(svc)
    seg = whole.split("[OMS-V2-MANAGED-OPEN]")[1][:1500]
    assert 'native_oco_bracket' in seg and '!= "true"' in seg
    assert 'fanout_leg' in seg


def test_the_attach_runs_OFF_the_fill_path() -> None:
    """It sleeps between retries; blocking the fill path with it would delay real executions."""
    src = inspect.getsource(svc.OmsRiskService._spawn_webull_protection)
    assert "ensure_future" in src
