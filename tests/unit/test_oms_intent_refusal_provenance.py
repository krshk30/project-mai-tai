"""Durable provenance for rejected trade-intent rows.

W2's 2026-08-28 control had 22 v2 open intents.  WHLR was deliberately
aborted by the local max-cross policy, while QNRX was skipped by the Webull
collision guard before any broker outcome existed.  Both rows nevertheless
stored only ``status='rejected'``.  These tests drive those two paths and the
broker-report path separately; no historical row is backfilled.
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
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    AccountPosition,
    BrokerOrder,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.settings import Settings


class _FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, object]]] = []

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs: object) -> str:
        del kwargs
        self.entries.append((stream, json.loads(fields["data"])))
        return "1-0"

    async def get(self, key: str) -> None:
        del key
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        del key, value, ex
        return True

    async def xread(self, offsets: object, block: int = 0, count: int = 0) -> list[object]:
        del offsets, block, count
        return []

    async def aclose(self) -> None:
        return None


class _RejectingAdapter:
    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.requests: list[object] = []

    async def submit_order(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return [
            ExecutionReport(
                event_type="rejected",
                client_order_id=request.client_order_id,
                broker_order_id="venue-reject-1" if self.origin == "broker" else None,
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason="VENUE_REFUSAL" if self.origin == "broker" else "CLIENT_PREFLIGHT",
                metadata=dict(request.metadata),
                origin=self.origin,  # type: ignore[arg-type]
            )
        ]

    async def fetch_order_update(self, request):  # type: ignore[no-untyped-def]
        del request
        return None

    async def list_account_positions(self, broker_account_name: str) -> list[object]:
        del broker_account_name
        return []


class _WebullClosingOnlyAdapter:
    def __init__(self, *, reject_close: bool = False) -> None:
        self.reject_close = reject_close
        self.requests: list[object] = []

    async def submit_order(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        rejected = request.intent_type == "open" or self.reject_close
        return [
            ExecutionReport(
                event_type="rejected" if rejected else "accepted",
                client_order_id=request.client_order_id,
                broker_order_id=f"webull-{len(self.requests)}",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=(
                    "Webull order rejected: CAN_NOT_CREATE_A_OPEN_ORDER "
                    "CAN_NOT_CREATE_A_OPEN_ORDER (http 417)"
                    if rejected
                    else request.reason
                ),
                metadata=dict(request.metadata),
                origin="broker",
            )
        ]

    async def fetch_order_update(self, request):  # type: ignore[no-untyped-def]
        del request
        return None

    async def list_account_positions(self, broker_account_name: str) -> list[object]:
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


def _service(
    factory: sessionmaker[Session],
    *,
    adapter: object | None = None,
    **settings: object,
) -> OmsRiskService:
    return OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            **settings,
        ),
        redis_client=_FakeRedis(),
        session_factory=factory,
        broker_adapter=adapter or SimulatedBrokerAdapter(),  # type: ignore[arg-type]
    )


def _v2_open(
    symbol: str,
    *,
    account: str = "live:orb",
    metadata: dict[str, str] | None = None,
) -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="schwab-1m-v2",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name=account,
            symbol=symbol,
            side="buy",
            quantity=Decimal("1"),
            intent_type="open",
            reason="schwab_1m_v2 ATR Flip CW-v2",
            metadata=metadata or {"reference_price": "1.00"},
        ),
    )


def _v2_close(symbol: str, *, account: str = "live:orb") -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="oms-risk",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name=account,
            symbol=symbol,
            side="sell",
            quantity=Decimal("1"),
            intent_type="close",
            reason="CW_HARD_STOP",
            metadata={"order_type": "market", "reference_price": "1.00"},
        ),
    )


def _seed_owned_position(
    factory: sessionmaker[Session], service: OmsRiskService, symbol: str
) -> None:
    with factory() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2")
        account = service.store.ensure_broker_account(
            session,
            "live:orb",
            provider="webull",
            environment="live",
        )
        session.add_all(
            [
                VirtualPosition(
                    strategy_id=strategy.id,
                    broker_account_id=account.id,
                    symbol=symbol,
                    quantity=Decimal("1"),
                    average_price=Decimal("1.00"),
                    realized_pnl=Decimal("0"),
                ),
                AccountPosition(
                    broker_account_id=account.id,
                    symbol=symbol,
                    quantity=Decimal("1"),
                    average_price=Decimal("1.00"),
                    market_value=Decimal("1.00"),
                ),
            ]
        )
        session.commit()


def _stored_intent(factory: sessionmaker[Session], symbol: str) -> TradeIntent:
    with factory() as session:
        intent = session.scalar(select(TradeIntent).where(TradeIntent.symbol == symbol))
        assert intent is not None
        session.expunge(intent)
        return intent


@pytest.fixture(autouse=True)
def _fillable_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(OmsRiskService, "_market_is_fillable", lambda self, now=None: True)


@pytest.mark.asyncio
async def test_webull_closing_only_latch_suppresses_opens_but_never_a_close() -> None:
    """BDRX 09-11: learn once, stop retrying opens, and preserve every way out."""
    factory = _session_factory()
    adapter = _WebullClosingOnlyAdapter()
    service = _service(
        factory,
        adapter=adapter,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    service._fanout_webull_collision_reason = lambda **kwargs: None  # type: ignore[method-assign]

    first = await service.process_trade_intent(_v2_open("BDRX"))
    assert first[-1].payload.status == "rejected"
    assert [request.intent_type for request in adapter.requests] == ["open"]

    second = await service.process_trade_intent(_v2_open("BDRX"))
    assert second[-1].payload.reason == "webull_ineligible_cached"
    assert [request.intent_type for request in adapter.requests] == ["open"]

    _seed_owned_position(factory, service, "BDRX")
    close = await service.process_trade_intent(_v2_close("BDRX"))
    assert close[-1].payload.status == "accepted"
    assert [request.intent_type for request in adapter.requests] == ["open", "close"]


@pytest.mark.asyncio
async def test_a_rejected_close_can_never_create_the_open_only_latch() -> None:
    factory = _session_factory()
    adapter = _WebullClosingOnlyAdapter(reject_close=True)
    service = _service(
        factory,
        adapter=adapter,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    _seed_owned_position(factory, service, "EXIT")

    close = await service.process_trade_intent(_v2_close("EXIT"))

    assert close[-1].payload.status == "rejected"
    with factory() as session:
        account = service.store.ensure_broker_account(
            session,
            "live:orb",
            provider="webull",
            environment="live",
        )
        assert (
            service.store.get_webull_ineligible_entry(
                session,
                broker_account_id=account.id,
                symbol="EXIT",
                session_date=service._current_session_day(),
            )
            is None
        )


@pytest.mark.asyncio
async def test_20260828_replay_resolves_whlr_and_qnrx_over_22_v2_open_intents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replay grades 2 known refusals over the named 22-intent population.

    It does not update production history.  The 20 unaffected rows keep no
    refusal fields, just as historical rows must remain ``COULD_NOT_TELL``.
    """

    monkeypatch.setattr(oms_service, "_is_regular_market_session", lambda now=None: False)
    monkeypatch.setattr(oms_service, "_extended_hours_session", lambda now=None: "AM")
    factory = _session_factory()
    service = _service(
        factory,
        oms_v2_eh_entry_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    service._fanout_webull_collision_reason = lambda **kwargs: None  # type: ignore[method-assign]
    service._latest_quotes_by_symbol["WHLR"] = {
        "ask": 1.55,
        "bid": 1.54,
        "received_at": datetime.now(UTC),
    }
    whlr = await service.process_trade_intent(
        _v2_open(
            "WHLR",
            metadata={
                "entry_price": "1.5135",
                "reference_price": "1.5135",
                "order_type": "limit",
                "session": "AM",
                "extended_hours": "true",
                "fanout_leg": "webull",
            },
        )
    )
    assert whlr[-1].payload.reason == "ASK_PAST_CROSS_CAP"

    service._fanout_webull_collision_reason = (  # type: ignore[method-assign]
        lambda **kwargs: "fanout_webull_collision_managed"
        if kwargs["symbol"] == "QNRX"
        else None
    )
    qnrx = await service.process_trade_intent(_v2_open("QNRX"))
    assert qnrx[-1].payload.reason == "fanout_webull_collision_managed"

    window_start = datetime(2026, 8, 28, 4, 0, tzinfo=UTC)
    with factory() as session:
        rows = session.scalars(select(TradeIntent).order_by(TradeIntent.symbol)).all()
        assert {row.symbol for row in rows} == {"QNRX", "WHLR"}
        for offset, row in enumerate(rows):
            row.created_at = window_start + timedelta(seconds=offset)

        store = OmsStore()
        strategy = store.ensure_strategy(session, "schwab_1m_v2")
        account = store.ensure_broker_account(
            session,
            "live:schwab_1m_v2",
            provider="schwab",
            environment="live",
        )
        for index in range(20):
            control = store.create_trade_intent(
                session,
                strategy=strategy,
                broker_account=account,
                event=_v2_open(
                    f"CTL{index:02d}",
                    account=account.name,
                ),
            )
            control.status = "submitted"
            control.created_at = window_start + timedelta(minutes=index + 1)
        session.commit()

        window = session.scalars(
            select(TradeIntent).where(
                TradeIntent.created_at >= window_start,
                TradeIntent.created_at < window_start + timedelta(days=1),
            )
        ).all()
        assert len(window) == 22
        by_symbol = {row.symbol: row for row in window}
        assert by_symbol["WHLR"].payload["refusal_origin"] == "client_abort"
        assert by_symbol["WHLR"].payload["refusal_code"] == "ASK_PAST_CROSS_CAP"
        assert by_symbol["QNRX"].payload["refusal_origin"] == "skipped_before_submit"
        assert (
            by_symbol["QNRX"].payload["refusal_code"]
            == "fanout_webull_collision_managed"
        )
        attributable = [row for row in window if "refusal_origin" in (row.payload or {})]
        assert len(attributable) == 2
        assert sum("refusal_origin" not in (row.payload or {}) for row in window) == 20
        assert session.scalars(select(BrokerOrder)).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report_origin", "expected_origin", "expected_code"),
    [
        ("broker", "broker_reject", "VENUE_REFUSAL"),
        ("client", "client_abort", "CLIENT_PREFLIGHT"),
        ("unknown", "could_not_tell", "CLIENT_PREFLIGHT"),
    ],
)
async def test_execution_report_origin_reaches_the_trade_intent_row(
    report_origin: str,
    expected_origin: str,
    expected_code: str,
) -> None:
    factory = _session_factory()
    adapter = _RejectingAdapter(report_origin)
    service = _service(factory, adapter=adapter)

    await service.process_trade_intent(
        _v2_open(
            "BROKR",
            account="live:schwab_1m_v2",
            metadata={"reference_price": "1.00"},
        )
    )

    intent = _stored_intent(factory, "BROKR")
    assert intent.status == "rejected"
    assert intent.payload["refusal_origin"] == expected_origin
    assert intent.payload["refusal_code"] == expected_code
    assert len(adapter.requests) == 1


