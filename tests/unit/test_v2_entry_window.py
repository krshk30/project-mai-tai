"""v2 trading-window entry gate — the operator's 7:00 AM–4:30 PM ET rule.

The isolated `schwab_1m_v2` bot had no clock gate on entries — `_market_session`
treats 4 PM–8 PM ET as tradeable "afterhours", so on 2026-07-13 it opened AGEN and
SOBR at ~7:51 PM ET; those positions then churned unfillable exits overnight. The
emit chokepoint (`_maybe_emit`) now drops "open" intents outside [start, end) ET
(default 7:00–16:30, narrowed from 7:00–18:00 by the 2026-07-15 operator rule).
Exits (cw_flip + OMS-managed) are unaffected.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import TradeIntentDraft

EASTERN = ZoneInfo("America/New_York")


def _svc(**settings_kwargs) -> SchwabV2BotService:
    return SchwabV2BotService(Settings(strategy_schwab_1m_v2_enabled=True, **settings_kwargs))


def _open_draft(symbol: str = "SUNE") -> TradeIntentDraft:
    return TradeIntentDraft(
        symbol=symbol,
        side="buy",
        intent_type="open",
        quantity=Decimal("10"),
        reason="schwab_1m_v2 ATR Flip B",
        metadata={"path": "ATR Flip", "reference_price": "2.87"},
    )


class _RecordingEmitter:
    def __init__(self) -> None:
        self.emitted: list = []
        self.cw_flips: list = []

    async def emit(self, draft) -> None:
        self.emitted.append(draft)

    async def emit_cw_flip(self, symbol, bar_time_ms) -> None:
        self.cw_flips.append((symbol, bar_time_ms))


# --- _within_entry_window boundaries (default 7:00–16:30 ET) ---


def test_default_entry_window_is_seven_to_sixteen_thirty() -> None:
    """Pin the operator rule in the DEFAULTS, so the live gate does not depend on env."""
    s = Settings()
    assert s.strategy_schwab_1m_v2_entry_window_start_hour_et == 7
    assert s.strategy_schwab_1m_v2_entry_window_start_minute_et == 0
    assert s.strategy_schwab_1m_v2_entry_window_end_hour_et == 16
    assert s.strategy_schwab_1m_v2_entry_window_end_minute_et == 30


def test_within_entry_window_inside() -> None:
    svc = _svc()
    assert svc._within_entry_window(datetime(2026, 7, 14, 7, 0, tzinfo=EASTERN)) is True   # 7 AM sharp
    assert svc._within_entry_window(datetime(2026, 7, 14, 12, 0, tzinfo=EASTERN)) is True
    assert svc._within_entry_window(datetime(2026, 7, 14, 16, 29, tzinfo=EASTERN)) is True  # 4:29 PM


def test_within_entry_window_outside() -> None:
    svc = _svc()
    assert svc._within_entry_window(datetime(2026, 7, 14, 6, 59, tzinfo=EASTERN)) is False   # pre-7 AM
    assert svc._within_entry_window(datetime(2026, 7, 14, 16, 30, tzinfo=EASTERN)) is False  # 4:30 sharp
    assert svc._within_entry_window(datetime(2026, 7, 13, 19, 51, tzinfo=EASTERN)) is False  # the incident time
    assert svc._within_entry_window(datetime(2026, 7, 11, 10, 0, tzinfo=EASTERN)) is False   # Saturday


def test_within_entry_window_blocks_the_old_late_afternoon_tail() -> None:
    """The 2026-07-15 narrowing: 16:30–18:00 used to be enterable, now it is not."""
    svc = _svc()
    for hh, mm in ((16, 31), (17, 0), (17, 59)):
        assert svc._within_entry_window(datetime(2026, 7, 14, hh, mm, tzinfo=EASTERN)) is False


def test_within_entry_window_respects_settings() -> None:
    svc = _svc(
        strategy_schwab_1m_v2_entry_window_start_hour_et=8,
        strategy_schwab_1m_v2_entry_window_end_hour_et=16,
        strategy_schwab_1m_v2_entry_window_end_minute_et=0,
    )
    assert svc._within_entry_window(datetime(2026, 7, 14, 7, 30, tzinfo=EASTERN)) is False  # before 8
    assert svc._within_entry_window(datetime(2026, 7, 14, 9, 0, tzinfo=EASTERN)) is True
    assert svc._within_entry_window(datetime(2026, 7, 14, 16, 0, tzinfo=EASTERN)) is False  # 4 PM end excl.


def test_entry_window_rollback_to_eighteen_via_env_overrides() -> None:
    """Rollback lever: the pre-07-15 7–18 window is restorable without a code change."""
    svc = _svc(
        strategy_schwab_1m_v2_entry_window_end_hour_et=18,
        strategy_schwab_1m_v2_entry_window_end_minute_et=0,
    )
    assert svc._within_entry_window(datetime(2026, 7, 14, 17, 59, tzinfo=EASTERN)) is True
    assert svc._within_entry_window(datetime(2026, 7, 14, 18, 0, tzinfo=EASTERN)) is False


# --- _maybe_emit honours the gate ---


@pytest.mark.asyncio
async def test_maybe_emit_drops_open_outside_window(monkeypatch) -> None:
    svc = _svc()
    svc.intent_emitter = _RecordingEmitter()
    monkeypatch.setattr(svc, "_within_entry_window", lambda now: False)
    await svc._maybe_emit(_open_draft())
    assert svc.intent_emitter.emitted == []  # dropped at the chokepoint, no broker intent


@pytest.mark.asyncio
async def test_maybe_emit_allows_open_inside_window(monkeypatch) -> None:
    svc = _svc()
    svc.intent_emitter = _RecordingEmitter()
    monkeypatch.setattr(svc, "_within_entry_window", lambda now: True)
    # Force RTH so extended-hours routing is a no-op and the emit goes through.
    monkeypatch.setattr(
        "project_mai_tai.services.schwab_1m_v2_bot.extended_hours_session", lambda now: None
    )
    await svc._maybe_emit(_open_draft())
    assert len(svc.intent_emitter.emitted) == 1


@pytest.mark.asyncio
async def test_maybe_emit_cw_flip_exit_not_gated(monkeypatch) -> None:
    """A cw_flip CLOSE draft is an EXIT — it must publish even outside the entry
    window (the window only caps entries)."""
    svc = _svc()
    svc.intent_emitter = _RecordingEmitter()
    monkeypatch.setattr(svc, "_within_entry_window", lambda now: False)  # outside window
    close_draft = TradeIntentDraft(
        symbol="SUNE",
        side="sell",
        intent_type="close",
        quantity=Decimal("10"),
        reason="schwab_1m_v2 CW flip",
        metadata={"cw_flip": "true", "bar_time_ms": "123"},
    )
    await svc._maybe_emit(close_draft)
    assert svc.intent_emitter.cw_flips == [("SUNE", "123")]
    assert svc.intent_emitter.emitted == []
