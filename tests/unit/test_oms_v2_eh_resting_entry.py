"""OMS extended-hours RESTING-entry band-cap re-price (P-B2) — flag-gated, default-off, byte-identical
when off (docs/premarket-eod-exit-design.md).

The strategy software-emulates the resting buy-stop-limit in EH (a broker stop trigger is dead there on
both brokers) and emits a MARKETABLE open tagged eh_resting on the ATR up-cross. This builder
(`strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled`, the SAME switch the strategy reads) re-prices it
off the OMS's OWN fresh Polygon ask -> min(ask, level*(1+band)); ASK past the band or no fresh ask ->
ABANDON (no blind order, no chase). RTH / flag-off / non-eh_resting are byte-identical.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import project_mai_tai.oms.service as oms_service
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

_FLAG = "strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled"


# --------------------------------------------------------------------------- helpers

class _FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    async def xadd(self, stream, fields, **kwargs):
        del kwargs
        self.entries.append((stream, json.loads(fields["data"])))
        return "1-0"

    async def get(self, key):
        return None

    async def set(self, key, value, ex=None):
        del ex
        return True

    async def xread(self, offsets, block=0, count=0):
        del offsets, block, count
        return []

    async def aclose(self):
        return None


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _oms(**settings_kw) -> OmsRiskService:
    return OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated", **settings_kw),
        redis_client=_FakeRedis(),
        session_factory=_session_factory(),
    )


def _v2_open(metadata: dict, *, symbol: str = "FOO", qty: str = "10") -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="schwab_1m_v2",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name="paper:schwab_1m_v2",
            symbol=symbol,
            side="buy",
            quantity=Decimal(qty),
            intent_type="open",
            reason="schwab_1m_v2 ATR Flip CW-v2-resting",
            metadata=metadata,
        ),
    )


def _eh_resting_meta(**extra) -> dict:
    """The metadata the OMS receives for a v2 EH RESTING open: the strategy's cross fields PLUS the bot's
    `_apply_extended_hours_routing` stamp (order_type=limit + session=AM + limit_price=ask)."""
    md = {
        "path": "ATR Flip",
        "atr_variant": "CW-v2-resting",
        "resting_entry": "true",
        "eh_resting": "true",
        "resting_level": "9.5000",       # the ATR line = the band anchor
        "resting_band_pct": "0.5",
        "entry_price": "9.5000",
        "order_type": "limit",           # bot already routed to a limit
        "session": "AM",
        "extended_hours": "true",
        "limit_price": "9.5000",         # bot's plain limit-at-ask
        "reference_price": "9.5000",
        "price_source": "ask",
    }
    md.update(extra)
    return md


def _eh_webull_fanout_meta(*, level: str, band_pct: str = "0.5") -> dict:
    return {
        "path": "ATR Flip",
        "atr_variant": "CW-v2-fanout",
        "fanout_leg": "webull",
        "fanout_source": "eh_resting",
        "entry_price": level,
        "reference_price": level,
        "resting_band_pct": band_pct,
        "order_type": "limit",
        "session": "AM",
        "extended_hours": "true",
        "limit_price": level,
        "price_source": "ask",
    }


def _set_quote(service: OmsRiskService, symbol: str, *, ask: float, bid: float, age_ms: int = 0) -> None:
    service._latest_quotes_by_symbol[symbol.upper()] = {
        "ask": ask,
        "bid": bid,
        "received_at": datetime.now(UTC) - timedelta(milliseconds=age_ms),
    }


def _stored_order(service: OmsRiskService) -> BrokerOrder | None:
    with service.session_factory() as session:
        return session.scalar(select(BrokerOrder))


@pytest.fixture
def eh(monkeypatch):
    """Force extended hours (pre-market AM) for the module-level session helpers the OMS reads."""
    monkeypatch.setattr(oms_service, "_is_regular_market_session", lambda now=None: False)
    monkeypatch.setattr(oms_service, "_extended_hours_session", lambda now=None: "AM")


@pytest.fixture
def rth(monkeypatch):
    monkeypatch.setattr(oms_service, "_is_regular_market_session", lambda now=None: True)
    monkeypatch.setattr(oms_service, "_extended_hours_session", lambda now=None: None)


@pytest.fixture
def pm(monkeypatch):
    monkeypatch.setattr(oms_service, "_is_regular_market_session", lambda now=None: False)
    monkeypatch.setattr(oms_service, "_extended_hours_session", lambda now=None: "PM")


# --------------------------------------------------------------------------- flag / default

def test_flag_defaults_off():
    s = Settings()
    assert getattr(s, _FLAG) is False
    assert s.oms_v2_eh_resting_entry_band_pct == 0.5
    assert s.oms_v2_eh_resting_entry_quote_max_age_ms == 2000


# --------------------------------------------------------------------------- re-pricing (flag ON, EH)

@pytest.mark.asyncio
async def test_eh_prices_min_ask_cap(eh):
    """ask 9.52 < cap (9.5*1.005 = 9.54750) -> marketable limit at the ask (min(ask, cap) = ask)."""
    service = _oms(**{_FLAG: True})
    _set_quote(service, "FOO", ask=9.52, bid=9.50)
    events = await service.process_trade_intent(_v2_open(_eh_resting_meta()))
    assert [e.payload.status for e in events] == ["accepted", "filled"]
    order = _stored_order(service)
    assert order is not None
    assert order.payload["order_type"] == "limit"
    assert order.payload["session"] == "AM"
    assert order.payload["extended_hours"] == "true"
    assert order.payload["oms_v2_eh_resting_entry"] == "true"
    assert order.payload["oms_v2_eh_resting_entry_ask"] == "9.5200"
    assert order.payload["oms_v2_eh_resting_entry_cap"] == "9.5475"
    assert order.payload["limit_price"] == "9.52"   # min(9.52, 9.5475) floored to tick
    assert order.payload["reference_price"] == "9.52"


@pytest.mark.asyncio
async def test_eh_limit_floored_to_band_cap(eh):
    """ask 9.55 is between the level (9.5) and the cap? No — cap is 9.5475, so 9.55 > cap -> abandon.
    Use ask 9.5460 (<= cap) to exercise the min-floor: min(9.5460, 9.5475) = 9.5460 -> 9.54 (round-down)."""
    service = _oms(**{_FLAG: True})
    _set_quote(service, "FOO", ask=9.5460, bid=9.53)
    events = await service.process_trade_intent(_v2_open(_eh_resting_meta()))
    assert [e.payload.status for e in events] == ["accepted", "filled"]
    order = _stored_order(service)
    assert order is not None
    assert order.payload["limit_price"] == "9.54"   # 9.5460 floored to 2-dp tick
    assert order.payload["oms_v2_eh_resting_entry"] == "true"


@pytest.mark.asyncio
async def test_eh_abandons_ask_past_band(eh):
    """ask 9.60 > band cap 9.5475 -> the market gapped past the line -> ABANDON (no chase, prefer no fill)."""
    service = _oms(**{_FLAG: True})
    _set_quote(service, "FOO", ask=9.60, bid=9.58)
    events = await service.process_trade_intent(_v2_open(_eh_resting_meta()))
    assert events[-1].payload.status == "rejected"
    assert events[-1].payload.reason == "ASK_PAST_BAND"
    assert _stored_order(service) is None   # never submitted


@pytest.mark.asyncio
async def test_clro_premarket_webull_and_schwab_both_refuse_past_half_percent(eh):
    # CLRO 09-28 08:51 ET: Webull previously chased to 5.57 after Schwab refused.
    for metadata in (
        _eh_resting_meta(resting_level="5.5284", entry_price="5.5284"),
        _eh_webull_fanout_meta(level="5.5284"),
    ):
        service = _oms(**{_FLAG: True}, oms_v2_eh_entry_enabled=True)
        _set_quote(service, "CLRO", ask=5.56, bid=5.55)
        events = await service.process_trade_intent(_v2_open(metadata, symbol="CLRO"))
        assert events[-1].payload.status == "rejected"
        assert events[-1].payload.reason == "ASK_PAST_BAND"
        assert _stored_order(service) is None


@pytest.mark.asyncio
async def test_dcoy_premarket_webull_and_schwab_use_same_ask_limit(eh):
    # DCOY 09-23: 4.32 ask is within the 4.3148 * 1.005 = 4.3364 cap.
    for metadata in (
        _eh_resting_meta(resting_level="4.3148", entry_price="4.3148"),
        _eh_webull_fanout_meta(level="4.3148"),
    ):
        service = _oms(**{_FLAG: True}, oms_v2_eh_entry_enabled=True)
        _set_quote(service, "DCOY", ask=4.32, bid=4.31)
        events = await service.process_trade_intent(_v2_open(metadata, symbol="DCOY"))
        assert [event.payload.status for event in events] == ["accepted", "filled"]
        order = _stored_order(service)
        assert order is not None
        assert order.payload["limit_price"] == "4.32"
        assert order.payload["oms_v2_eh_resting_entry_cap"] == "4.3364"
        assert "oms_v2_eh_entry" not in order.payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("level", "at_cap", "past_cap", "other_line"),
    (("8.0000", 8.04, 8.041, "7.0000"), ("10.0000", 10.05, 10.051, "9.0000")),
)
async def test_premarket_webull_band_uses_its_own_anchor_and_exact_boundary(
    eh, level, at_cap, past_cap, other_line
):
    for ask, expected_status in ((at_cap, "filled"), (past_cap, "rejected")):
        service = _oms(**{_FLAG: True}, oms_v2_eh_entry_enabled=True)
        _set_quote(service, "BOUND", ask=ask, bid=at_cap - 0.01)
        metadata = _eh_webull_fanout_meta(level=level)
        metadata["cw_flip_level"] = other_line  # Not the cap anchor.
        events = await service.process_trade_intent(_v2_open(metadata, symbol="BOUND"))
        assert events[-1].payload.status == expected_status
        if expected_status == "filled":
            assert _stored_order(service).payload["limit_price"] == f"{at_cap:.2f}"
        else:
            assert events[-1].payload.reason == "ASK_PAST_BAND"
            assert _stored_order(service) is None


@pytest.mark.asyncio
async def test_premarket_webull_uses_its_own_band_not_the_oms_default(eh):
    service = _oms(**{_FLAG: True}, oms_v2_eh_entry_enabled=True)
    _set_quote(service, "BOUND", ask=8.03, bid=8.02)
    events = await service.process_trade_intent(
        _v2_open(_eh_webull_fanout_meta(level="8.0000", band_pct="0.25"), symbol="BOUND")
    )
    assert events[-1].payload.reason == "ASK_PAST_BAND"  # cap is 8.02, not 8.04
    assert _stored_order(service) is None


@pytest.mark.asyncio
async def test_premarket_webull_keeps_reactive_route_when_resting_flag_is_off(eh):
    service = _oms(**{_FLAG: False}, oms_v2_eh_entry_enabled=True)
    _set_quote(service, "CLRO", ask=5.56, bid=5.55)
    events = await service.process_trade_intent(
        _v2_open(_eh_webull_fanout_meta(level="5.5284"), symbol="CLRO")
    )
    assert events[-1].payload.status == "filled"
    order = _stored_order(service)
    assert order.payload["limit_price"] == "5.57"
    assert order.payload["oms_v2_eh_entry"] == "true"


@pytest.mark.asyncio
async def test_postmarket_webull_fanout_keeps_reactive_pricing(pm):
    service = _oms(**{_FLAG: True}, oms_v2_eh_entry_enabled=True)
    _set_quote(service, "CLRO", ask=5.56, bid=5.55)
    events = await service.process_trade_intent(
        _v2_open(_eh_webull_fanout_meta(level="5.5284"), symbol="CLRO")
    )
    assert events[-1].payload.status == "filled"
    order = _stored_order(service)
    assert order.payload["session"] == "PM"
    assert order.payload["limit_price"] == "5.57"
    assert order.payload["oms_v2_eh_entry"] == "true"
    assert "oms_v2_eh_resting_entry" not in order.payload


def test_regular_hours_webull_fanout_never_enters_eh_pricers(rth):
    service = _oms(**{_FLAG: True}, oms_v2_eh_entry_enabled=True)
    event = _v2_open(_eh_webull_fanout_meta(level="5.5284"), symbol="CLRO")
    assert service._v2_eh_reactive_entry_applies(event) is False
    assert service._v2_eh_resting_entry_applies(event) is False


@pytest.mark.asyncio
async def test_eh_band_cap_threshold_is_pinned(eh):
    """Threshold mutation guard on the 0.5% band: cap = 9.5*1.005 = 9.54750. ask 9.5480 is JUST past ->
    abandon; ask 9.5470 is JUST inside -> fills. Pins the band VALUE, not just the direction."""
    over = _oms(**{_FLAG: True})
    _set_quote(over, "FOO", ask=9.5480, bid=9.53)
    ev_over = await over.process_trade_intent(_v2_open(_eh_resting_meta()))
    assert ev_over[-1].payload.reason == "ASK_PAST_BAND"

    under = _oms(**{_FLAG: True})
    _set_quote(under, "FOO", ask=9.5470, bid=9.53)
    ev_under = await under.process_trade_intent(_v2_open(_eh_resting_meta()))
    assert [e.payload.status for e in ev_under] == ["accepted", "filled"]
    assert _stored_order(under).payload["limit_price"] == "9.54"


@pytest.mark.asyncio
async def test_eh_band_from_metadata_widens_cap(eh):
    """The band is taken from the intent's `resting_band_pct` (single source). At 2% the cap is 9.69, so
    an ask of 9.60 that abandoned at 0.5% now FILLS."""
    service = _oms(**{_FLAG: True})
    _set_quote(service, "FOO", ask=9.60, bid=9.58)
    events = await service.process_trade_intent(_v2_open(_eh_resting_meta(resting_band_pct="2.0")))
    assert [e.payload.status for e in events] == ["accepted", "filled"]
    order = _stored_order(service)
    assert order.payload["oms_v2_eh_resting_entry_cap"] == "9.6900"   # 9.5*1.02
    assert order.payload["limit_price"] == "9.60"


@pytest.mark.asyncio
async def test_eh_abandons_no_fresh_quote(eh):
    service = _oms(**{_FLAG: True})
    events = await service.process_trade_intent(_v2_open(_eh_resting_meta()))
    assert events[-1].payload.status == "rejected"
    assert events[-1].payload.reason == "NO_FRESH_QUOTE"
    assert _stored_order(service) is None


@pytest.mark.asyncio
async def test_eh_abandons_stale_quote(eh):
    service = _oms(**{_FLAG: True})
    _set_quote(service, "FOO", ask=9.52, bid=9.50, age_ms=10_000)  # 10s old > 2000ms
    events = await service.process_trade_intent(_v2_open(_eh_resting_meta()))
    assert events[-1].payload.status == "rejected"
    assert events[-1].payload.reason == "NO_FRESH_QUOTE"
    assert _stored_order(service) is None


@pytest.mark.asyncio
async def test_eh_abandons_missing_level(eh):
    service = _oms(**{_FLAG: True})
    _set_quote(service, "FOO", ask=9.52, bid=9.50)
    md = _eh_resting_meta()
    del md["resting_level"]
    del md["entry_price"]   # fail-closed: no band anchor
    events = await service.process_trade_intent(_v2_open(md))
    assert events[-1].payload.status == "rejected"
    assert events[-1].payload.reason == "MISSING_SIGNAL"
    assert _stored_order(service) is None


# --------------------------------------------------------------------------- byte-identical / exclusions

@pytest.mark.asyncio
async def test_flag_off_eh_byte_identical(eh):
    """Flag OFF -> the OMS does not touch the intent; the bot's plain limit-at-ask (9.50) fills as today."""
    service = _oms()  # flag default off
    _set_quote(service, "FOO", ask=9.40, bid=9.38)  # a different fresh ask that WOULD have re-priced
    events = await service.process_trade_intent(_v2_open(_eh_resting_meta()))
    assert [e.payload.status for e in events] == ["accepted", "filled"]
    order = _stored_order(service)
    assert order is not None
    assert order.payload["limit_price"] == "9.5000"   # untouched (bot's price)
    assert "oms_v2_eh_resting_entry" not in order.payload