@pytest.mark.parametrize(
    ("report_origin", "expected_origin"),
    [
        ("broker", "broker_reject"),
        (None, "could_not_tell"),
        ("", "could_not_tell"),
        ("future_unrecognised_origin", "could_not_tell"),
    ],
)
def test_report_origin_fallback_never_blames_the_broker(
    report_origin: str | None,
    expected_origin: str,
) -> None:
    """Only an explicit broker origin may become ``broker_reject``."""

    intent = TradeIntent(payload={})
    report = ExecutionReport(
        event_type="rejected",
        client_order_id="fallback-origin-control",
        reason="ORIGIN_CONTROL",
        origin=report_origin,  # type: ignore[arg-type]
    )

    OmsStore().mark_intent_from_report(intent, report)

    assert intent.status == "rejected"
    assert intent.payload["refusal_origin"] == expected_origin
    assert intent.payload["refusal_code"] == "ORIGIN_CONTROL"
    if expected_origin == "could_not_tell":
        assert intent.payload["refusal_origin"] != "broker_reject"
        assert intent.payload["refusal_origin"] != "client_abort"


def test_legacy_rejected_row_without_provenance_stays_could_not_tell() -> None:
    factory = _session_factory()
    store = OmsStore()
    with factory() as session:
        strategy = store.ensure_strategy(session, "schwab_1m_v2")
        account = store.ensure_broker_account(
            session,
            "live:schwab_1m_v2",
            provider="schwab",
            environment="live",
        )
        legacy = store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=account,
            event=_v2_open("LEGACY", account=account.name),
        )
        store.mark_intent_status(legacy, "rejected")
        session.commit()

        assert legacy.status == "rejected"
        assert "refusal_origin" not in legacy.payload
        assert "refusal_code" not in legacy.payload


def test_refusal_origin_is_a_closed_vocabulary() -> None:
    intent = TradeIntent(payload={})
    with pytest.raises(ValueError, match="unsupported intent refusal origin"):
        OmsStore().mark_intent_refused(intent, origin="maybe_broker", code="NOPE")
    assert intent.payload == {}
