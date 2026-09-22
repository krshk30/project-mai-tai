"""#4 (operator 2026-09-22): CW_HARD_STOP and CW_FLOOR on Webull run on the SHARED cancel-then-sell path.

The named case is YMAT 2026-09-09 08:41:53 ET, pre-market, hard-stop level 1.8492: three refused
closes inside one second, then nothing — the share stayed open 78 minutes and left by CW_FLOOR at
1.95 by luck. The old path (`_emit_v2_exit_on_loop` straight from the ladder) had no ending for a
refused sell. The shared routine (#1028) ends every run sold / resolved / flat / re-protected /
paged. These tests drive the REAL ladder (`_evaluate_v2_managed_exit`) with the fanout harness'
Webull row and a broker that answers like Webull did.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import SystemIncident
from project_mai_tai.oms import service as service_module
from tests.unit.test_confirmation_exit_fanout import (
    SCHWAB,
    SYMBOL,
    WEBULL,
    _CapturedLogger,
    _FanoutAdapter,
    _sell_accounts,
    _service,
)


def _quote(service, acct_symbol_bid: float) -> None:
    service._latest_quotes_by_symbol[SYMBOL] = {
        "bid": acct_symbol_bid,
        "ask": acct_symbol_bid + 0.01,
        "received_at": service_module.utcnow(),
    }


def _cw(service) -> None:
    # the confirmed-window ladder: +2% target / -5% stop, floor off (production defaults)
    service._cw_exit_enabled = True
    service._cw_target_pct = 2.0
    service._cw_stop_pct = 5.0
    service._cw_floor_pct = 1.0
    service._cw_floor_exit_enabled = False


def _incidents(sf) -> list[dict]:
    with sf() as session:
        rows = session.scalars(select(SystemIncident).order_by(SystemIncident.opened_at)).all()
        return [dict(row.payload or {}, title=row.title) for row in rows]


@pytest.fixture(autouse=True)
def _regular_session(monkeypatch):
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)


@pytest.mark.asyncio
async def test_hard_stop_with_no_resting_pair_sells_through_the_shared_path(monkeypatch) -> None:
    # Bracket released earlier (handle known, pair gone) - the post-release shape of GLND 09-21.
    service, sf = _service(fanout=True, adapter=_FanoutAdapter())
    service.logger = _CapturedLogger()
    _cw(service)
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    _quote(service, 9.40)  # entry 10 -> stop 9.50

    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    assert _sell_accounts(sf) == [WEBULL]
    lines = "\n".join(service.logger.lines)
    assert "[OMS-WEBULL-CANCEL-THEN-SELL] exit=CW_HARD_STOP" in lines
    assert "outcome=close_submitted" in lines or "outcome=closed" in lines
    with sf() as session:
        row = service.store.get_open_managed_position(
            session, broker_account_name=WEBULL, symbol=SYMBOL
        )
    assert row is None or row.status != "open" or (WEBULL, SYMBOL) not in service._webull_cw_exit_inflight


@pytest.mark.asyncio
async def test_YMAT_pre_market_hard_stop_refused_ends_paged_not_silent(monkeypatch) -> None:
    # YMAT 2026-09-09: no pair can rest pre-market; the stop's sell was refused three times in one
    # second and then NOTHING happened for 78 minutes. Same shape: no pair, Webull refuses the sell.
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: False)
    adapter = _FanoutAdapter(reject_accounts={WEBULL})
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    _cw(service)
    service._webull_protect_base.pop((WEBULL, SYMBOL), None)  # never protected (pre-market)

    async def _held(*args, **kwargs):
        return service_module._PositionRead.HELD

    monkeypatch.setattr(service, "_broker_symbol_position_state", _held)
    _quote(service, 9.40)

    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    for task in list(service.__dict__.get("_confirmation_exit_recovery_tasks", set())):
        await task

    lines = "\n".join(service.logger.lines)
    assert "[OMS-WEBULL-CANCEL-THEN-SELL] exit=CW_HARD_STOP" in lines
    assert "sell_refused_recovering" in lines
    incidents = [i for i in _incidents(sf) if i.get("broker_account_name") == WEBULL]
    assert incidents, "a refused pre-market hard stop must PAGE - YMAT sat silent for 78 min"
    # pre-market: no pair can be attached, so the page says protection FAILED - the truth
    assert incidents[0]["title"] == f"CW_HARD_STOP exit protection FAILED: {SYMBOL} on {WEBULL}; check now"
    assert str(incidents[0].get("protection_restored", "false")).lower() in ("false", "0")
    # the row is still open (nothing sold) - honest state, not a phantom close
    with sf() as session:
        row = service.store.get_open_managed_position(
            session, broker_account_name=WEBULL, symbol=SYMBOL
        )
    assert row is not None and row.status == "open"


@pytest.mark.asyncio
async def test_refused_hard_stop_is_paced_not_a_per_quote_storm(monkeypatch) -> None:
    # YMAT's three refusals inside one second. After a refused run the next quote waits 10 s.
    adapter = _FanoutAdapter(reject_accounts={WEBULL})
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    _cw(service)
    service._webull_protect_base.pop((WEBULL, SYMBOL), None)

    async def _held(*args, **kwargs):
        return service_module._PositionRead.HELD

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(service, "_broker_symbol_position_state", _held)
    monkeypatch.setattr(service_module.asyncio, "sleep", _no_sleep)
    clock = {"t": 1000.0}
    # the SERVICE module's clock only - patching time.monotonic globally freezes asyncio's loop
    monkeypatch.setattr(service_module, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    _quote(service, 9.40)

    for _ in range(3):  # three quotes inside one second
        await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
        clock["t"] += 0.3
    for task in list(service.__dict__.get("_confirmation_exit_recovery_tasks", set())):
        await task
    # count the LADDER's close attempts only: the recovery's re-attach sends a protective SELL
    # pair (reason "webull attach protection after bare rest") that must not be confused with them
    def _closes() -> int:
        return len(
            [a for a in adapter.submitted if a.side == "sell" and "CW_HARD_STOP" in a.reason]
        )

    assert _closes() == 1, "one refused close, then pacing - not three in a second"

    clock["t"] += service._WEBULL_CW_EXIT_RETRY_SECONDS + 0.1
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    for task in list(service.__dict__.get("_confirmation_exit_recovery_tasks", set())):
        await task
    assert _closes() == 2  # retried after 10 s


@pytest.mark.asyncio
async def test_floor_on_webull_uses_the_same_routine(monkeypatch) -> None:
    service, sf = _service(fanout=True, adapter=_FanoutAdapter())
    service.logger = _CapturedLogger()
    _cw(service)
    service._cw_floor_exit_enabled = True
    service._cw_floor_armed.add((WEBULL, SYMBOL))  # rode past +2%, floor armed at +1%
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    _quote(service, 10.05)  # back under the +1% floor (10.10)

    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    assert _sell_accounts(sf) == [WEBULL]
    assert "[OMS-WEBULL-CANCEL-THEN-SELL] exit=CW_FLOOR" in "\n".join(service.logger.lines)
    assert (WEBULL, SYMBOL) not in service._cw_floor_armed


@pytest.mark.asyncio
async def test_CONTROL_schwab_hard_stop_is_unchanged(monkeypatch) -> None:
    service, sf = _service(fanout=True, adapter=_FanoutAdapter())
    service.logger = _CapturedLogger()
    _cw(service)
    _quote(service, 9.40)

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)

    assert _sell_accounts(sf) == [SCHWAB]
    assert "[OMS-WEBULL-CANCEL-THEN-SELL]" not in "\n".join(service.logger.lines)


@pytest.mark.asyncio
async def test_CONTROL_a_resting_bracket_still_owns_the_stop(monkeypatch) -> None:
    # The stand-down: while a native pair rests, the software stop does not fire at all.
    service, sf = _service(fanout=True, adapter=_FanoutAdapter())
    service.logger = _CapturedLogger()
    _cw(service)
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    monkeypatch.setattr(service, "_native_oco_stand_down_active", lambda acct, sym: True)
    _quote(service, 9.40)

    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    assert _sell_accounts(sf) == []
    assert "[OMS-WEBULL-CANCEL-THEN-SELL]" not in "\n".join(service.logger.lines)


@pytest.mark.asyncio
async def test_no_second_sell_while_the_first_refusal_is_still_recovering(monkeypatch) -> None:
    # #1032 review P1: a refused sell only SPAWNS recovery. Ten seconds later the ladder must not
    # send another sell while that recovery is still reading the broker / re-attaching.
    adapter = _FanoutAdapter(reject_accounts={WEBULL})
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    _cw(service)
    service._webull_protect_base.pop((WEBULL, SYMBOL), None)
    gate = asyncio.Event()  # the broker read hangs until we release it

    async def _blocked_read(*args, **kwargs):
        await gate.wait()
        return service_module._PositionRead.HELD

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(service, "_broker_symbol_position_state", _blocked_read)
    monkeypatch.setattr(service_module.asyncio, "sleep", _no_sleep)
    clock = {"t": 1000.0}
    monkeypatch.setattr(service_module, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    _quote(service, 9.40)

    def _closes() -> int:
        return len([a for a in adapter.submitted if a.side == "sell" and "CW_HARD_STOP" in a.reason])

    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    assert _closes() == 1
    assert service._webull_recovery_in_progress(WEBULL, SYMBOL)

    clock["t"] += 60.0  # well past the 10 s pacing - recovery is STILL running
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    assert _closes() == 1, "a second sell raced a running recovery"

    gate.set()  # the broker answers; recovery finishes
    for task in list(service.__dict__.get("_confirmation_exit_recovery_tasks", set())):
        await task
    assert not service._webull_recovery_in_progress(WEBULL, SYMBOL)
    clock["t"] += 60.0
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    assert _closes() == 2  # CONTROL: once recovery has ended, the ladder may try again


@pytest.mark.asyncio
async def test_a_never_protected_share_is_not_reported_as_released(monkeypatch) -> None:
    # #1032 review P2: recovery used to record released=True unconditionally.
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: False)
    adapter = _FanoutAdapter(reject_accounts={WEBULL})
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    _cw(service)
    service._webull_protect_base.pop((WEBULL, SYMBOL), None)

    async def _held(*args, **kwargs):
        return service_module._PositionRead.HELD

    monkeypatch.setattr(service, "_broker_symbol_position_state", _held)
    _quote(service, 9.40)
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    for task in list(service.__dict__.get("_confirmation_exit_recovery_tasks", set())):
        await task

    fanout = [l for l in service.logger.lines if "[OMS-V2-CONFIRMATION-EXIT-FANOUT]" in l][-1]
    assert "legs_released=0" in fanout and "legs_uncovered=1" in fanout
    assert "released_accounts=-" in fanout
    incident = [i for i in _incidents(sf) if i.get("broker_account_name") == WEBULL][0]
    assert incident["exit_tag"] == "CW_HARD_STOP"
