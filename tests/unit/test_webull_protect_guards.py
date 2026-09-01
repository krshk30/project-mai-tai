"""Pre-flight guards on the Webull protective attach — and what they must NOT do.

⛔⛔ READ THIS BEFORE TREATING A FALLING REFUSAL COUNT AS A FIX. Measured 2026-08-17 across all
seven retained `oms.log` files (08-11 -> 08-17), this path has **never once succeeded**: zero
`[WEBULL-PROTECT-ATTACHED]`, zero `[WEBULL-EXIT-PAIR-PLACED]`. 08-14 alone was 10 episodes and 0
attaches, across BOTH callers — #689's bare-fill attach (fires ~0.2s after the fill) and #692's
reprotect (37s–10min later) — refused identically whether the position was visible or not.

So these guards are NOISE fixes. They stop us sending orders we can already tell will be refused.
They do not make the pair place, and they protect nothing that was not protected before. The live
PASS to require is a `[WEBULL-PROTECT-ATTACHED]`, which has never yet been observed.

⭐ THE OVERREACH IS THE REAL RISK HERE, not the under-reach. A guard that abandons too eagerly
leaves a real position with nothing at the broker — strictly worse than the noise it removes. That
is why the FLAT_INFERRED and UNKNOWN cases below are pinned as hard as the FLAT_CONFIRMED one.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.oms import service as svc


@pytest.fixture(autouse=True)
def _attach_tests_run_in_rth(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests grade attach internals, not the new outside-RTH refusal boundary."""
    monkeypatch.setattr(svc, "_is_regular_market_session", lambda: True)


class _Adapter:
    """Records every submit. Accepts whatever it is given."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    async def submit_order(self, request):
        self.calls.append(request)
        return [SimpleNamespace(event_type="accepted", reason="ok")]


def _svc(adapter: _Adapter, *, state=None, quote=None) -> svc.OmsRiskService:
    s = object.__new__(svc.OmsRiskService)
    s.settings = SimpleNamespace(
        oms_v2_cw_target_pct=2.0, oms_v2_cw_hard_stop_pct=5.0,
        oms_webull_protect_attempts=3, oms_webull_protect_interval_seconds=0.0,
    )
    s.logger = logging.getLogger("test-protect-guards")
    s.broker_adapter = adapter
    s._webull_protect_base = {}

    async def _state(_acct, _sym):
        return state if state is not None else svc._PositionRead.UNKNOWN

    async def _quote(*, broker_account_name, symbol):  # noqa: ARG001 - signature must match
        return dict(quote or {})

    s._broker_symbol_position_state = _state
    s._fetch_quote_for_order = _quote
    return s


def _run(s) -> None:
    asyncio.run(s._attach_webull_protection(
        broker_account_name="live:orb", symbol="TEST", quantity=1,
        entry_price=5.0, strategy_code="schwab_1m_v2"))
    # entry 5.00 -> target 5.10 (+2%), stop 4.75 (-5%)


# --------------------------------------------------------------- guard 1: do we still hold it?
def test_a_CONFIRMED_flat_abandons_without_sending(caplog: pytest.LogCaptureFixture) -> None:
    """2 of 27 refusals on 08-14 were SYMBOL_CAN_NOT_SELL_SHORT — the shares had already gone, so
    the attach was sending a protective SELL against nothing, which Webull reads as opening a
    short."""
    a = _Adapter()
    with caplog.at_level(logging.INFO):
        _run(_svc(a, state=svc._PositionRead.FLAT_CONFIRMED))
    assert a.calls == [], "nothing to protect — must not send"
    assert "[WEBULL-PROTECT-ABANDONED]" in caplog.text


def test_FLAT_INFERRED_still_attaches() -> None:
    """⛔⭐ THE OVERREACH TEST. FLAT_INFERRED is the ORDINARY shape inside the settle window: live
    08-14 CGTL read FLAT_INFERRED (n=0) for 12.7s after a real fill. Abandoning on it would walk
    away from exactly the bare fills this path exists to cover."""
    a = _Adapter()
    _run(_svc(a, state=svc._PositionRead.FLAT_INFERRED))
    assert len(a.calls) == 1, "an inconclusive read must never cost us protection"


def test_UNKNOWN_still_attaches() -> None:
    """Same discipline as `_v2_close_reconcile_flat`: HELD and UNKNOWN both continue."""
    a = _Adapter()
    _run(_svc(a, state=svc._PositionRead.UNKNOWN))
    assert len(a.calls) == 1


def test_HELD_still_attaches() -> None:
    a = _Adapter()
    _run(_svc(a, state=svc._PositionRead.HELD))
    assert len(a.calls) == 1


# ------------------------------------------------------- guard 2: would the broker refuse these?
def test_a_stop_at_or_above_the_market_is_not_sent(caplog: pytest.LogCaptureFixture) -> None:
    """Webull: "The stop price of the stop-loss order should be lower than the current market
    price." Stop is 4.75 and every proxy sits at or below it."""
    a = _Adapter()
    with caplog.at_level(logging.WARNING):
        _run(_svc(a, quote={"bid_price": 4.70, "ask_price": 4.74, "last_price": 4.72}))
    assert a.calls == []
    assert "[WEBULL-PROTECT-UNPLACEABLE]" in caplog.text


def test_a_target_at_or_below_the_market_is_not_sent(caplog: pytest.LogCaptureFixture) -> None:
    """The live 08-14 CGTL 15:14 shape: we sent target 5.2173 against Webull's own "should be
    higher than 5.23"."""
    a = _Adapter()
    with caplog.at_level(logging.WARNING):
        _run(_svc(a, quote={"bid_price": 5.20, "ask_price": 5.25, "last_price": 5.22}))
    assert a.calls == []
    assert "[WEBULL-PROTECT-UNPLACEABLE]" in caplog.text


