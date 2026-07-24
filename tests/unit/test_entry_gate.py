"""Characterization tests for the SHARED entry emit-gate (`strategy_core.entry_gate`).

These pin the behavior-identical extraction (Deliverable 1 of the backtest-replay P1):
the live bot's former inline `_within_entry_window` / `_apply_extended_hours_routing`
bodies now delegate to `entry_gate`, and the replay runs the same functions. The tests
prove the shared primitives return EXACTLY what the bot methods return (byte-for-byte
metadata + return value) across RTH, pre-market EH, post-market EH, the ORB-skip window,
and the drop cases — so the extraction cannot have changed live behavior. The existing
`test_v2_entry_window.py` / `test_v2_extended_hours_routing.py` suites (unchanged) are the
other half of the proof: they still call the bot methods directly and stay green.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import entry_gate
from project_mai_tai.strategy_core.schwab_1m_v2 import TradeIntentDraft

EASTERN = ZoneInfo("America/New_York")
PRE = datetime(2026, 6, 23, 11, 0, tzinfo=UTC)    # 07:00 ET premarket
RTH = datetime(2026, 6, 23, 14, 0, tzinfo=UTC)    # 10:00 ET regular
POST = datetime(2026, 6, 23, 21, 0, tzinfo=UTC)   # 17:00 ET post


def _svc(**kw) -> SchwabV2BotService:
    return SchwabV2BotService(Settings(strategy_schwab_1m_v2_enabled=True, **kw))


def _open_draft(symbol: str = "SUNE") -> TradeIntentDraft:
    return TradeIntentDraft(
        symbol=symbol, side="buy", intent_type="open", quantity=Decimal("10"),
        reason="schwab_1m_v2 ATR Flip B", metadata={"path": "ATR Flip", "reference_price": "2.87"},
    )


def _seed_quote(svc, symbol="SUNE", ask=2.91, bid=2.89):
    svc._last_quote_by_symbol[symbol.upper()] = Quote(
        symbol=symbol, bid_price=bid, ask_price=ask, last_price=2.90, quote_time_ms=0,
    )


# ---------------------------------------------------------------- window primitive parity
@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 7, 14, 6, 59, tzinfo=EASTERN),
        datetime(2026, 7, 14, 7, 0, tzinfo=EASTERN),      # start sharp
        datetime(2026, 7, 14, 9, 45, tzinfo=EASTERN),     # ORB-skip window — still IN the emit window
        datetime(2026, 7, 14, 12, 0, tzinfo=EASTERN),
        datetime(2026, 7, 14, 15, 59, tzinfo=EASTERN),
        datetime(2026, 7, 14, 16, 0, tzinfo=EASTERN),     # end sharp (excluded)
        datetime(2026, 7, 13, 19, 51, tzinfo=EASTERN),    # the AGEN/SOBR incident time
        datetime(2026, 7, 11, 10, 0, tzinfo=EASTERN),     # Saturday
    ],
)
def test_within_entry_window_matches_bot_method(when) -> None:
    """The shared fn returns exactly what the bot's `_within_entry_window` returns."""
    svc = _svc()
    assert entry_gate.within_entry_window(when, svc.settings) == svc._within_entry_window(when)


def test_within_entry_window_respects_settings_like_bot() -> None:
    svc = _svc(
        strategy_schwab_1m_v2_entry_window_start_hour_et=8,
        strategy_schwab_1m_v2_entry_window_end_hour_et=16,
        strategy_schwab_1m_v2_entry_window_end_minute_et=0,
    )
    for when in (
        datetime(2026, 7, 14, 7, 30, tzinfo=EASTERN),
        datetime(2026, 7, 14, 9, 0, tzinfo=EASTERN),
        datetime(2026, 7, 14, 16, 0, tzinfo=EASTERN),
    ):
        assert entry_gate.within_entry_window(when, svc.settings) == svc._within_entry_window(when)


def test_resolve_entry_window_defaults() -> None:
    # Threshold-pin: mutate any of these and the shared window bounds shift (07:00–16:00 ET).
    assert entry_gate.resolve_entry_window(Settings()) == (7, 0, 16, 0)


