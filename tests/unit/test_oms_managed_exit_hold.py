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


# ------------------------------------------------- P0a OBSERVABILITY (2026-08-04)
# ⭐ WHY THIS EXISTS. For its first four days the hold branch was a bare `pass`, so engaging it
# left NO trace at all. P0a sat "deployed-not-validated" partly because there was nothing to look
# for: a watch could only infer the hold from the ABSENCE of cancel/replace lines, and inferring
# health from an absence is exactly how a broken watch reports a false clean.
# [[feedback_a_watch_that_fails_to_a_false_clean]]

class _CapturingLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, msg, *args) -> None:
        self.lines.append(msg % args if args else msg)

    warning = exception = debug = info


def _svc_with_logger(**over):
    svc = _svc(**over)
    svc.logger = _CapturingLogger()
    return svc


def _held_order(order_id: str = "o1", limit_price: float = KUST_EXIT_LIMIT, session: str = "AM"):
    o = _exit_order(age_secs=30, limit_price=limit_price)
    o.id = order_id
    o.symbol = "KUST"
    o.payload["session"] = session
    return o


def test_hold_logs_once_per_order_not_once_per_refresh_tick() -> None:
    """⛔ EDGE-triggered, not level-triggered. The refresh re-evaluates every working order every
    ~5s; a per-tick line would be ~12/min/order and drown its own signal (the REVISE-STORM shape).
    Three evaluations of the same still-held order must produce exactly ONE line."""
    svc = _svc_with_logger()
    order = _held_order()
    for _ in range(3):
        svc._log_p0a_hold_edge(order, bid=1.76)
    engaged = [ln for ln in svc.logger.lines if "[OMS-P0A-HOLD]" in ln]
    assert len(engaged) == 1, f"expected exactly one engage line, got {len(engaged)}"
    assert "ENGAGED" in engaged[0] and "KUST" in engaged[0]


def test_release_reports_how_long_the_hold_actually_held() -> None:
    """The DURATION is the evidence. 'Rested through a refresh then filled' is the P0a pass
    condition, and it cannot be scored without knowing how long the order sat."""
    svc = _svc_with_logger()
    order = _held_order()
    svc._log_p0a_hold_edge(order, bid=1.76)
    svc._log_p0a_hold_release(order, bid=1.70)
    released = [ln for ln in svc.logger.lines if "[OMS-P0A-HOLD-RELEASED]" in ln]
    assert len(released) == 1
    assert "held" in released[0] and "s then released" in released[0]


def test_release_without_a_prior_engage_is_silent() -> None:
    """An order that was never held must not manufacture a release line — a spurious RELEASED
    would read as 'the hold engaged and gave up', inventing an incident that never happened."""
    svc = _svc_with_logger()
    svc._log_p0a_hold_release(_held_order("never-held"), bid=1.70)
    assert not any("P0A-HOLD-RELEASED" in ln for ln in svc.logger.lines)


def test_re_engage_after_a_release_logs_again() -> None:
    """A genuinely NEW hold on the same order (bid recovered above the limit) is a new event and
    must be visible. Otherwise a single flapping order would report only its first hold."""
    svc = _svc_with_logger()
    order = _held_order()
    svc._log_p0a_hold_edge(order, bid=1.76)
    svc._log_p0a_hold_release(order, bid=1.70)
    svc._log_p0a_hold_edge(order, bid=1.76)
    assert len([ln for ln in svc.logger.lines if "[OMS-P0A-HOLD]" in ln]) == 2


def test_logging_survives_a_service_without_the_tracking_dict() -> None:
    """__new__-constructed instances lack _p0a_held_orders. A LOG-ONLY attribute must never raise
    on the working-order path — the refresh loop touches real money."""
    svc = _svc_with_logger()
    assert "_p0a_held_orders" not in svc.__dict__
    svc._log_p0a_hold_edge(_held_order(), bid=1.76)   # must not raise
    assert any("[OMS-P0A-HOLD]" in ln for ln in svc.logger.lines)


def test_the_eh_session_is_the_case_that_matters() -> None:
    """KUST was session=AM. The hold predicate has no session gating, so the EH exit qualifies on
    exactly the same terms as an RTH one — this pins that it is not accidentally RTH-only."""
    svc = _svc()
    assert svc._managed_exit_refresh_exempt(_held_order(session="AM"), bid=1.76) is True
    assert svc._managed_exit_refresh_exempt(_held_order(session="NORMAL"), bid=1.76) is True


# ----------------------------------------------------- INSTRUMENT THE NEGATIVE (2026-08-06)
#
# ⭐⭐ WHY. `[OMS-P0A-HOLD]` sat at ZERO lines from deploy (08-05 21:06 ET) onward, and zero is
# ambiguous between "no managed exit ever qualified" and "the branch never runs". Those are
# completely different worlds and we could not tell them apart, which is a large part of why P0a
# stayed deployed-not-validated. The census makes the negative legible.
#
# ⛔ `_p0a_decline_reason` DUPLICATES the predicate's structure, so the two CAN drift. The pairing
# test below is what stops that, and it is the reason the duplication is acceptable at all.


