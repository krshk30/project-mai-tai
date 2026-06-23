"""v2 extended-hours entry routing — real-emit coverage.

The isolated `schwab_1m_v2` bot previously emitted plain market/NORMAL open
intents, which Schwab cannot fill outside 9:30-16:00. This restores the legacy
macd_30s / schwab_1m handoff at the emit chokepoint: in extended hours the open
intent is stamped session=AM/PM + order_type=limit + limit_price = the live ask
(mirroring `_resolve_routed_price`); RTH is byte-identical (market/NORMAL).

Exercises `_apply_extended_hours_routing` directly with a frozen clock and a
seeded quote — no Redis, no run loop, no emitter.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import TradeIntentDraft

PRE = datetime(2026, 6, 23, 11, 0, tzinfo=UTC)    # 07:00 ET premarket
RTH = datetime(2026, 6, 23, 14, 0, tzinfo=UTC)    # 10:00 ET regular
POST = datetime(2026, 6, 23, 21, 0, tzinfo=UTC)   # 17:00 ET post


def _svc() -> SchwabV2BotService:
    return SchwabV2BotService(Settings(strategy_schwab_1m_v2_enabled=True))


def _open_draft(symbol: str = "SUNE") -> TradeIntentDraft:
    return TradeIntentDraft(
        symbol=symbol,
        side="buy",
        intent_type="open",
        quantity=Decimal("10"),
        reason="schwab_1m_v2 ATR Flip B",
        metadata={"path": "ATR Flip", "reference_price": "2.87"},
    )


def _seed_quote(svc: SchwabV2BotService, symbol: str = "SUNE", ask: float = 2.91, bid: float = 2.89) -> None:
    svc._last_quote_by_symbol[symbol.upper()] = Quote(
        symbol=symbol, bid_price=bid, ask_price=ask, last_price=2.90, quote_time_ms=0,
    )


def test_rth_entry_unchanged():
    # Gate (a): RTH -> nothing added; order stays market/NORMAL (byte-identical).
    svc = _svc()
    _seed_quote(svc)
    draft = _open_draft()
    before = dict(draft.metadata)
    assert svc._apply_extended_hours_routing(draft, RTH) is True
    assert draft.metadata == before
    assert "session" not in draft.metadata
    assert "order_type" not in draft.metadata


def test_premarket_entry_routes_ask_limit():
    # Gate (b): premarket -> session=AM + limit + limit_price = the live ask.
    svc = _svc()
    _seed_quote(svc, ask=2.91)
    draft = _open_draft()
    assert svc._apply_extended_hours_routing(draft, PRE) is True
    assert draft.metadata["session"] == "AM"
    assert draft.metadata["order_type"] == "limit"
    assert draft.metadata["extended_hours"] == "true"
    assert draft.metadata["limit_price"] == "2.91"  # the ask, formatted to cents
    assert draft.metadata["price_source"] == "ask"


def test_postmarket_entry_routes_pm():
    svc = _svc()
    _seed_quote(svc, ask=3.05)
    draft = _open_draft()
    assert svc._apply_extended_hours_routing(draft, POST) is True
    assert draft.metadata["session"] == "PM"
    assert draft.metadata["limit_price"] == "3.05"


def test_extended_hours_no_ask_skips_entry():
    # Mirrors legacy _resolve_routed_price: no ask quote in extended hours -> skip
    # (a limit order with no price is invalid; better to not send it).
    svc = _svc()  # no quote seeded
    draft = _open_draft()
    assert svc._apply_extended_hours_routing(draft, PRE) is False
    assert "session" not in draft.metadata
    assert "order_type" not in draft.metadata


def test_non_open_intent_untouched():
    # Only entries get routing; exits/cancels are the OMS exit-ladder's job.
    svc = _svc()
    _seed_quote(svc)
    draft = TradeIntentDraft(
        symbol="SUNE", side="sell", intent_type="close",
        quantity=Decimal("10"), reason="x", metadata={},
    )
    assert svc._apply_extended_hours_routing(draft, PRE) is True
    assert draft.metadata == {}