# ------------------------------------------------------------- EH-routing primitive parity
def _routing_parity(svc, when):
    """Run the bot method and the shared fn on two IDENTICAL fresh drafts; return
    (bot_ret, bot_meta, shared_ret, shared_meta) for a byte-for-byte compare."""
    bot_draft, shared_draft = _open_draft(), _open_draft()
    bot_ret = svc._apply_extended_hours_routing(bot_draft, when)
    shared_ret = entry_gate.route_extended_hours(
        shared_draft, when, svc._last_quote_by_symbol.get,
    )
    return bot_ret, dict(bot_draft.metadata), shared_ret, dict(shared_draft.metadata)


def test_eh_routing_rth_unchanged_matches() -> None:
    svc = _svc()
    _seed_quote(svc)
    br, bm, sr, sm = _routing_parity(svc, RTH)
    assert br is True and sr is True
    assert bm == sm  # neither adds session/order_type in RTH


def test_eh_routing_premarket_matches() -> None:
    svc = _svc()
    _seed_quote(svc, ask=2.91)
    br, bm, sr, sm = _routing_parity(svc, PRE)
    assert br is True and sr is True
    assert bm == sm
    assert sm["session"] == "AM" and sm["order_type"] == "limit" and sm["limit_price"] == "2.91"


def test_eh_routing_postmarket_matches() -> None:
    svc = _svc()
    _seed_quote(svc, ask=3.05)
    br, bm, sr, sm = _routing_parity(svc, POST)
    assert br is True and sr is True and bm == sm and sm["session"] == "PM"


def test_eh_routing_no_ask_skip_matches() -> None:
    svc = _svc()  # no quote seeded
    br, bm, sr, sm = _routing_parity(svc, PRE)
    assert br is False and sr is False and bm == sm  # both skip, neither mutates


def test_eh_routing_non_open_untouched_matches() -> None:
    svc = _svc()
    _seed_quote(svc)
    bot_draft = TradeIntentDraft(symbol="SUNE", side="sell", intent_type="close",
                                 quantity=Decimal("10"), reason="x", metadata={})
    shared_draft = TradeIntentDraft(symbol="SUNE", side="sell", intent_type="close",
                                    quantity=Decimal("10"), reason="x", metadata={})
    assert svc._apply_extended_hours_routing(bot_draft, PRE) is True
    assert entry_gate.route_extended_hours(shared_draft, PRE, svc._last_quote_by_symbol.get) is True
    assert dict(bot_draft.metadata) == dict(shared_draft.metadata) == {}


# --------------------------------------------------------- gate_open_intent decision cases
def test_gate_drops_outside_window() -> None:
    svc = _svc()
    d = entry_gate.gate_open_intent(_open_draft(), POST, svc.settings, svc._last_quote_by_symbol.get)
    # POST (17:00 ET) is outside 07:00–16:00 -> window drop takes precedence.
    assert d.emit is False and d.drop_reason == "entry_window"


def test_gate_drops_non_atr_reason_when_atr_only() -> None:
    svc = _svc(strategy_schwab_1m_v2_atr_only_mode=True)
    draft = _open_draft()
    draft.reason = "schwab_1m_v2 MACD Cross"  # not an ATR path
    d = entry_gate.gate_open_intent(draft, RTH, svc.settings, svc._last_quote_by_symbol.get)
    assert d.emit is False and d.drop_reason == "atr_only"


def test_gate_drops_eh_no_ask() -> None:
    svc = _svc()  # no quote seeded
    d = entry_gate.gate_open_intent(_open_draft(), PRE, svc.settings, svc._last_quote_by_symbol.get)
    assert d.emit is False and d.drop_reason == "eh_no_quote"


def test_gate_passes_rth_unmutated() -> None:
    svc = _svc(strategy_schwab_1m_v2_atr_only_mode=True)
    _seed_quote(svc)
    draft = _open_draft()
    before = dict(draft.metadata)
    d = entry_gate.gate_open_intent(draft, RTH, svc.settings, svc._last_quote_by_symbol.get)
    assert d.emit is True and d.drop_reason == "" and dict(d.draft.metadata) == before


