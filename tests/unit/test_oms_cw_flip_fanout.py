"""CW_FLIP FAN-OUT — the flip exit must reach the Webull leg, like every other exit reason.

⭐⭐ THE DEFECT, measured over the 7-day OMS corpus:

    CW_HARD_STOP   400 live:orb  /  241 live:schwab_1m_v2
    CW_FLOOR        47 live:orb  /    9 live:schwab_1m_v2
    CW_FLIP          0 live:orb  /    4 live:schwab_1m_v2      <- the gap

The replacement signal is account-neutral: the OMS evaluates every configured account and binds
only rows that were open when the transition bar closed. No publisher-owned account can narrow it.

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
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from project_mai_tai.oms.service import OmsRiskService, _CWFlipBinding
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
    svc._cw_flip_decisions = {}
    svc._cw_flip_missing_bound_accounts = set()
    svc._cw_flip_unanswerable_accounts = set()
    svc._cw_exit_enabled = True
    svc.logger = SimpleNamespace(
        _lines=[],
        info=lambda m, *a: svc.logger._lines.append(m % a),
        warning=lambda m, *a: svc.logger._lines.append(m % a),
        error=lambda m, *a: svc.logger._lines.append(m % a),
    )

    async def _bound(acct: str, symbol: str) -> _CWFlipBinding:
        if acct in svc._cw_flip_unanswerable_accounts:
            return _CWFlipBinding(status="unanswerable")
        if acct in svc._cw_flip_missing_bound_accounts:
            return _CWFlipBinding(status="not_owned")
        return _CWFlipBinding(
            status="owned",
            managed_row_id=f"row:{acct}:{symbol}",
            entry_time=datetime.now(UTC) - timedelta(seconds=120),
        )

    svc._cw_flip_bound_managed_position = _bound
    return svc


def _flip(svc, symbol: str, *, extra: dict | None = None):
    """Drive the REAL stream handler, not a helper — the arm site is what changed, and a helper
    would let the handler's own wiring drift away from what the test proves."""
    bar_time_ms = int((datetime.now(UTC) - timedelta(seconds=90)).timestamp() * 1000)
    asyncio.run(
        svc._handle_stream_message(
            {
                "data": json.dumps(
                    {
                        "event_type": "v2_atr_sell_observation",
                        "symbol": symbol,
                        "bar_time_ms": str(bar_time_ms),
                        "decision_id": f"atr-sell:{symbol}:{bar_time_ms}",
                        **(extra or {}),
                    }
                )
            }
        )
    )


