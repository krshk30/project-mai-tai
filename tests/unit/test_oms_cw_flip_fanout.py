"""CW_FLIP FAN-OUT — the flip exit must reach the Webull leg, like every other exit reason.

⭐⭐ THE DEFECT, measured over the 7-day OMS corpus:

    CW_HARD_STOP   400 live:orb  /  241 live:schwab_1m_v2
    CW_FLOOR        47 live:orb  /    9 live:schwab_1m_v2
    CW_FLIP          0 live:orb  /    4 live:schwab_1m_v2      <- the gap

⛔ CLASS A (no owner), NOT Class B (refused). There is NO reject count, because nothing is ever
emitted: the flip is EVENT-driven and arms one account, while the stop and floor are STATE-driven
and iterate managed ROWS (so live:orb is covered for free).

COST, n=2 of 4 usable events -- the Webull leg rode the reversal until the hard-stop fallback:
    AAOG 2026-08-04   flip exit 4.2903 @08:14:01  ->  Webull 4.1911 @08:36:38   +22m37s   -2.31%
    GTE  2026-08-05   flip exit 10.0809 @09:16:05 ->  Webull 9.6027 @09:30:06   +14m01s   -4.74%
⛔ RARE AND EXPENSIVE, NOT A RUNNING COST: ~1 event every 2 days. n=2 is not a median.

⛔ ACCEPTANCE — the three known cases, all mandatory. A clean run is not acceptance:
  1  AAOG 08-04        Webull armed WITH the flip, not +22 min on the fallback
  2  GTE  08-05 09:16  same, not +14 min
  3  GTE  08-05 16:24  Webull had ALREADY closed (16:01:32) => nothing emitted at all
  4  the symmetric case: Schwab closed, Webull still open => the open leg still arms
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

SCHWAB = "live:schwab_1m_v2"
ORB = "live:orb"


def _svc(fanout: bool = True) -> OmsRiskService:
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.settings = Settings(
        strategy_schwab_1m_v2_account_name=SCHWAB,
        # ⛔ `_v2_accounts()` appends the Webull leg only when BOTH the flag is on AND this name is
        # set (it defaults to ""). Omitting it silently yields a Schwab-only list — which is the
        # correct production behaviour, and would have made these tests pass for the wrong reason.
        strategy_schwab_1m_v2_webull_account_name=ORB,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=fanout,
        # ⛔ Defaults FALSE, and the whole exit evaluator returns immediately without it -- so C3/C4
        # would have "passed" by never running the code they exist to test. Verified TRUE in the
        # live env (MAI_TAI_OMS_V2_EXIT_MANAGEMENT_ENABLED=true) before setting it here, so the
        # tests exercise the production configuration rather than a convenient one.
        oms_v2_exit_management_enabled=True,
    )
    svc._cw_flip_pending = set()
    svc._cw_exit_enabled = True
    svc.logger = SimpleNamespace(
        _lines=[],
        info=lambda m, *a: svc.logger._lines.append(m % a),
        warning=lambda m, *a: svc.logger._lines.append(m % a),
        error=lambda m, *a: svc.logger._lines.append(m % a),
    )
    return svc


def _flip(svc, symbol: str, account: str = SCHWAB):
    """Drive the REAL stream handler, not a helper — the arm site is what changed, and a helper
    would let the handler's own wiring drift away from what the test proves."""
    asyncio.run(
        svc._handle_stream_message(
            {
                "data": json.dumps(
                    {
                        "event_type": "v2_cw_flip",
                        "symbol": symbol,
                        "broker_account_name": account,
                        "bar_time_ms": "1786048080000",
                    }
                )
            }
        )
    )


# ------------------------------------------------------- criteria 1 & 2: the leg gets armed

def test_C1_AAOG_the_webull_leg_is_armed_WITH_the_flip() -> None:
    """AAOG 2026-08-04. Before this change the Webull leg was never told, and the CW_HARD_STOP
    fallback caught it 22m37s later at -2.31%."""
    svc = _svc()
    _flip(svc, "AAOG")
    assert (SCHWAB, "AAOG") in svc._cw_flip_pending
    assert (ORB, "AAOG") in svc._cw_flip_pending, (
        "the Webull leg was not armed — it will ride the reversal to the hard-stop fallback"
    )