def test_levels_that_straddle_the_market_ARE_sent() -> None:
    """The control. Without this, a guard that refused everything would still pass the two tests
    above and look correct."""
    a = _Adapter()
    _run(_svc(a, quote={"bid_price": 4.99, "ask_price": 5.01, "last_price": 5.00}))
    assert len(a.calls) == 1


def test_NO_QUOTE_means_no_opinion_and_the_pair_is_still_sent() -> None:
    """⛔⭐ Not knowing the price is not evidence the levels are wrong. This guard must never be the
    reason a position goes uncovered — and it is INERT wherever nothing can quote, so its silence
    in the log is not proof it ran."""
    a = _Adapter()
    _run(_svc(a, quote={}))
    assert len(a.calls) == 1


def test_a_single_borderline_proxy_does_not_block_the_send() -> None:
    """⛔ Biased towards SENDING: we skip only when a level is unplaceable against EVERY proxy. A
    false skip costs real protection; a false send costs one log line."""
    a = _Adapter()
    # stop 4.75 is above the bid but below ask/last -> still placeable somewhere, so send.
    _run(_svc(a, quote={"bid_price": 4.70, "ask_price": 5.02, "last_price": 5.00}))
    assert len(a.calls) == 1


# ------------------------------------------------------------------ guard 3: one attach at a time
def test_a_second_attach_is_COALESCED_into_the_one_already_running(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Live 08-14 STKH interleaved two sequences on ONE fill: `1/3, 2/3, 1/3, 3/3, FAILED, 2/3,
    3/3, FAILED`. Two FAILED lines, one position — which also means any unprotected count read off
    those lines is INFLATED."""
    started = 0

    async def _slow(**_kw):
        nonlocal started
        started += 1
        await asyncio.sleep(0.05)

    async def _drive():
        s = object.__new__(svc.OmsRiskService)
        s.logger = logging.getLogger("test-protect-guards")
        s._attach_webull_protection = _slow
        first = s._spawn_webull_protection(broker_account_name="live:orb", symbol="TEST")
        second = s._spawn_webull_protection(broker_account_name="live:orb", symbol="TEST")
        assert second is first, "the second call must join the in-flight attach, not race it"
        await first
        return started

    with caplog.at_level(logging.INFO):
        assert asyncio.run(_drive()) == 1
    assert "[WEBULL-PROTECT-COALESCED]" in caplog.text


def test_a_DIFFERENT_symbol_is_not_coalesced() -> None:
    """The latch is per position. Blocking an unrelated symbol's protection would be a far worse
    bug than the interleaving it fixes."""
    started = 0

    async def _slow(**_kw):
        nonlocal started
        started += 1
        await asyncio.sleep(0.01)

    async def _drive():
        s = object.__new__(svc.OmsRiskService)
        s.logger = logging.getLogger("test-protect-guards")
        s._attach_webull_protection = _slow
        a = s._spawn_webull_protection(broker_account_name="live:orb", symbol="AAA")
        b = s._spawn_webull_protection(broker_account_name="live:orb", symbol="BBB")
        assert a is not b
        await asyncio.gather(a, b)
        return started

    assert asyncio.run(_drive()) == 2


def test_the_slot_FREES_after_the_attach_finishes() -> None:
    """⛔ A latch that outlives its attach would mean the NEXT position on this symbol could never
    be protected — silently, because the code would still look like it was handling the case."""
    async def _quick(**_kw):
        return None

    async def _drive():
        s = object.__new__(svc.OmsRiskService)
        s.logger = logging.getLogger("test-protect-guards")
        s._attach_webull_protection = _quick
        first = s._spawn_webull_protection(broker_account_name="live:orb", symbol="TEST")
        await first
        second = s._spawn_webull_protection(broker_account_name="live:orb", symbol="TEST")
        await second
        return first is not second

    assert asyncio.run(_drive()), "a finished attach must release its slot"


# ------------------------------------------- the forwarder, without which the quote guard is inert
def test_the_router_borrows_a_quote_from_a_provider_that_HAS_one() -> None:
    """⛔⭐ THE SILENT-NO-OP TRAP. The OMS holds the ROUTER, not a leaf adapter. `fetch_quotes` is
    implemented ONLY by the Schwab adapter, so without this forwarder every Webull-account quote
    lookup returns {} and the placeability guard reads as present while never once running — the
    same shape as the `fetch_oco_exit_fill` forwarder bug found on 2026-07-27."""
    class _Quoteless:
        async def submit_order(self, request): ...

    class _Quoter:
        async def fetch_quotes(self, symbols):
            return {s.upper(): {"bid_price": 1.0, "ask_price": 1.1, "last_price": 1.05}
                    for s in symbols}

    router = RoutingBrokerAdapter(
        default_provider="webull",
        provider_by_account={"live:orb": "webull"},
        factories_by_provider={"webull": _Quoteless, "schwab": _Quoter},
    )
    quotes = asyncio.run(router.fetch_quotes(["TEST"]))
    assert quotes["TEST"]["bid_price"] == 1.0, "a Webull-held symbol must still be quotable"


def test_the_router_returns_empty_when_NOTHING_can_quote() -> None:
    """A real "we cannot know", which the caller must not confuse with "the price is fine"."""
    class _Quoteless:
        async def submit_order(self, request): ...

    router = RoutingBrokerAdapter(
        default_provider="webull",
        provider_by_account={"live:orb": "webull"},
        factories_by_provider={"webull": _Quoteless},
    )
    assert asyncio.run(router.fetch_quotes(["TEST"])) == {}