@pytest.mark.asyncio
async def test_rth_flag_on_byte_identical(rth):
    """Flag ON but regular session -> the OMS never routes the EH resting builder (RTH is the broker
    stop-limit path). A RTH resting STOP_LIMIT is left untouched by this builder."""
    service = _oms(**{_FLAG: True})
    _set_quote(service, "FOO", ask=9.52, bid=9.50)
    md = {"path": "ATR Flip", "atr_variant": "CW-v2-resting", "resting_entry": "true",
          "order_type": "STOP_LIMIT", "stop_price": "9.5000", "limit_price": "9.5475",
          "entry_price": "9.5000", "reference_price": "9.5000"}
    await service.process_trade_intent(_v2_open(md))
    order = _stored_order(service)
    assert order is not None
    assert "oms_v2_eh_resting_entry" not in order.payload
    assert order.payload["order_type"] == "STOP_LIMIT"   # unchanged broker stop-limit


@pytest.mark.asyncio
async def test_reactive_open_not_touched_by_resting_builder(eh):
    """A REACTIVE EH open (no eh_resting tag) is NOT handled by the resting builder (it carries no
    eh_resting=true), so this builder leaves it alone."""
    service = _oms(**{_FLAG: True})  # resting flag on, reactive flag off
    _set_quote(service, "FOO", ask=1.92, bid=1.90)
    md = {"path": "ATR Flip", "atr_variant": "CW-v2", "entry_price": "2.00", "order_type": "limit",
          "session": "AM", "extended_hours": "true", "limit_price": "1.92", "reference_price": "1.92",
          "price_source": "ask"}
    await service.process_trade_intent(_v2_open(md))
    order = _stored_order(service)
    assert order is not None
    assert "oms_v2_eh_resting_entry" not in order.payload
    assert order.payload["limit_price"] == "1.92"   # untouched by the resting builder


@pytest.mark.asyncio
async def test_non_v2_untouched(eh):
    service = _oms(**{_FLAG: True})
    _set_quote(service, "BAR", ask=9.52, bid=9.50)
    event = TradeIntentEvent(
        source_service="macd_30s",
        payload=TradeIntentPayload(
            strategy_code="macd_30s",
            broker_account_name="paper:macd_30s",
            symbol="BAR",
            side="buy",
            quantity=Decimal("5"),
            intent_type="open",
            reason="MACD_OPEN",
            metadata={"order_type": "limit", "session": "AM", "extended_hours": "true",
                      "limit_price": "9.50", "reference_price": "9.50", "eh_resting": "true",
                      "resting_level": "9.50"},   # even with the tag, non-v2 is excluded
        ),
    )
    await service.process_trade_intent(event)
    order = _stored_order(service)
    assert order is not None
    assert "oms_v2_eh_resting_entry" not in order.payload
    assert order.payload["limit_price"] == "9.50"