def test_gate_passes_eh_routes_ask_limit() -> None:
    svc = _svc()
    _seed_quote(svc, ask=2.91)
    d = entry_gate.gate_open_intent(_open_draft(), PRE, svc.settings, svc._last_quote_by_symbol.get)
    assert d.emit is True and d.draft.metadata["session"] == "AM"
    assert d.draft.metadata["limit_price"] == "2.91"


# --------------------------------------------- gate_open_intent mirrors the real _maybe_emit
class _FrozenNow(datetime):
    """A datetime subclass whose `.now()` returns a fixed instant, so the bot's
    internal `datetime.now(UTC)` in `_maybe_emit` is driven to a chosen wall-clock —
    the honest way to force RTH vs EH end-to-end (the EH routing re-derives the session
    from `now` via `order_routing_metadata`, so faking only the gate is not enough)."""

    _fixed: datetime = RTH

    @classmethod
    def now(cls, tz=None):  # noqa: D401
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)


class _Rec:
    def __init__(self): self.emitted = []
    async def emit(self, d): self.emitted.append(d)
    async def emit_cw_flip(self, *a): ...


async def _emit_at(monkeypatch, fixed: datetime, **svc_kw):
    """Drive the REAL `_maybe_emit` with the wall-clock frozen at `fixed`; return the emitted
    draft's metadata (or None if dropped)."""
    svc = _svc(**svc_kw)
    _seed_quote(svc, ask=2.91)
    svc.intent_emitter = _Rec()
    frozen = type("F", (_FrozenNow,), {"_fixed": fixed})
    monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.datetime", frozen)
    await svc._maybe_emit(_open_draft())
    return svc, (dict(svc.intent_emitter.emitted[0].metadata) if svc.intent_emitter.emitted else None)


@pytest.mark.asyncio
async def test_gate_open_intent_mirrors_maybe_emit_rth(monkeypatch) -> None:
    """End-to-end: with the clock frozen at RTH (10:00 ET, inside window), the real bot
    `_maybe_emit` emits a draft whose metadata equals `gate_open_intent`'s emitted draft —
    the replay's gate == the live emit path."""
    svc, live_meta = await _emit_at(monkeypatch, RTH, strategy_schwab_1m_v2_atr_only_mode=True)
    assert live_meta is not None
    d = entry_gate.gate_open_intent(_open_draft(), RTH, svc.settings, svc._last_quote_by_symbol.get)
    assert d.emit is True and dict(d.draft.metadata) == live_meta


@pytest.mark.asyncio
async def test_gate_open_intent_mirrors_maybe_emit_eh(monkeypatch) -> None:
    """End-to-end EH parity: with the clock frozen at pre-market (07:00 ET), the live emit path
    routes the ask-limit and `gate_open_intent` (same `now`) produces the identical routed metadata."""
    svc, live_meta = await _emit_at(monkeypatch, PRE, strategy_schwab_1m_v2_atr_only_mode=True)
    assert live_meta is not None and live_meta["session"] == "AM" and live_meta["limit_price"] == "2.91"
    d = entry_gate.gate_open_intent(_open_draft(), PRE, svc.settings, svc._last_quote_by_symbol.get)
    assert d.emit is True and dict(d.draft.metadata) == live_meta


@pytest.mark.asyncio
async def test_gate_open_intent_mirrors_maybe_emit_outside_window(monkeypatch) -> None:
    """End-to-end drop parity: clock at 17:00 ET (POST, outside 07:00–16:00) — the live path
    drops the open (nothing emitted) and `gate_open_intent` reports the entry_window drop."""
    svc, live_meta = await _emit_at(monkeypatch, POST, strategy_schwab_1m_v2_atr_only_mode=True)
    assert live_meta is None  # dropped at the window chokepoint
    d = entry_gate.gate_open_intent(_open_draft(), POST, svc.settings, svc._last_quote_by_symbol.get)
    assert d.emit is False and d.drop_reason == "entry_window"