def _flip_at_age(svc, symbol: str, *, age_seconds: float, now: datetime) -> None:
    bar_time_ms = int((now - timedelta(seconds=age_seconds)).timestamp() * 1000)
    asyncio.run(
        svc._handle_stream_message(
            {
                "data": json.dumps(
                    {
                        "event_type": "v2_atr_sell_observation",
                        "symbol": symbol,
                        "bar_time_ms": str(bar_time_ms),
                        "decision_id": f"atr-sell:{symbol}:{bar_time_ms}",
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
    assert (
        svc._cw_flip_decisions[(SCHWAB, "AAOG")].managed_row_id
        == "row:live:schwab_1m_v2:AAOG"
    )
    assert svc._cw_flip_decisions[(ORB, "AAOG")].managed_row_id == "row:live:orb:AAOG"


def test_C2_GTE_the_webull_leg_is_armed_WITH_the_flip() -> None:
    """GTE 2026-08-05 09:16. Fallback caught it 14m01s later at -4.74%."""
    svc = _svc()
    _flip(svc, "GTE")
    assert {(SCHWAB, "GTE"), (ORB, "GTE")} <= svc._cw_flip_pending


def test_both_accounts_are_logged_so_the_fan_out_is_visible_on_the_tape() -> None:
    svc = _svc()
    _flip(svc, "GTE")
    armed = [ln for ln in svc.logger._lines if "outcome=armed" in ln]
    assert len(armed) == 2
    assert any(ORB in ln for ln in armed) and any(SCHWAB in ln for ln in armed)


# ------------------------------------------------------- the self-gating property

def test_flag_OFF_is_byte_identical_to_the_old_behaviour() -> None:
    """⭐ This needs no flag of its own: `_v2_accounts()` already collapses to Schwab-only when the
    fan-out flag is off. That is what makes the change CONSISTENCY rather than a new rule."""
    svc = _svc(fanout=False)
    _flip(svc, "AAOG")
    assert svc._cw_flip_pending == {(SCHWAB, "AAOG")}


def test_publisher_cannot_inject_an_account_into_the_account_neutral_observation() -> None:
    svc = _svc()
    _flip(svc, "AAOG", extra={"broker_account_name": "live:some_other"})
    assert ("live:some_other", "AAOG") not in svc._cw_flip_pending
    assert svc._cw_flip_pending == {(SCHWAB, "AAOG"), (ORB, "AAOG")}


def test_cw_disabled_arms_nothing() -> None:
    svc = _svc()
    svc._cw_exit_enabled = False
    _flip(svc, "AAOG")
    assert svc._cw_flip_pending == set()


def test_observation_identity_must_bind_the_symbol_and_bar() -> None:
    svc = _svc()
    _flip(svc, "YMAT", extra={"decision_id": "atr-sell:OTHER:1"})
    assert svc._cw_flip_pending == set()
    assert any("invalid_decision_identity" in line for line in svc.logger._lines)


def test_expired_observation_is_refused_before_any_account_is_armed(monkeypatch) -> None:
    now = datetime(2026, 9, 9, 15, 0, tzinfo=UTC)
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: now)
    svc = _svc()

    _flip_at_age(svc, "YMAT", age_seconds=181.0, now=now)

    assert svc._cw_flip_pending == set()
    assert any("reason=invalid_or_expired_bar" in line for line in svc.logger._lines)


def test_observation_inside_expiry_still_arms_each_owned_account(monkeypatch) -> None:
    now = datetime(2026, 9, 9, 15, 0, tzinfo=UTC)
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: now)
    svc = _svc()

    async def _owned_before_bar(acct: str, symbol: str) -> _CWFlipBinding:
        return _CWFlipBinding(
            status="owned",
            managed_row_id=f"row:{acct}:{symbol}",
            entry_time=now - timedelta(minutes=10),
        )

    svc._cw_flip_bound_managed_position = _owned_before_bar

    _flip_at_age(svc, "YMAT", age_seconds=120.0, now=now)

    assert svc._cw_flip_pending == {(SCHWAB, "YMAT"), (ORB, "YMAT")}
    assert not any("invalid_or_expired_bar" in line for line in svc.logger._lines)


def test_retired_v2_cw_flip_event_arms_nothing() -> None:
    svc = _svc()
    bar_time_ms = int((datetime.now(UTC) - timedelta(seconds=90)).timestamp() * 1000)
    asyncio.run(
        svc._handle_stream_message(
            {
                "data": json.dumps(
                    {
                        "event_type": "v2_cw_flip",
                        "symbol": "YMAT",
                        "broker_account_name": SCHWAB,
                        "bar_time_ms": bar_time_ms,
                    }
                )
            }
        )
    )
    assert svc._cw_flip_pending == set()


# ------------------------------------------------------- criteria 3 & 4: bind only open legs


def test_C3_GTE_1624_webull_ALREADY_CLOSED_emits_NOTHING() -> None:
    """⛔⭐ THE PHANTOM-EXIT CASE, AND IT IS ALREADY INSTANCED. GTE 2026-08-05: the Webull leg closed
    at 16:01:32 on its own OCO; the Schwab flip fired at 16:24. Under the fan-out that flip now arms
    only a leg with an open managed position. An ownerless decision is refused at acceptance, not
    left pending in the hope that a later quote will clear it."""
    svc = _svc()
    svc._cw_flip_missing_bound_accounts.add(ORB)
    _flip(svc, "GTE")
    assert (ORB, "GTE") not in svc._cw_flip_pending
    assert (SCHWAB, "GTE") in svc._cw_flip_pending
    assert any("outcome=not_owned" in line for line in svc.logger._lines)


def test_C4_symmetric_schwab_closed_webull_OPEN_the_open_leg_stays_armed() -> None:
    """YMAT 2026-09-09: Webull-only ownership still arms Webull and never arms Schwab."""
    svc = _svc()
    svc._cw_flip_missing_bound_accounts.add(SCHWAB)
    _flip(svc, "YMAT")
    assert (SCHWAB, "YMAT") not in svc._cw_flip_pending
    assert (ORB, "YMAT") in svc._cw_flip_pending, (
        "refusing the closed leg's arm also dropped the OPEN leg's — the open leg would never flip"
    )


def test_one_accounts_unanswerable_read_does_not_block_the_other_account() -> None:
    svc = _svc()
    svc._cw_flip_unanswerable_accounts.add(SCHWAB)
    _flip(svc, "YMAT")
    assert (SCHWAB, "YMAT") not in svc._cw_flip_pending
    assert (ORB, "YMAT") in svc._cw_flip_pending
    summary = next(line for line in svc.logger._lines if "CW-FLIP-EVALUATED" in line)
    assert "accounts_evaluated=2" in summary
    assert "owned=1" in summary
    assert "armed=1" in summary
    assert "refused=1" in summary
    assert "unanswerable=1" in summary
