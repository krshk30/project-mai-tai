"""The software close was selling shares its OWN resting exit pair had reserved.

⛔⭐⭐ THE DEFECT (live 2026-08-13, `live:orb`). A resting exit leg RESERVES the position at the
broker. The v2 ladder then sends its own market sell for the same shares, Webull sees
available-to-sell = 0 and 417s it as a naked short:
    NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K   39
    ORDER_NOT_SUPPORT_REVERSE_OPTION                              19
56 of the 58 were ONE XHG share; 48 of those inside five minutes.

⭐ THE ASYMMETRY THAT EXPLAINS IT. `-close-` filled 4/62 at Webull against 5/6 at Schwab. Schwab
stands its ladder down while a bracket is armed (`_native_oco_stand_down_active`); Webull exposes
no `fetch_armed_native_oco_symbols`, and `routing.py` fails OPEN, so the ladder fires into its own
reservation.

⛔ WHY THIS IS EVEN POSSIBLE: the OCO children are broker-created and never land in `broker_orders`
(store.py says a DB query for them always returns nothing), so they cannot be looked UP — but they
are placed under DETERMINISTIC coids, so they can be ADDRESSED BY NAME.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.oms import service as svc


# --------------------------------------------------------------- the adapter can address the legs
def _adapter() -> WebullBrokerAdapter:
    return object.__new__(WebullBrokerAdapter)


def test_the_two_legs_are_addressable_by_deterministic_id() -> None:
    ids = WebullBrokerAdapter.exit_pair_leg_client_order_ids(_adapter(), "base-coid")
    assert ids == ["base-coidT", "base-coidS"]


def test_leg_ids_stay_inside_the_40_CHAR_CAP() -> None:
    """⛔ Same cap that broke the attach. A cancel aimed at an over-long id addresses an order that
    cannot exist, so the reservation is never released — which reads exactly like the bug."""
    ids = WebullBrokerAdapter.exit_pair_leg_client_order_ids(_adapter(), "z" * 60)
    assert all(len(i) <= 40 for i in ids)
    assert ids[0] != ids[1]


def test_no_base_id_means_NOTHING_is_cancelled() -> None:
    """⛔ An empty base must never fan out into blind cancels."""
    assert WebullBrokerAdapter.exit_pair_leg_client_order_ids(_adapter(), "") == []


class _Adapter:
    """Records what the OMS asked the broker to do."""

    def __init__(self, *, has_capability: bool = True) -> None:
        self.cancelled: list[tuple[str, str]] = []
        if not has_capability:
            # Schwab / simulated: no addressable resting legs.
            self.cancel_exit_pair = None  # type: ignore[assignment]

    async def cancel_exit_pair(self, *, broker_account_name, symbol, base_client_order_id):
        self.cancelled.append((symbol, base_client_order_id))
        return [SimpleNamespace(event_type="cancelled"), SimpleNamespace(event_type="cancelled")]


def _svc(adapter, *, base: str = "protect-base") -> svc.OmsRiskService:
    s = object.__new__(svc.OmsRiskService)
    s.settings = SimpleNamespace(oms_v2_exit_release_reservation_enabled=True)
    s.logger = logging.getLogger("test-release")
    s.broker_adapter = adapter
    s._webull_protect_base = {("live:orb", "XHG"): base} if base else {}
    s._exit_reservation_released = set()
    s._find_oco_entry_order = lambda *a, **k: None
    return s


def _release(s, symbol: str = "XHG") -> bool:
    return asyncio.run(s._release_exit_reservation_before_close(
        session=object(), broker_account_name="live:orb", symbol=symbol))


def test_it_CANCELS_the_resting_pair_before_the_close() -> None:
    a = _Adapter()
    assert _release(_svc(a)) is True
    assert a.cancelled == [("XHG", "protect-base")]


def test_it_cancels_ONCE_PER_EPISODE_not_once_per_quote_tick() -> None:
    """⛔ THE FIX MUST NOT BECOME THE BUG. The ladder re-evaluates every quote tick — 48 times in
    five minutes for XHG. Re-cancelling each tick would just swap one storm for another."""
    a = _Adapter()
    s = _svc(a)
    for _ in range(25):
        _release(s)
    assert len(a.cancelled) == 1, "a released reservation must not be re-cancelled every tick"


def test_the_latch_is_CLEARED_when_the_position_closes() -> None:
    """⛔ A latch that outlives its position means the NEXT entry's reservation is never released
    and the storm returns silently — the code would still look like it handles the case."""
    a = _Adapter()
    s = _svc(a)
    _release(s)
    assert len(a.cancelled) == 1
    svc.OmsRiskService._clear_exit_reservation_release(s, "live:orb", "XHG")
    s._webull_protect_base[("live:orb", "XHG")] = "protect-base-2"
    _release(s)
    assert len(a.cancelled) == 2, "the next position must release its own legs"


def test_an_adapter_with_NO_capability_changes_nothing() -> None:
    """Schwab/simulated: no addressable legs -> byte-identical to today, and NOT latched as
    released (claiming a release we never performed would be worse than doing nothing)."""
    a = _Adapter(has_capability=False)
    s = _svc(a)
    s.broker_adapter = SimpleNamespace()   # no cancel_exit_pair at all

    async def _no_cap(**kw):
        return []
    s.broker_adapter.cancel_exit_pair = _no_cap
    assert _release(s) is False
    assert ("live:orb", "XHG") not in s._exit_reservation_released


def test_no_known_base_and_no_entry_order_cancels_NOTHING() -> None:
    a = _Adapter()
    s = _svc(a, base="")
    assert _release(s) is False
    assert a.cancelled == []


def test_it_falls_back_to_the_ENTRY_coid_when_the_attach_id_is_forgotten() -> None:
    """The attach base is in-memory only, so a restart loses it. The bracket ENTRY order IS a real
    `broker_orders` row, and the native combo's legs hang off it."""
    a = _Adapter()
    s = _svc(a, base="")
    s._find_oco_entry_order = lambda *a_, **k: SimpleNamespace(client_order_id="entry-coid")
    assert _release(s) is True
    assert a.cancelled == [("XHG", "entry-coid")]