def test_p0a_decline_reason_matches_predicate() -> None:
    """⛔⭐ THE PIN. `_p0a_decline_reason(...) is None` must equal `_managed_exit_refresh_exempt(...)`
    for every input. Two functions answering one question is the bug class we keep hitting
    (`feedback_authoritative_for_a_is_not_for_b`); this test is what keeps the pair honest.

    Edit either function without the other and this goes red."""
    svc = _svc()
    cases = [
        (_exit_order(), 1.76),                                   # marketable -> exempt
        (_exit_order(), 1.70),                                   # bid below limit
        (_exit_order(), None),                                   # no bid
        (_exit_order(), 0.0),                                    # zero bid
        (_exit_order(order_type="MARKET"), 1.76),                # not a LIMIT
        (_exit_order(managed=False), 1.76),                      # not a managed exit
        (_exit_order(limit_price=None), 1.76),                   # no limit price
        (_exit_order(limit_price=0.0), 1.76),                    # zero limit price
        (_exit_order(), KUST_EXIT_LIMIT),                        # limit == bid, still marketable
    ]
    for order, bid in cases:
        exempt = svc._managed_exit_refresh_exempt(order, bid=bid)
        reason = svc._p0a_decline_reason(order, bid=bid)
        assert (reason is None) is exempt, (
            f"drift: exempt={exempt} but reason={reason!r} for bid={bid} payload={order.payload}"
        )


def test_p0a_decline_reason_is_specific_not_generic() -> None:
    """A census of `declined=N` with no breakdown would be the same silence in a new costume."""
    svc = _svc()
    assert svc._p0a_decline_reason(_exit_order(), bid=1.70) == "not_marketable"
    assert svc._p0a_decline_reason(_exit_order(), bid=None) == "no_bid"
    assert svc._p0a_decline_reason(_exit_order(order_type="MARKET"), bid=1.76) == "not_limit"
    assert svc._p0a_decline_reason(_exit_order(managed=False), bid=1.76) == "not_managed_exit"
    assert svc._p0a_decline_reason(_exit_order(limit_price=None), bid=1.76) == "no_limit_price"
    assert svc._p0a_decline_reason(_exit_order(), bid=1.76) is None


def test_p0a_decline_reason_reports_flag_off_rather_than_a_false_negative() -> None:
    """⛔ With the flag off the predicate returns False for a reason that has nothing to do with the
    market. Reading that as 'the exit was not marketable' would be a wrong conclusion drawn from a
    correct number — the exact failure this census exists to prevent."""
    svc = _svc(oms_hold_marketable_managed_exit=False)
    assert svc._managed_exit_refresh_exempt(_exit_order(), bid=1.76) is False
    assert svc._p0a_decline_reason(_exit_order(), bid=1.76) == "flag_off"


def test_p0a_census_emits_even_when_nothing_was_evaluated() -> None:
    """⭐⭐ THE WHOLE POINT. A census that only speaks when it has something to say rebuilds the
    silence it exists to cure. `evaluated=0` is a RESULT and must reach the tape."""
    svc = _svc()
    lines: list[str] = []
    svc.logger = SimpleNamespace(info=lambda msg, *a: lines.append(msg % a))
    svc._p0a_census = {}
    svc._p0a_census_last_emit = None

    svc._maybe_emit_p0a_census()

    assert len(lines) == 1, "a quiet window must still emit a census line"
    assert "evaluated=0" in lines[0]
    assert "held=0" in lines[0]


def test_p0a_census_counts_and_breaks_down_by_reason() -> None:
    svc = _svc()
    lines: list[str] = []
    svc.logger = SimpleNamespace(info=lambda msg, *a: lines.append(msg % a))
    svc._p0a_census = {"held": 2, "not_marketable": 5, "no_bid": 1}
    svc._p0a_census_last_emit = None

    svc._maybe_emit_p0a_census()

    assert "evaluated=8" in lines[0]
    assert "held=2" in lines[0]
    assert "no_bid=1" in lines[0]
    assert "not_marketable=5" in lines[0]
    assert svc._p0a_census == {}, "the window must reset after emitting"


def test_p0a_census_respects_its_interval() -> None:
    """It sits on the ~15s order sync; emitting every pass would be the trade-coach retry-storm
    shape (45% CPU while nominally disabled)."""
    svc = _svc()
    lines: list[str] = []
    svc.logger = SimpleNamespace(info=lambda msg, *a: lines.append(msg % a))
    svc._p0a_census = {}
    svc._p0a_census_last_emit = None

    svc._maybe_emit_p0a_census()
    svc._maybe_emit_p0a_census()
    svc._maybe_emit_p0a_census()

    assert len(lines) == 1, "only the first call in the interval may emit"
