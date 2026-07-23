"""Webull mirror-on-FILL — mirror a CONFIRMED primary Schwab v2 buy-open FILL to a SECOND
(Webull) account as a native-OCO combo (MARKET master + take-profit + stop-loss).

Replaces the old on-SUBMIT mirror (`_maybe_mirror_v2_open`, removed): the resting v2 entry
rests until the up-cross, so mirroring at placement would enter Webull early/wrong, and Webull
structurally refuses a buy-STOP master. The mirror now fires on the Schwab fill observed by
`sync_broker_orders`. See docs/webull-mirror-on-fill-design.md.

Load-bearing properties proven here:
- transform: a fresh-ask fill -> MARKET master + bracket, target≈ask*1.02, stop≈ask*0.95, NO
  stop_price key on the request;
- fallback: no fresh quote -> exits anchor off the Schwab fill price;
- flag OFF -> byte-identical dormant: submit_order is NEVER called;
- non-eligible fills (sell, non-schwab_1m_v2, wrong account) never mirror;
- collision guard: the Webull account already holding the symbol -> skip, no submit;
- safety: a Webull submit_order that RAISES is swallowed and never propagates.

Harness mirrors the (removed) test_oms_v2_webull_mirror.py: a real OmsRiskService over an
in-memory SQLite session_factory with an injectable, request-capturing broker adapter.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import ArmedHardStop, OmsRiskService
from project_mai_tai.settings import Settings

PRIMARY = "paper:schwab_1m_v2"
WEBULL = "live:v2_webull"


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


async def _noop_sync_broker_state(*, account_names=None):
    del account_names
    return None


class _CaptureAdapter:
    """Records every submit_order request and returns a simple 'accepted' report. Can be told
    to RAISE on submit (to prove the mirror swallows a Webull failure)."""

    def __init__(self, *, raise_on_submit: bool = False) -> None:
        self.requests: list[OrderRequest] = []
        self._raise = raise_on_submit
        self._n = 0

    async def submit_order(self, request: OrderRequest) -> list[ExecutionReport]:
        self.requests.append(request)
        if self._raise:
            raise RuntimeError("webull down")
        self._n += 1
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id=f"wb-{self._n}",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request: OrderRequest):
        return None

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


class _SyncFillAdapter:
    """Submits leave the order 'accepted' (open); fetch_order_update returns a 'filled' report
    for the PRIMARY buy-open so `sync_broker_orders` drives the on-fill path. Records every
    (account, symbol) submit so we can assert whether / what the Webull mirror submitted."""

    def __init__(self, *, fill_price: str = "2.60", fill_qty: str = "10") -> None:
        self.submits: list[tuple[str, str]] = []
        self.requests: list[OrderRequest] = []
        self._fp = Decimal(fill_price)
        self._fq = Decimal(fill_qty)
        self._n = 0

    async def submit_order(self, request: OrderRequest) -> list[ExecutionReport]:
        self.submits.append((request.broker_account_name, request.symbol))
        self.requests.append(request)
        self._n += 1
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id=f"ord-{self._n}",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata=dict(request.metadata),
            )
        ]

    async def fetch_order_update(self, request: OrderRequest):
        # Only the PRIMARY buy-open fills; any other order (the Webull leg) stays 'accepted'.
        if (
            request.broker_account_name == PRIMARY
            and str(request.side).lower() == "buy"
            and str(request.intent_type).lower() == "open"
        ):
            return ExecutionReport(
                event_type="filled",
                client_order_id=request.client_order_id,
                broker_order_id=request.metadata.get("broker_order_id") or "ord-1",
                broker_fill_id="fill-1",
                symbol=request.symbol,
                side="buy",
                intent_type="open",
                quantity=self._fq,
                filled_quantity=self._fq,
                fill_price=self._fp,
                reason=request.reason,
                metadata={},
            )
        return None

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _oms(*, adapter, mirror_on: bool = True, webull_account: str = WEBULL) -> OmsRiskService:
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_v2_exit_management_enabled=True,
            strategy_schwab_1m_v2_account_name=PRIMARY,
            strategy_schwab_1m_v2_webull_account_name=webull_account,
            strategy_schwab_1m_v2_webull_mirror_enabled=mirror_on,
        ),
        redis_client=_FakeRedis(),
        session_factory=_session_factory(),
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]
    return service


def _seed_fresh_ask(service: OmsRiskService, symbol: str, ask: float, *, age_ms: float = 0.0) -> None:
    service._latest_quotes_by_symbol[symbol] = {
        "bid": ask - 0.02,
        "ask": ask,
        "received_at": datetime.now(timezone.utc) - timedelta(milliseconds=age_ms),
    }


def _v2_open(*, symbol: str = "VSME", qty: str = "10", account: str = PRIMARY,
             strategy: str = "schwab_1m_v2", side: str = "buy",
             intent_type: str = "open") -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="schwab_1m_v2",
        payload=TradeIntentPayload(
            strategy_code=strategy,
            broker_account_name=account,
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            quantity=Decimal(qty),
            intent_type=intent_type,  # type: ignore[arg-type]
            reason="ENTRY_ATR_FLIP",
            metadata={"path": "ATR Flip", "reference_price": "2.50"},
        ),
    )


def _orders(service: OmsRiskService) -> list[BrokerOrder]:
    with service.session_factory() as session:
        return list(session.scalars(select(BrokerOrder)).all())


# --------------------------------------------------------------------------- direct-call tests
# The transform / anchor / guard / collision / safety logic lives in _mirror_v2_fill_to_webull;
# these call it directly with explicit params and capture the built OrderRequest.

@pytest.mark.asyncio
async def test_transform_market_master_and_bracket_from_fresh_ask():
    """A fresh ask quote -> MARKET master + native-OCO bracket, exits anchored to the ASK:
    target≈ask*1.02, stop≈ask*0.95, and NO stop_price key (the resting trigger is dropped)."""
    adapter = _CaptureAdapter()
    service = _oms(adapter=adapter, mirror_on=True)
    _seed_fresh_ask(service, "VSME", ask=3.00)

    await service._mirror_v2_fill_to_webull(
        symbol="VSME", quantity=Decimal("10"), schwab_fill_price=2.50,
        source_metadata={"path": "ATR Flip"},
    )

    assert len(adapter.requests) == 1
    req = adapter.requests[0]
    assert req.broker_account_name == WEBULL
    assert req.order_type == "market"
    md = req.metadata
    assert md["bracket"] == "true"
    assert md["native_oco_bracket"] == "true"
    assert md["bracket_entry_type"] == "MARKET"
    # Anchored to the ASK (3.00), NOT the Schwab fill (2.50).
    assert float(md["bracket_target_price"]) == pytest.approx(3.00 * 1.02, rel=1e-6)
    assert float(md["bracket_stop_price"]) == pytest.approx(3.00 * 0.95, rel=1e-6)
    assert md["path"] == "ATR Flip"
    # A MARKET master carries no limit and no resting stop trigger.
    assert "stop_price" not in md
    assert "limit_price" not in md


@pytest.mark.asyncio
async def test_fallback_to_schwab_fill_price_without_fresh_quote():
    """No quote in _latest_quotes_by_symbol -> exits anchor off the Schwab fill price."""
    adapter = _CaptureAdapter()
    service = _oms(adapter=adapter, mirror_on=True)
    # no _seed_fresh_ask -> no quote for VSME

    await service._mirror_v2_fill_to_webull(
        symbol="VSME", quantity=Decimal("10"), schwab_fill_price=2.50,
        source_metadata={"path": "ATR Flip"},
    )

    assert len(adapter.requests) == 1
    md = adapter.requests[0].metadata
    assert float(md["bracket_target_price"]) == pytest.approx(2.50 * 1.02, rel=1e-6)
    assert float(md["bracket_stop_price"]) == pytest.approx(2.50 * 0.95, rel=1e-6)


@pytest.mark.asyncio
async def test_stale_quote_falls_back_to_schwab_fill_price():
    """A quote older than oms_v2_exit_quote_max_age_ms is NOT trusted -> Schwab-fill anchor."""
    adapter = _CaptureAdapter()
    service = _oms(adapter=adapter, mirror_on=True)
    _seed_fresh_ask(service, "VSME", ask=3.00, age_ms=60_000)  # 60s old >> 5000ms default

    await service._mirror_v2_fill_to_webull(
        symbol="VSME", quantity=Decimal("10"), schwab_fill_price=2.50,
        source_metadata={"path": "ATR Flip"},
    )

    md = adapter.requests[0].metadata
    assert float(md["bracket_target_price"]) == pytest.approx(2.50 * 1.02, rel=1e-6)


@pytest.mark.asyncio
async def test_flag_off_is_dormant_no_submit():
    """Flag OFF -> byte-identical dormant: submit_order is NEVER called."""
    adapter = _CaptureAdapter()
    service = _oms(adapter=adapter, mirror_on=False)
    _seed_fresh_ask(service, "VSME", ask=3.00)

    await service._mirror_v2_fill_to_webull(
        symbol="VSME", quantity=Decimal("10"), schwab_fill_price=2.50,
        source_metadata={"path": "ATR Flip"},
    )

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_unset_webull_account_refuses_to_mirror():
    """Flag ON but the Webull account is UNSET -> no-op + warn, never fan out to a default."""
    adapter = _CaptureAdapter()
    service = _oms(adapter=adapter, mirror_on=True, webull_account="")

    await service._mirror_v2_fill_to_webull(
        symbol="VSME", quantity=Decimal("10"), schwab_fill_price=2.50, source_metadata={},
    )

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_collision_guard_skips_when_webull_account_holds_symbol():
    """The Webull account already holds the symbol (an armed native stop) -> SKIP, no submit —
    v2 never fights ORB for the same name on the shared account."""
    adapter = _CaptureAdapter()
    service = _oms(adapter=adapter, mirror_on=True)
    _seed_fresh_ask(service, "VSME", ask=3.00)
    service._armed_hard_stops[("orb", WEBULL, "VSME")] = ArmedHardStop(
        strategy_code="orb", broker_account_name=WEBULL, symbol="VSME",
        quantity=Decimal("5"), entry_price=Decimal("2.50"), stop_loss_pct=1.5,
        stop_price=Decimal("2.46"), quote_max_age_ms=2000, initial_panic_buffer_pct=0.5,
    )

    await service._mirror_v2_fill_to_webull(
        symbol="VSME", quantity=Decimal("10"), schwab_fill_price=2.50, source_metadata={},
    )

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_webull_submit_raise_is_swallowed():
    """A Webull submit_order that RAISES must NOT propagate — the primary Schwab leg is already
    committed and a Webull failure can never unwind it."""
    adapter = _CaptureAdapter(raise_on_submit=True)
    service = _oms(adapter=adapter, mirror_on=True)
    _seed_fresh_ask(service, "VSME", ask=3.00)

    # Must return normally (not raise).
    result = await service._mirror_v2_fill_to_webull(
        symbol="VSME", quantity=Decimal("10"), schwab_fill_price=2.50, source_metadata={},
    )
    assert result is None
    assert len(adapter.requests) == 1  # it attempted the submit, then swallowed the error


# --------------------------------------------------------------------------- sync-wiring tests
# The eligibility guard + candidate plumbing live in sync_broker_orders; these drive a real
# resting fill through it end-to-end.

@pytest.mark.asyncio
async def test_sync_fires_mirror_once_on_v2_primary_fill():
    """A v2-primary buy-open that FILLS during sync_broker_orders -> exactly one Webull combo
    submit (MARKET + bracket). Proves Edit 2 (fill-path wiring) + Edit 3 (transform) together."""
    adapter = _SyncFillAdapter(fill_price="2.60")
    service = _oms(adapter=adapter, mirror_on=True)
    _seed_fresh_ask(service, "VSME", ask=2.65)

    # Seed an OPEN primary order (adapter returns 'accepted' on submit -> stays open).
    await service.process_trade_intent(_v2_open(symbol="VSME"))
    assert adapter.submits == [(PRIMARY, "VSME")]

    await service.sync_broker_orders(account_names=[PRIMARY])

    webull_submits = [s for s in adapter.submits if s[0] == WEBULL]
    assert webull_submits == [(WEBULL, "VSME")]
    webull_req = [r for r in adapter.requests if r.broker_account_name == WEBULL][0]
    assert webull_req.order_type == "market"
    assert webull_req.metadata["bracket"] == "true"
    assert webull_req.metadata["bracket_entry_type"] == "MARKET"
    # Anchored to the fresh ask (2.65).
    assert float(webull_req.metadata["bracket_target_price"]) == pytest.approx(2.65 * 1.02, rel=1e-6)


@pytest.mark.asyncio
async def test_sync_double_run_places_mirror_exactly_once():
    """record_fill_if_needed is idempotent + a filled order is terminal -> a second sync does
    NOT re-place the Webull leg (no double-place across reconcile cycles)."""
    adapter = _SyncFillAdapter(fill_price="2.60")
    service = _oms(adapter=adapter, mirror_on=True)
    _seed_fresh_ask(service, "VSME", ask=2.65)

    await service.process_trade_intent(_v2_open(symbol="VSME"))
    await service.sync_broker_orders(account_names=[PRIMARY])
    await service.sync_broker_orders(account_names=[PRIMARY])

    webull_submits = [s for s in adapter.submits if s[0] == WEBULL]
    assert webull_submits == [(WEBULL, "VSME")]  # exactly one, not two


@pytest.mark.asyncio
async def test_sync_does_not_mirror_non_v2_strategy_fill():
    """A non-schwab_1m_v2 fill is not eligible -> no Webull mirror even with the flag on."""
    adapter = _SyncFillAdapter(fill_price="1.20")

    # An adapter variant that fills a macd_1m buy-open on its own account.
    class _MacdFillAdapter(_SyncFillAdapter):
        async def fetch_order_update(self, request):
            if str(request.side).lower() == "buy" and str(request.intent_type).lower() == "open":
                return ExecutionReport(
                    event_type="filled", client_order_id=request.client_order_id,
                    broker_order_id=request.metadata.get("broker_order_id") or "ord-1",
                    broker_fill_id="fill-1", symbol=request.symbol, side="buy",
                    intent_type="open", quantity=self._fq, filled_quantity=self._fq,
                    fill_price=self._fp, reason=request.reason, metadata={},
                )
            return None

    adapter = _MacdFillAdapter(fill_price="1.20")
    service = _oms(adapter=adapter, mirror_on=True)
    _seed_fresh_ask(service, "BFRG", ask=1.25)

    macd_open = TradeIntentEvent(
        source_service="strategy-engine",
        payload=TradeIntentPayload(
            strategy_code="macd_1m", broker_account_name="paper:macd_1m", symbol="BFRG",
            side="buy", quantity=Decimal("100"), intent_type="open",
            reason="ENTRY_P3_MACD_SURGE", metadata={"reference_price": "1.15"},
        ),
    )
    await service.process_trade_intent(macd_open)
    await service.sync_broker_orders(account_names=["paper:macd_1m"])

    assert all(s[0] != WEBULL for s in adapter.submits)