def test_C2_GTE_the_webull_leg_is_armed_WITH_the_flip() -> None:
    """GTE 2026-08-05 09:16. Fallback caught it 14m01s later at -4.74%."""
    svc = _svc()
    _flip(svc, "GTE")
    assert {(SCHWAB, "GTE"), (ORB, "GTE")} <= svc._cw_flip_pending


def test_both_accounts_are_logged_so_the_fan_out_is_visible_on_the_tape() -> None:
    svc = _svc()
    _flip(svc, "GTE")
    armed = [ln for ln in svc.logger._lines if "flip pending armed" in ln]
    assert len(armed) == 2
    assert any(ORB in ln for ln in armed) and any(SCHWAB in ln for ln in armed)


# ------------------------------------------------------- the self-gating property

def test_flag_OFF_is_byte_identical_to_the_old_behaviour() -> None:
    """⭐ This needs no flag of its own: `_v2_accounts()` already collapses to Schwab-only when the
    fan-out flag is off. That is what makes the change CONSISTENCY rather than a new rule."""
    svc = _svc(fanout=False)
    _flip(svc, "AAOG")
    assert svc._cw_flip_pending == {(SCHWAB, "AAOG")}


def test_an_unexpected_publisher_account_is_still_armed_not_silently_dropped() -> None:
    """Superset, never a substitution: if the publisher ever names an account outside
    `_v2_accounts()`, we must not silently ignore it. An unmanaged pair self-discards downstream."""
    svc = _svc()
    _flip(svc, "AAOG", account="live:some_other")
    assert ("live:some_other", "AAOG") in svc._cw_flip_pending
    assert (ORB, "AAOG") in svc._cw_flip_pending


def test_cw_disabled_arms_nothing() -> None:
    svc = _svc()
    svc._cw_exit_enabled = False
    _flip(svc, "AAOG")
    assert svc._cw_flip_pending == set()


# ------------------------------------------------------- criteria 3 & 4: the phantom guard

def _drive_exit(svc, acct: str, symbol: str, snapshot):
    """Drive `_maybe_emit_v2_managed_exit` far enough to exercise the no-open-row branch."""
    svc._managed_v2_symbols = {(acct, symbol)}
    svc._cw_floor_armed = set()
    svc._latest_quotes_by_symbol = {symbol: {"bid": 9.5}}
    svc.emitted = []

    async def _run_db(fn, commit=False):
        return snapshot

    async def _emit(*a, **k):
        svc.emitted.append((a, k))

    svc._run_db = _run_db
    svc._emit_v2_exit_on_loop = _emit
    asyncio.run(svc._evaluate_v2_managed_exit(acct, symbol))


def test_C3_GTE_1624_webull_ALREADY_CLOSED_emits_NOTHING() -> None:
    """⛔⭐ THE PHANTOM-EXIT CASE, AND IT IS ALREADY INSTANCED. GTE 2026-08-05: the Webull leg closed
    at 16:01:32 on its own OCO; the Schwab flip fired at 16:24. Under the fan-out that flip now arms
    live:orb for a symbol with NO open position. Nothing may be emitted, and the stale arm must be
    dropped."""
    svc = _svc()
    _flip(svc, "GTE")
    assert (ORB, "GTE") in svc._cw_flip_pending          # armed by the fan-out
    _drive_exit(svc, ORB, "GTE", snapshot=None)          # ...but no open managed row
    assert svc.emitted == [], "a phantom sell was emitted for a leg that had already closed"
    assert (ORB, "GTE") not in svc._cw_flip_pending, "the stale flip was not dropped"
    assert (ORB, "GTE") not in svc._managed_v2_symbols


def test_C4_symmetric_schwab_closed_webull_OPEN_the_open_leg_stays_armed() -> None:
    """The mirror of C3 and the more common shape once the fan-out works: the Schwab leg has gone
    but the Webull leg is still open. Dropping the Schwab arm must not disturb the Webull one."""
    svc = _svc()
    _flip(svc, "GTE")
    _drive_exit(svc, SCHWAB, "GTE", snapshot=None)       # Schwab leg already flat
    assert (SCHWAB, "GTE") not in svc._cw_flip_pending
    assert (ORB, "GTE") in svc._cw_flip_pending, (
        "dropping the closed leg's arm also dropped the OPEN leg's — the open leg would never flip"
    )
