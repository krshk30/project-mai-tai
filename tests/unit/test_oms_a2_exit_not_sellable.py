"""A2 — the broker refuses a sell on a position we HOLD.

⭐⭐ THE INCIDENT CLASS. `ORDER_NOT_SUPPORT_REVERSE_OPTION` on Webull, 394 rejects over 14 days,
16 episodes. AAOG 2026-08-04: **313 attempts in 816 s -- one every 2.6 s, every one rejected.**
The blocker is broker-side ACCOUNT STATE, not price and not our limit, so faster retrying provably
cannot help. Anyone reading the reject count will reach for retry tuning; these tests exist partly
to make that impossible to do quietly.

⛔ ACCEPTANCE (docs/v2-a2-reverse-reject-design.md §7), stated BEFORE the code:
  A2  the never-cleared tail is CLOSED -- ERNA 07-15 and AGEN 07-13 never filled at all, and a
      design that improves the median while leaving them is NOT a fix. They must reach a terminal
      state: a fill, or a PAGE.
  A3  reject volume falls WITHOUT trigger->fill getting worse. A defer that hides rejects and
      lengthens exposure FAILS.
  A4  a block that RESOLVES inside the bound is never escalated.
  A5  the managed row stays OPEN throughout a backoff (Ship 2 must stay green).
  A6  the classifier catches BOTH brokers' strings.

⛔ The bound is 90 s by OPERATOR RISK DECISION (2026-08-06), not a derived value: it sits in the
bimodal gap where every bound from ~90 s to ~250 s escalates on the same 7 of 11.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from project_mai_tai.oms.service import OmsRiskService, _PositionRead, utcnow
from project_mai_tai.settings import Settings

ORB = "live:orb"
SCHWAB = "live:schwab_1m_v2"

# The REAL reject strings, captured from broker_order_events. Not invented.
WEBULL_REVERSE = "Webull order rejected: ORDER_NOT_SUPPORT_REVERSE_OPTION ORDER_NOT_SUPPORT_REVERSE_OPTION (http 417)"
SCHWAB_OVERSOLD = "This order may result in an oversold/overbought position in your account."


def _svc(enabled: bool = True) -> OmsRiskService:
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.settings = Settings(oms_a2_exit_not_sellable_backoff_enabled=enabled)
    svc.logger = SimpleNamespace(
        _lines=[],
        info=lambda m, *a: svc.logger._lines.append(("info", m % a)),
        warning=lambda m, *a: svc.logger._lines.append(("warning", m % a)),
        error=lambda m, *a: svc.logger._lines.append(("error", m % a)),
    )
    svc._a2_not_sellable_since = {}
    svc._a2_last_probe = {}
    svc._a2_escalated = set()
    return svc


def _errors(svc) -> list[str]:
    return [m for lvl, m in svc.logger._lines if lvl == "error"]


# ------------------------------------------------------------------ A6: the classifier

def test_A6_classifier_catches_BOTH_brokers_strings() -> None:
    """⛔ Keying on one broker's text is how 394 Webull rejects stayed invisible in Schwab-keyed
    queries for 14 days. One condition, two strings."""
    assert OmsRiskService._is_exit_refused_not_sellable(WEBULL_REVERSE) is True
    assert OmsRiskService._is_exit_refused_not_sellable(SCHWAB_OVERSOLD) is True


def test_A6_classifier_matches_screaming_snake_and_free_text_forms() -> None:
    assert OmsRiskService._is_exit_refused_not_sellable("ORDER_NOT_SUPPORT_REVERSE_OPTION") is True
    assert OmsRiskService._is_exit_refused_not_sellable("order not support reverse option") is True
    assert OmsRiskService._is_exit_refused_not_sellable("OVERSOLD") is True


def test_each_configured_substring_is_INDEPENDENTLY_load_bearing() -> None:
    """⛔⭐ FOUND BY MUTATION, 2026-08-06. Deleting `"oversold"` from the tuple left
    `test_A6_classifier_catches_BOTH_brokers_strings` GREEN -- because Schwab's real message happens
    to contain BOTH "oversold" and "overbought", so one covers for the other. The test looked like
    it pinned two-broker coverage and actually pinned only one marker.

    These strings isolate each substring so no single deletion can hide behind a sibling."""
    assert OmsRiskService._is_exit_refused_not_sellable("position would be oversold") is True
    assert OmsRiskService._is_exit_refused_not_sellable("position would be overbought") is True
    assert OmsRiskService._is_exit_refused_not_sellable("ORDER_NOT_SUPPORT_REVERSE_OPTION") is True


def test_classifier_does_NOT_match_unrelated_rejects() -> None:
    """⛔ A false positive here defers a REAL exit. These are other live reject classes."""
    for other in (
        "Opening transactions for this security must be placed with a broker. Contact us",
        "Webull order rejected: missing Webull App Key/App Secret",
        "429 too many requests",
        "NO_SUCH_TICKER",
        "insufficient buying power",
        "",
        None,
    ):
        assert OmsRiskService._is_exit_refused_not_sellable(other) is False, other


# ------------------------------------------------------------------ scope + flag

def test_default_OFF_and_scoped_to_live_orb() -> None:
    """⛔ Behaviour is confined to the account where the class was MEASURED. Schwab's oversold
    population belongs to D1 / slice C, which are separately analysed and parked."""
    assert _svc(enabled=False)._a2_enabled_for(ORB) is False      # flag off -> inert
    assert _svc(enabled=True)._a2_enabled_for(ORB) is True
    assert _svc(enabled=True)._a2_enabled_for(SCHWAB) is False    # scoped, even when enabled


def test_defer_is_inert_when_disabled() -> None:
    svc = _svc(enabled=False)
    svc._a2_note_reject(ORB, "ZYBT")
    assert svc._a2_should_defer(ORB, "ZYBT") is False


# ------------------------------------------------------------------ A3 / A5: the backoff

def test_A3_backs_off_within_the_interval_then_probes_again() -> None:
    """⭐ A probe, NOT suppression. The block can clear at any second (30 s at the low end), so we
    must keep testing -- otherwise we trade the reject burn for a MISSED exit, the same bug facing
    the other way. That is what makes this pass A3 rather than merely reduce reject count."""
    svc = _svc()
    svc._a2_note_reject(ORB, "ZYBT")
    assert svc._a2_should_defer(ORB, "ZYBT") is True, "must back off immediately after a refusal"

    svc._a2_last_probe[(ORB, "ZYBT")] = utcnow() - timedelta(
        seconds=svc._A2_BACKOFF_SECONDS + 1
    )
    assert svc._a2_should_defer(ORB, "ZYBT") is False, "must probe again once the interval elapses"


def test_A5_backoff_never_touches_the_managed_row_or_the_guard_set() -> None:
    """⛔ Back-off is NOT abandonment. If a backoff dropped the managed row, Ship 2 would see an
    unowned position and correctly page -- and the position would be genuinely unprotected."""
    svc = _svc()
    svc._managed_v2_symbols = {(ORB, "ZYBT")}
    svc._a2_note_reject(ORB, "ZYBT")
    svc._a2_should_defer(ORB, "ZYBT")
    assert (ORB, "ZYBT") in svc._managed_v2_symbols


# ------------------------------------------------------------------ A4 / A2: escalation

def _escalate(svc, acct, symbol, state):
    async def _state(_a, _s):
        return state
    svc._broker_symbol_position_state = _state
    asyncio.run(svc._a2_maybe_escalate(acct, symbol))


def test_A4_a_block_that_resolves_INSIDE_the_bound_is_never_escalated() -> None:
    """The harmless half -- a live broker bracket reserving the shares -- self-resolves to flat.
    We cannot SEE that it was a bracket, so we gate on the OUTCOME instead of the cause."""
    svc = _svc()
    svc._a2_note_reject(ORB, "YXT")
    _escalate(svc, ORB, "YXT", _PositionRead.HELD)          # only seconds in
    assert _errors(svc) == [], "must not page before the bound"

    # ...and if it then goes flat, the episode is forgotten with no page ever.
    svc._a2_not_sellable_since[(ORB, "YXT")] = utcnow() - timedelta(seconds=600)
    _escalate(svc, ORB, "YXT", _PositionRead.FLAT_CONFIRMED)
    assert _errors(svc) == []
    assert (ORB, "YXT") not in svc._a2_not_sellable_since


def test_A2_ACCEPTANCE_still_held_at_the_bound_reaches_a_terminal_state() -> None:
    """⛔⭐ THE ACCEPTANCE TEST. ERNA 07-15 and AGEN 07-13 NEVER FILLED AT ALL. A design that
    improves the median and leaves them is not a fix. Under A2 they reach a terminal state -- a
    PAGE -- instead of silently never resolving."""
    svc = _svc()
    svc._a2_note_reject(ORB, "ERNA")
    svc._a2_not_sellable_since[(ORB, "ERNA")] = utcnow() - timedelta(
        seconds=svc._A2_ESCALATE_AFTER_SECONDS + 1
    )
    _escalate(svc, ORB, "ERNA", _PositionRead.HELD)
    errs = _errors(svc)
    assert len(errs) == 1 and "OMS-A2-EXIT-BLOCKED" in errs[0]
    assert "ERNA" in errs[0]


def test_A2_pages_exactly_once_per_episode() -> None:
    """A page every 15 s would be muted inside a week, and a muted pager is worse than none."""
    svc = _svc()
    svc._a2_note_reject(ORB, "AGEN")
    svc._a2_not_sellable_since[(ORB, "AGEN")] = utcnow() - timedelta(seconds=300)
    for _ in range(5):
        _escalate(svc, ORB, "AGEN", _PositionRead.HELD)
    assert len(_errors(svc)) == 1


def test_UNKNOWN_is_treated_as_still_held_and_DOES_page() -> None:
    """⛔ THE #608 COLLAPSE GUARD. Reading UNKNOWN as flat is what let NCRA retry 145 times. For a
    PAGE the ambiguity resolves toward escalating: a false page is noise, a missed page is a
    position nobody knows is stuck."""
    svc = _svc()
    svc._a2_note_reject(ORB, "KUST")
    svc._a2_not_sellable_since[(ORB, "KUST")] = utcnow() - timedelta(seconds=300)
    _escalate(svc, ORB, "KUST", _PositionRead.UNKNOWN)
    assert len(_errors(svc)) == 1


def test_FLAT_INFERRED_does_NOT_cancel_the_page() -> None:
    """FLAT_INFERRED means 'absent from the read' -- a genuine close and a silently-failed read
    produce it IDENTICALLY (the ERNA lesson). Only FLAT_CONFIRMED is positive enough to cancel."""
    svc = _svc()
    svc._a2_note_reject(ORB, "FCUV")
    svc._a2_not_sellable_since[(ORB, "FCUV")] = utcnow() - timedelta(seconds=300)
    _escalate(svc, ORB, "FCUV", _PositionRead.FLAT_INFERRED)
    assert len(_errors(svc)) == 1


def test_a_broker_read_that_RAISES_still_pages() -> None:
    svc = _svc()
    svc._a2_note_reject(ORB, "CNET")
    svc._a2_not_sellable_since[(ORB, "CNET")] = utcnow() - timedelta(seconds=300)

    async def _boom(_a, _s):
        raise RuntimeError("broker unreachable")

    svc._broker_symbol_position_state = _boom
    asyncio.run(svc._a2_maybe_escalate(ORB, "CNET"))
    assert len(_errors(svc)) == 1


# ------------------------------------------------------------------ the episode clock

def test_the_episode_START_survives_repeated_refusals() -> None:
    """⛔⭐ THE CORE INVARIANT. #608's counter RESETS on a HELD read, which is precisely why the
    existing abandon bound is unreachable during a jam -- the symbol IS held, so it resets forever.
    A2's clock must measure the EPISODE, not the gap between two rejects. If `_a2_note_reject`
    moved the start, the 90 s bound would never be reached and the acceptance test above would be
    unreachable in production while still passing in isolation."""
    svc = _svc()
    svc._a2_note_reject(ORB, "AAOG")
    started = svc._a2_not_sellable_since[(ORB, "AAOG")]
    for _ in range(313):                       # AAOG's real attempt count
        svc._a2_note_reject(ORB, "AAOG")
    assert svc._a2_not_sellable_since[(ORB, "AAOG")] == started


def test_the_HELD_branch_of_reconcile_flat_MUST_NOT_clear_the_episode() -> None:
    """⛔⭐⭐ THE INVARIANT THAT MAKES THE BOUND REACHABLE AT ALL — and it had NO test until a
    self-review caught the gap.

    `_v2_close_reconcile_flat` RESETS `_v2_exit_close_failures` to 0 on every positively-HELD read
    (#608, correctly: we do hold it, so keep managing). That is exactly why the existing
    `_V2_EXIT_ABANDON_AFTER_FAILURES` bound is UNREACHABLE during a jam — the symbol is genuinely
    held, so the counter resets forever.

    A2's clock must measure the EPISODE, not the gap between rejects. If anyone later adds an
    `_a2_clear` to that HELD branch, the 90 s bound silently becomes unreachable in production
    while every other test in this file still passes. This is the only test that would catch it."""
    svc = _svc()
    svc._v2_exit_close_failures = {}
    svc._v2_exit_stood_down = set()
    svc._V2_EXIT_RECONCILE_AFTER_FAILURES = 1

    async def _held_state(_a, _s):
        return _PositionRead.HELD

    async def _not_flat(*_a, **_k):
        return False

    svc._broker_symbol_position_state = _held_state
    svc._broker_symbol_is_flat = _not_flat

    started = utcnow() - timedelta(seconds=45)
    svc._a2_not_sellable_since[(ORB, "AAOG")] = started
    row = SimpleNamespace(entry_time=utcnow() - timedelta(hours=1))

    for _ in range(20):        # twenty more refusals while genuinely held
        assert asyncio.run(svc._v2_close_reconcile_flat(None, ORB, "AAOG", row)) is False

    assert svc._v2_exit_close_failures[(ORB, "AAOG")] == 0, "the #608 reset must be preserved"
    assert svc._a2_not_sellable_since[(ORB, "AAOG")] == started, (
        "the A2 episode clock was reset by the HELD branch — the 90s bound is now unreachable"
    )


def test_clear_forgets_the_episode_so_the_next_position_is_unaffected() -> None:
    """A stale episode would defer the NEXT position on this symbol -- the same reason the #608
    stand-down MUST clear on the recovery path."""
    svc = _svc()
    svc._a2_note_reject(ORB, "ZYBT")
    svc._a2_escalated.add((ORB, "ZYBT"))
    svc._a2_clear(ORB, "ZYBT")
    assert (ORB, "ZYBT") not in svc._a2_not_sellable_since
    assert (ORB, "ZYBT") not in svc._a2_last_probe
    assert (ORB, "ZYBT") not in svc._a2_escalated
    assert svc._a2_should_defer(ORB, "ZYBT") is False
