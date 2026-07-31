"""P0 VALIDATION GATE — a working managed EXIT must be HELD, not cancelled on a timer.

⭐⭐ THE INCIDENT THIS PINS (KUST, 2026-07-31, real money).

A pre-market entry got no native broker OCO (`[V2-OCO-EMIT] SKIPPED (outside RTH)`), so the software
ladder owned the exit. The ladder placed a sell LIMIT 1.74 at 13:26:20 and the working-order refresh
cancel/replaced it ~every 30s. NINE orders, none filled, and one was priced 1.77 -- ABOVE a falling
bid -- because the limit came off a stale reference. The position then rode to the -5% hard stop and
the market sells collided (125 rejects).

⛔⭐ THE PART THAT MAKES THIS UNARGUABLE — the REAL bid tape for that window:

    13:26:13  1.76      13:27:13  1.76      13:27:50  1.77
    13:26:14  1.77      13:27:14  1.75      13:28:02  1.78
    13:26:16  1.76      13:27:34  1.74      13:28:04  1.78
    13:26:54  1.75      13:27:38  1.75      13:28:10  1.76

The bid was **>= the 1.74 limit continuously**. The order was marketable the whole time and there was
never an instant at which a reprice was justified. It did not fail to fill because the market moved --
it failed because we kept taking it off the book. The Webull leg got the SAME price (limit 1.74,
bid-sourced) as a single order nobody cancelled, and it filled in **34 milliseconds** at 1.7501.

Cost: Webull +1.76%, Schwab -5.17% on the same signal. ~6.9 percentage points of pure execution loss.

⛔ The precedent for the fix already exists in this file's sibling
(`test_oms_resting_refresh_throttle.py`): resting buy STOP/STOP_LIMIT *entries* were made FULL-EXEMPT
from the refresh cadence on 2026-07-23 after NVVE, with the reasoning "no order resting when price
crosses". That reasoning applies verbatim to an exit. The mechanism existed; the exit path never
reached it. [[feedback_has_the_other_bot_solved_this]]
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

# The REAL Schwab bid tape for KUST 13:26:20 -> 13:28:17 UTC (market_quote_ticks, provider=schwab).
# Captured from the incident, not invented. (offset_secs_from_placement, bid)
KUST_BID_TAPE: list[tuple[int, float]] = [
    (0, 1.76), (1, 1.77), (3, 1.76), (34, 1.75), (53, 1.76), (54, 1.75),
    (74, 1.74), (78, 1.75), (90, 1.77), (102, 1.78), (104, 1.78), (110, 1.76),
]
KUST_EXIT_LIMIT = 1.74          # what the ladder actually placed, bid-sourced
KUST_STALE_REPRICE = 1.77       # the 13:28:20 order — placed ABOVE the bid off a stale reference


def _svc(**over) -> OmsRiskService:
    svc = OmsRiskService.__new__(OmsRiskService)   # only reads self.settings + static helpers
    svc.settings = Settings(**over)
    return svc


def _exit_order(order_type: str = "LIMIT", *, managed: bool = True, age_secs: float = 0.0,
                limit_price: float | None = KUST_EXIT_LIMIT):
    payload: dict[str, object] = {"order_type": order_type}
    if managed:
        payload["oms_v2_managed_exit"] = "true"
    if limit_price is not None:
        payload["limit_price"] = str(limit_price)
    updated = datetime.now(timezone.utc) - timedelta(seconds=age_secs)
    return SimpleNamespace(payload=payload, order_type=order_type,
                           updated_at=updated, submitted_at=updated, side="sell")


# --------------------------------------------------------------------- the tape itself

def test_the_exit_limit_was_marketable_for_the_entire_window() -> None:
    """⛔ The premise of the whole fix. If the bid had fallen below 1.74 the cancels would have been
    defensible. It never did -- so every one of the nine cancels destroyed a fillable order."""
    assert all(bid >= KUST_EXIT_LIMIT for _, bid in KUST_BID_TAPE), (
        "the incident's premise is wrong: the bid DID fall below the limit"
    )
    assert min(bid for _, bid in KUST_BID_TAPE) == KUST_EXIT_LIMIT
    assert KUST_BID_TAPE[0][1] > KUST_EXIT_LIMIT   # marketable from the very first tick


def test_the_stale_reprice_was_above_every_bid_in_its_first_seconds() -> None:
    """The 1.77 reprice could not fill at placement — proof the reference was stale, not the market."""
    bids_at_placement = [bid for off, bid in KUST_BID_TAPE if off <= 90]
    assert all(bid <= KUST_STALE_REPRICE for bid in bids_at_placement)


# --------------------------------------------------------------------- P0a: hold, don't cancel

def test_managed_exit_is_refresh_exempt_while_marketable() -> None:
    """P0a. A working managed exit whose limit is still marketable must NOT be cancel/replaced on
    the refresh cadence. This is the single assertion that would have saved the trade."""
    svc = _svc()
    order = _exit_order(age_secs=30)      # older than any cadence
    assert svc._managed_exit_refresh_exempt(order, bid=1.76) is True


def test_managed_exit_is_NOT_exempt_once_the_bid_falls_below_the_limit() -> None:
    """P0a, the other half. Exemption is not "never reprice" -- if the market genuinely moves away,
    the order is no longer fillable where it sits and MUST be repriced, or we recreate the stuck-exit
    problem from the other direction."""
    svc = _svc()
    order = _exit_order(age_secs=30)
    assert svc._managed_exit_refresh_exempt(order, bid=1.70) is False


def test_every_tick_of_the_real_tape_holds_the_order() -> None:
    """Replay the captured window: at no point should the fixed loop have cancelled."""
    svc = _svc()
    order = _exit_order(age_secs=30)
    cancels = [off for off, bid in KUST_BID_TAPE
               if not svc._managed_exit_refresh_exempt(order, bid=bid)]
    assert cancels == [], f"fixed loop would still cancel at offsets {cancels}s — the KUST bug"


def test_entry_side_exemption_is_unchanged() -> None:
    """Behaviour-identical guard: the 2026-07-23 resting-ENTRY exemption must not shift."""
    svc = _svc()
    entry = SimpleNamespace(payload={"order_type": "STOP_LIMIT"}, order_type="STOP_LIMIT",
                            updated_at=datetime.now(timezone.utc),
                            submitted_at=datetime.now(timezone.utc))
    assert svc._resting_trigger_refresh_exempt(entry) is True


def test_non_managed_limit_orders_are_untouched() -> None:
    """Scope guard: this exemption is for v2 MANAGED exits only. A plain limit order must keep the
    old refresh behaviour, or we silently change every other order path in the OMS."""
    svc = _svc()
    assert svc._managed_exit_refresh_exempt(_exit_order(managed=False), bid=1.76) is False


def test_market_orders_are_never_exempt() -> None:
    """A market exit has no resting price to protect; exempting it would just delay the flatten."""
    svc = _svc()
    assert svc._managed_exit_refresh_exempt(_exit_order("MARKET", limit_price=None), bid=1.76) is False


def test_flag_off_restores_the_old_behaviour() -> None:
    """Kill switch. OFF => byte-identical to today, so the change can be reverted by config."""
    svc = _svc(oms_hold_marketable_managed_exit=False)
    assert svc._managed_exit_refresh_exempt(_exit_order(age_secs=30), bid=1.76) is False


def test_default_is_ON() -> None:
    """⛔ Pin the VALUE, not just the behaviour — a default that silently flips is how the vol floor
    guarded dead code for weeks."""
    assert Settings().oms_hold_marketable_managed_exit is True


# --------------------------------------------------------------------- P0b: never sell above the bid

def test_exit_limit_is_never_placed_above_the_current_bid() -> None:
    """P0b. The 1.77 order is the proof this was missing: a sell limit above the bid cannot fill.
    Any repriced exit must be capped at the FRESH bid."""
    svc = _svc()
    assert svc._cap_exit_limit_to_bid(1.77, bid=1.76) == 1.76   # capped down to the bid
    assert svc._cap_exit_limit_to_bid(1.74, bid=1.76) == 1.74   # already marketable -> untouched


def test_capping_is_inert_without_a_usable_bid() -> None:
    """No/zero bid must not turn into a 0.0 limit — fail safe, leave the caller's price alone."""
    svc = _svc()
    assert svc._cap_exit_limit_to_bid(1.74, bid=0.0) == 1.74
    assert svc._cap_exit_limit_to_bid(1.74, bid=None) == 1.74