def test_a_RAISING_cancel_never_blocks_the_close() -> None:
    """⛔ The close is protection. A failed release must degrade to today's behaviour — a possibly
    refused sell — never to NO sell at all."""
    class _Boom:
        async def cancel_exit_pair(self, **kw):
            raise RuntimeError("network")

    s = _svc(_Boom())
    assert _release(s) is False           # returned, did not raise
    assert ("live:orb", "XHG") not in s._exit_reservation_released


# ------------------------------------------------- re-protect what the release uncovered
def _reprotect_svc(*, released: bool):
    s = object.__new__(svc.OmsRiskService)
    s.logger = logging.getLogger("test-reprotect")
    s.settings = SimpleNamespace()
    s._exit_reservation_released = {("live:orb", "XHG")} if released else set()
    s._v2_exit_close_failures = {("live:orb", "XHG"): 3}
    s._v2_exit_stood_down = set()
    s.spawned = []
    s._spawn_webull_protection = lambda **kw: s.spawned.append(kw)
    return s


def _row(entry_price=5.0, qty=1):
    return SimpleNamespace(entry_price=entry_price, current_quantity=qty,
                           strategy_code="schwab_1m_v2")


def test_a_released_position_that_will_not_CLOSE_gets_its_protection_BACK() -> None:
    """⛔⭐⭐ THE HAZARD THE RELEASE CREATES. Before it existed, a failing close was survivable —
    the OCO legs stayed put and took the position out at +2%/-5% on their own. Cancelling them
    removes that net, so a close that never goes through leaves the position NAKED. That would be
    strictly worse than the reject storm it replaced."""
    s = _reprotect_svc(released=True)
    svc.OmsRiskService._reprotect_after_failed_release(s, "live:orb", "XHG", _row())
    assert len(s.spawned) == 1
    assert s.spawned[0]["symbol"] == "XHG"
    assert s.spawned[0]["entry_price"] == 5.0
    assert s.spawned[0]["quantity"] == 1


def test_it_will_NOT_reprotect_from_an_unusable_row() -> None:
    """⛔ A pair priced off a zero entry, or for zero shares, is an unpaired sell against shares we
    may not own — the E5/NXTC oversell shape. Refuse and say so."""
    for row in (_row(entry_price=0), _row(qty=0)):
        s = _reprotect_svc(released=True)
        svc.OmsRiskService._reprotect_after_failed_release(s, "live:orb", "XHG", row)
        assert s.spawned == []


def test_reprotect_says_so_LOUDLY_when_it_cannot(caplog) -> None:
    s = _reprotect_svc(released=True)
    with caplog.at_level(logging.WARNING):
        svc.OmsRiskService._reprotect_after_failed_release(s, "live:orb", "XHG", _row(entry_price=0))
    assert "UNCOVERED" in caplog.text


def test_a_raising_reprotect_never_breaks_the_protective_sync() -> None:
    s = _reprotect_svc(released=True)

    def _boom(**kw):
        raise RuntimeError("no adapter")
    s._spawn_webull_protection = _boom
    svc.OmsRiskService._reprotect_after_failed_release(s, "live:orb", "XHG", _row())  # must not raise


def test_reprotect_fires_ONLY_when_we_actually_released() -> None:
    """A position whose legs we never cancelled is already protected — a second pair would
    double-reserve the shares and draw an oversell refusal."""
    import inspect

    src = inspect.getsource(svc.OmsRiskService._v2_close_reconcile_flat)
    assert "if key in self._exit_reservation_released:" in src
    assert "_reprotect_after_failed_release" in src
    # ...and only on a POSITIVELY-HELD read, never an inconclusive one.
    held = src.index("if state is _PositionRead.HELD:")
    assert held < src.index("_reprotect_after_failed_release")


def test_the_flag_defaults_OFF() -> None:
    """⛔ It edits the exit path and opens a brief unprotected window. Must be chosen, not
    inherited."""
    from project_mai_tai.settings import Settings

    assert Settings().oms_v2_exit_release_reservation_enabled is False


def test_the_release_runs_BEFORE_the_sell_is_submitted() -> None:
    """Ordering is the entire point — cancelling after the sell releases nothing."""
    import inspect

    src = inspect.getsource(svc.OmsRiskService._emit_v2_managed_sell)
    assert "_release_exit_reservation_before_close" in src
    assert src.index("_release_exit_reservation_before_close") < src.index(
        "reports = await self.broker_adapter.submit_order(request)"
    )


def test_the_hook_is_FLAG_GATED() -> None:
    import inspect

    src = inspect.getsource(svc.OmsRiskService._emit_v2_managed_sell)
    assert "oms_v2_exit_release_reservation_enabled" in src


def test_every_managed_row_close_clears_the_latch() -> None:
    """⛔ Counted, not eyeballed: every `close_managed_position` call site must clear the latch, or
    one forgotten path silently disables the fix for the next position on that symbol."""
    import inspect

    src = inspect.getsource(svc)
    closes = src.count("self.store.close_managed_position(")
    clears = src.count("_clear_exit_reservation_release(")
    # one definition + one call per close site
    assert clears >= closes + 1, f"{closes} close sites but only {clears - 1} latch clears"
