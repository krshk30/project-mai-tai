"""CONF3: one Schwab confirmation decision closes each managed broker leg once."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import (
    BrokerPositionSnapshot,
    ExecutionReport,
    ExitPairReleaseResult,
)
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, OmsManagedPosition, TradeIntent
from project_mai_tai.oms import service as service_module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from tests.webull_confirmation_exit_fixtures import (
    bq_170729_not_tradable_reject,
    cancelled_leg,
    filled_leg,
    gipr_170707_detail_rate_limited,
    gipr_170707_order_cannot_cancel,
    gipr_180510_order_cannot_cancel,
    gipr_180511_detail_rate_limited,
    imcc_174604_cancelled_and_rate_limited,
    lgps_133416_malformed_client_order_id_reject,
    unknown_answer,
    working_leg_after_cancel_request,
    ztg_195912_no_position_reject,
)

SCHWAB = "live:schwab_1m_v2"
WEBULL = "live:orb"
SYMBOL = "IMRN"


class _FakeRedis:
    async def xadd(self, *args, **kwargs):
        return b"1-1"


class _CapturedLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _record(self, message: str, *args, **kwargs) -> None:
        self.lines.append(message % args if args else message)

    info = _record
    warning = _record
    error = _record
    exception = _record


class _FanoutAdapter:
    def __init__(self, *, reject_accounts: set[str] | None = None) -> None:
        self.reject_accounts = reject_accounts or set()
        self.submitted = []
        self.native_release_calls: list[tuple[str, str]] = []
        self.cancel_pair_calls: list[tuple[str, str, str]] = []
        self.position_state: dict[str, Decimal] = {SCHWAB: Decimal("1"), WEBULL: Decimal("1")}
        self.release_results: list[ExitPairReleaseResult] = []
        self.submit_results: list[list[ExecutionReport] | BaseException] = []

    async def submit_order(self, request):
        self.submitted.append(request)
        if self.submit_results:
            result = self.submit_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return [
                replace(
                    report,
                    client_order_id=request.client_order_id,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                )
                for report in result
            ]
        rejected = request.broker_account_name in self.reject_accounts
        return [
            ExecutionReport(
                event_type="rejected" if rejected else "filled",
                origin="broker",
                client_order_id=request.client_order_id,
                broker_order_id=None if rejected else f"exit-{request.broker_account_name}",
                broker_fill_id=None if rejected else f"fill-{request.broker_account_name}",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                filled_quantity=Decimal("0") if rejected else request.quantity,
                fill_price=None if rejected else Decimal("9.90"),
                reason="refused for controlled test" if rejected else "filled",
                metadata=dict(request.metadata),
            )
        ]

    async def release_native_oco_for_close(
        self, broker_account_name: str, entry_broker_order_id: str
    ) -> str:
        self.native_release_calls.append((broker_account_name, entry_broker_order_id))
        return "released"

    async def cancel_exit_pair(
        self, *, broker_account_name: str, symbol: str, base_client_order_id: str
    ) -> list[ExecutionReport]:
        self.cancel_pair_calls.append((broker_account_name, symbol, base_client_order_id))
        return [
            ExecutionReport(
                event_type="cancelled",
                origin="broker",
                client_order_id=f"{base_client_order_id}{suffix}",
                symbol=symbol,
                side="sell",
                intent_type="cancel",
                reason="cancelled",
            )
            for suffix in ("T", "S")
        ]

    async def release_exit_pair_for_close(
        self, *, broker_account_name: str, symbol: str, base_client_order_id: str
    ) -> ExitPairReleaseResult:
        if self.release_results:
            self.cancel_pair_calls.append((broker_account_name, symbol, base_client_order_id))
            return self.release_results.pop(0)
        reports = tuple(
            await self.cancel_exit_pair(
                broker_account_name=broker_account_name,
                symbol=symbol,
                base_client_order_id=base_client_order_id,
            )
        )
        filled = tuple(report for report in reports if report.event_type == "filled")
        if len(filled) == 1:
            return ExitPairReleaseResult(outcome="resolved_by_fill", reports=reports)
        if len(reports) != 2:
            return ExitPairReleaseResult(outcome="unanswerable", reports=reports)
        if all(report.event_type == "cancelled" for report in reports):
            return ExitPairReleaseResult(outcome="released", reports=reports)
        if any(report.metadata.get("cancel_outcome") == "could_not_tell" for report in reports):
            return ExitPairReleaseResult(outcome="unanswerable", reports=reports)
        return ExitPairReleaseResult(outcome="reserved", reports=reports)

    async def list_account_positions(self, broker_account_name: str):
        return [
            BrokerPositionSnapshot(
                broker_account_name=broker_account_name,
                symbol=SYMBOL,
                quantity=self.position_state[broker_account_name],
                average_price=Decimal("10"),
            )
        ]


class _DelayedFillAdapter(_FanoutAdapter):
    async def submit_order(self, request):
        self.submitted.append(request)
        return [
            ExecutionReport(
                event_type="accepted",
                origin="broker",
                client_order_id=request.client_order_id,
                broker_order_id=f"exit-{request.broker_account_name}",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                filled_quantity=Decimal("0"),
                reason="accepted",
                metadata={},
            )
        ]

    async def fetch_order_update(self, request):
        return ExecutionReport(
            event_type="filled",
            origin="broker",
            client_order_id=request.client_order_id,
            broker_order_id=f"exit-{request.broker_account_name}",
            broker_fill_id=f"fill-{request.broker_account_name}",
            symbol=request.symbol,
            side=request.side,
            intent_type=request.intent_type,
            quantity=request.quantity,
            filled_quantity=request.quantity,
            fill_price=Decimal("9.90"),
            reason="filled",
            metadata={},
        )


def _make_sf() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name not in ("market_trade_ticks", "market_quote_ticks")
    ]
    Base.metadata.create_all(engine, tables=tables)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _service(*, fanout: bool, adapter: _FanoutAdapter) -> tuple[OmsRiskService, sessionmaker]:
    sf = _make_sf()
    settings = Settings(
        strategy_schwab_1m_v2_account_name=SCHWAB,
        strategy_schwab_1m_v2_webull_account_name=WEBULL,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=fanout,
        oms_v2_exit_management_enabled=True,
        oms_v2_exit_close_on_fill_enabled=True,
        oms_v2_exit_release_reservation_enabled=True,
    )
    service = OmsRiskService(
        settings,
        redis_client=_FakeRedis(),
        session_factory=sf,
        broker_adapter=adapter,
    )
    with sf() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2", name="v2")
        for account_name, provider in ((SCHWAB, "schwab"), (WEBULL, "webull")):
            account = service.store.ensure_broker_account(
                session, account_name, provider=provider, environment="live"
            )
            row = service.store.create_managed_position(
                session,
                strategy_code="schwab_1m_v2",
                broker_account_name=account_name,
                symbol=SYMBOL,
                entry_price=Decimal("10"),
                quantity=1,
                entry_path="RESTING",
            )
            service._managed_v2_symbols.add((account_name, SYMBOL))
            if account_name == WEBULL:
                session.add(
                    BrokerOrder(
                        intent_id=None,
                        strategy_id=strategy.id,
                        broker_account_id=account.id,
                        client_order_id="webull-stop-limit-entry",
                        broker_order_id="webull-entry-1",
                        symbol=SYMBOL,
                        side="buy",
                        order_type="stop_limit",
                        time_in_force="day",
                        quantity=Decimal("1"),
                        status="filled",
                        payload={
                            "fanout_leg": "webull",
                            "native_oco_bracket": "false",
                            "cw_entry_slot": "first",
                            "managed_row_id": str(row.id),
                        },
                    )
                )
        session.commit()
    return service, sf


def _open_row_ids(sf: sessionmaker) -> dict[str, str]:
    with sf() as session:
        rows = session.scalars(
            select(OmsManagedPosition).where(OmsManagedPosition.status == "open")
        ).all()
        return {row.broker_account_name: str(row.id) for row in rows}


def _sell_accounts(sf: sessionmaker) -> list[str]:
    with sf() as session:
        rows = session.execute(
            select(BrokerAccount.name)
            .join(TradeIntent, TradeIntent.broker_account_id == BrokerAccount.id)
            .where(TradeIntent.side == "sell", TradeIntent.symbol == SYMBOL)
            .order_by(TradeIntent.created_at)
        ).scalars()
        return list(rows)


def _confirmation_close_metadata(sf: sessionmaker) -> dict[str, dict[str, object]]:
    with sf() as session:
        rows = session.execute(
            select(BrokerAccount.name, BrokerOrder.payload)
            .join(BrokerOrder, BrokerOrder.broker_account_id == BrokerAccount.id)
            .join(TradeIntent, TradeIntent.id == BrokerOrder.intent_id)
            .where(TradeIntent.reason == "oms_v2_managed_exit:CONFIRMATION_EXIT")
        ).all()
        return {account: dict(payload or {}) for account, payload in rows}


async def _arm_decision(service: OmsRiskService) -> None:
    evaluated_at = service_module.utcnow() - timedelta(seconds=1)
    await service._handle_stream_message(
        {
            "data": json.dumps(
                {
                    "event_type": "v2_confirmation_exit",
                    "symbol": SYMBOL,
                    "broker_account_name": SCHWAB,
                    "source_fill_id": "schwab-decision-fill",
                    "fanout_slot_id": "imrn-first-slot",
                    "broker_order_id": "schwab-entry-1",
                    "evaluated_at_ms": str(int(evaluated_at.timestamp() * 1000)),
                    "atr_state": "short",
                    "should_exit": True,
                    "entry_slot": "first",
                }
            )
        }
    )
    service._latest_quotes_by_symbol[SYMBOL] = {
        "bid": 9.90,
        "ask": 9.91,
        "received_at": datetime.now(UTC),
    }


async def _prepare_webull_leg(
    service: OmsRiskService, *, expected_row_id: str
) -> str:
    return await service._prepare_confirmation_webull_leg(
        WEBULL,
        SYMBOL,
        expected_row_id=expected_row_id,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        decision=service_module._ConfirmationFanoutDecision(
            symbol=SYMBOL,
            source_fill_id="test-confirmation",
            accounts=(WEBULL,),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "terminal"),
    (
        pytest.param("both_cancelled", "SOLD", id="both-cancelled"),
        pytest.param("one_cancelled_one_429", "REPROTECTED+PAGED", id="imcc-one-cancelled-one-429"),
        pytest.param(
            "one_cancelled_one_order_cannot_cancel",
            "REPROTECTED+PAGED",
            id="one-cancelled-one-order-cannot-cancel",
        ),
        pytest.param(
            "both_order_cannot_cancel_after_release",
            "SOLD",
            id="gipr-both-order-cannot-cancel-after-confirmed-release",
        ),
        pytest.param("both_429", "REPROTECTED+PAGED", id="gipr-both-429"),
        pytest.param("transport_exception", "REPROTECTED+PAGED", id="transport-exception"),
        pytest.param(
            "working_after_cancelled_report",
            "REPROTECTED+PAGED",
            id="leg-read-working-after-cancelled-report",
        ),
        pytest.param("leg_filled", "SOLD", id="leg-read-filled"),
        pytest.param(
            "unknown_future_answer",
            "REPROTECTED+PAGED",
            id="unknown-answer-default",
        ),
    ),
)
async def test_webull_confirmation_bad_answer_matrix_has_only_safe_terminals(
    monkeypatch, row: str, terminal: str
) -> None:
    """Every known bad answer sells, or restores protection and opens one page."""
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    base = "known-protect-base"
    service._webull_protect_base[(WEBULL, SYMBOL)] = base
    await _arm_decision(service)
    pending = service._confirmation_exit_pending[(WEBULL, SYMBOL)]
    decision = service._confirmation_fanout_decision(pending)
    assert decision is not None
    row_id = _open_row_ids(sf)[WEBULL]

    cancelled = (
        cancelled_leg(base, "T", symbol=SYMBOL),
        cancelled_leg(base, "S", symbol=SYMBOL),
    )
    order_cannot_cancel = tuple(
        gipr_180510_order_cannot_cancel(base, suffix, symbol=SYMBOL) for suffix in ("T", "S")
    )
    rate_limited = tuple(
        gipr_180511_detail_rate_limited(base, suffix, symbol=SYMBOL) for suffix in ("T", "S")
    )
    if row == "both_cancelled":
        results = [ExitPairReleaseResult(outcome="released", reports=cancelled)]
    elif row == "one_cancelled_one_429":
        reports = imcc_174604_cancelled_and_rate_limited(base, symbol=SYMBOL)
        results = [ExitPairReleaseResult(outcome="unanswerable", reports=reports)] * 4
    elif row == "one_cancelled_one_order_cannot_cancel":
        reports = (cancelled[0], order_cannot_cancel[1])
        results = [ExitPairReleaseResult(outcome="reserved", reports=reports)] * 4
    elif row == "both_order_cannot_cancel_after_release":
        service._exit_reservation_released.add((WEBULL, SYMBOL))
        service._confirmation_webull_released_episode[(WEBULL, SYMBOL)] = (row_id, base)
        adapter.release_results.append(
            ExitPairReleaseResult(outcome="reserved", reports=order_cannot_cancel)
        )
        results = []
    elif row == "both_429":
        results = [ExitPairReleaseResult(outcome="unanswerable", reports=rate_limited)] * 4
    elif row == "transport_exception":
        results = []

        async def _transport_failure(**_kwargs):
            raise TimeoutError("tape-equivalent transport failure")

        monkeypatch.setattr(adapter, "release_exit_pair_for_close", _transport_failure)
    elif row == "working_after_cancelled_report":
        reports = (
            cancelled[0],
            working_leg_after_cancel_request(base, "S", symbol=SYMBOL),
        )
        results = [ExitPairReleaseResult(outcome="reserved", reports=reports)] * 4
    elif row == "leg_filled":
        reports = (filled_leg(base, "T", symbol=SYMBOL), cancelled[1])
        results = [ExitPairReleaseResult(outcome="resolved_by_fill", reports=reports)]
    else:
        reports = (
            unknown_answer(base, "T", symbol=SYMBOL),
            unknown_answer(base, "S", symbol=SYMBOL),
        )
        results = [ExitPairReleaseResult(outcome="unanswerable", reports=reports)] * 4
    adapter.release_results.extend(results)

    async def _no_sleep(_seconds: float) -> None:
        return None

    async def _attach(**_kwargs) -> bool:
        return True

    monkeypatch.setattr(service_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(service, "_attach_webull_protection", _attach)

    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    with sf() as session:
        incidents = session.scalars(select(service_module.SystemIncident)).all()
    if terminal == "SOLD":
        assert decision.outcomes[WEBULL] in {"closed", "resolved_by_fill"}
        assert incidents == []
    else:
        assert decision.outcomes[WEBULL] == "reprotected"
        assert len(incidents) == 1
        assert incidents[0].payload["protection_restored"] is True
    assert decision.outcomes[WEBULL] != "refused"


def _close_reject_fixture(response_class: str) -> ExecutionReport | BaseException:
    if response_class == "A_ALREADY_GONE":
        return ztg_195912_no_position_reject()
    if response_class == "B_UNCLEAR":
        return TimeoutError(
            "GIPR 2026-09-18 17:07:07Z: TOO_MANY_REQUESTS Too many requests (http 429)"
        )
    if response_class == "C_NOT_TRADABLE":
        return bq_170729_not_tradable_reject()
    return unknown_answer("future-protect-", "T", symbol="FUTR")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_class",
    ("A_ALREADY_GONE", "B_UNCLEAR", "C_NOT_TRADABLE", "E_UNKNOWN"),
)
async def test_confirmation_close_classes_reprotect_and_page_instead_of_giving_up(
    monkeypatch, response_class: str
) -> None:
    """Real A-C responses and the E default all end protected, never refused."""
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    response = _close_reject_fixture(response_class)
    adapter.submit_results.append(response if isinstance(response, BaseException) else [response])
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    attached: list[str] = []

    async def _attach(**kwargs) -> bool:
        attached.append(kwargs["symbol"])
        return True

    monkeypatch.setattr(service, "_attach_webull_protection", _attach)
    await _arm_decision(service)
    decision = service._confirmation_fanout_decision(
        service._confirmation_exit_pending[(WEBULL, SYMBOL)]
    )
    assert decision is not None

    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    await service._confirmation_exit_recovery_tasks.pop()

    assert len(adapter.submitted) == 1, f"{response_class} is not a corrected-retry class"
    assert attached == [SYMBOL]
    assert decision.outcomes[WEBULL] == "reprotected"
    assert decision.outcomes[WEBULL] != "refused"
    with sf() as session:
        incidents = session.scalars(select(service_module.SystemIncident)).all()
    assert len(incidents) == 1
    assert incidents[0].payload["protection_restored"] is True


@pytest.mark.asyncio
async def test_no_position_reject_closes_only_after_broker_read_confirms_flat(
    monkeypatch,
) -> None:
    """ZTG's class-A words are not flat proof; the independent position read is."""
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    adapter.submit_results.append([ztg_195912_no_position_reject()])
    adapter.position_state[WEBULL] = Decimal("0")
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    await _arm_decision(service)
    decision = service._confirmation_fanout_decision(
        service._confirmation_exit_pending[(WEBULL, SYMBOL)]
    )
    assert decision is not None

    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    await service._confirmation_exit_recovery_tasks.pop()

    assert decision.outcomes[WEBULL] == "flat"
    assert decision.outcomes[WEBULL] != "refused"
    with sf() as session:
        row = service.store.get_open_managed_position(
            session, broker_account_name=WEBULL, symbol=SYMBOL
        )
        incidents = session.scalars(select(service_module.SystemIncident)).all()
    assert row is None
    assert incidents == []


@pytest.mark.asyncio
async def test_malformed_confirmation_close_gets_exactly_one_corrected_retry(
    monkeypatch,
) -> None:
    """LGPS's real class-D response is corrected once; the retry fill is terminal."""
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    adapter.submit_results.append([lgps_133416_malformed_client_order_id_reject()])
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    await _arm_decision(service)
    decision = service._confirmation_fanout_decision(
        service._confirmation_exit_pending[(WEBULL, SYMBOL)]
    )
    assert decision is not None

    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    assert len(adapter.submitted) == 2
    first, corrected = adapter.submitted
    assert corrected.client_order_id != first.client_order_id
    assert len(corrected.client_order_id) <= 40
    assert corrected.order_type == "market"
    assert corrected.time_in_force == "day"
    assert corrected.metadata["confirmation_corrected_retry"] == "true"
    assert corrected.metadata["confirmation_corrected_from_client_order_id"] == (
        first.client_order_id
    )
    assert decision.outcomes[WEBULL] == "closed"
    assert service._confirmation_exit_recovery_tasks == set()
    with sf() as session:
        incidents = session.scalars(select(service_module.SystemIncident)).all()
        orders = session.scalars(
            select(BrokerOrder)
            .join(TradeIntent, TradeIntent.id == BrokerOrder.intent_id)
            .where(
                TradeIntent.reason == "oms_v2_managed_exit:CONFIRMATION_EXIT",
                BrokerOrder.broker_account_id
                == session.scalar(select(BrokerAccount.id).where(BrokerAccount.name == WEBULL)),
            )
            .order_by(BrokerOrder.submitted_at)
        ).all()
    assert incidents == []
    assert [order.status for order in orders] == ["rejected", "filled"]
    assert orders[1].payload["confirmation_corrected_retry"] == "true"


@pytest.mark.asyncio
async def test_malformed_confirmation_close_never_retries_more_than_once(
    monkeypatch,
) -> None:
    """A second class-D reject terminates in re-protection, not an order loop."""
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    malformed = lgps_133416_malformed_client_order_id_reject()
    adapter.submit_results.extend(([malformed], [malformed]))
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    attached: list[str] = []

    async def _attach(**kwargs) -> bool:
        attached.append(kwargs["symbol"])
        return True

    monkeypatch.setattr(service, "_attach_webull_protection", _attach)
    await _arm_decision(service)
    decision = service._confirmation_fanout_decision(
        service._confirmation_exit_pending[(WEBULL, SYMBOL)]
    )
    assert decision is not None

    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    await service._confirmation_exit_recovery_tasks.pop()

    assert len(adapter.submitted) == 2
    assert attached == [SYMBOL]
    assert decision.outcomes[WEBULL] == "reprotected"
    assert decision.outcomes[WEBULL] != "refused"
    with sf() as session:
        incidents = session.scalars(select(service_module.SystemIncident)).all()
    assert len(incidents) == 1


@pytest.mark.asyncio
async def test_state_long_reports_one_decision_without_arming_per_account_exits() -> None:
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()

    await service._handle_stream_message(
        {
            "data": json.dumps(
                {
                    "event_type": "v2_confirmation_exit",
                    "symbol": SYMBOL,
                    "broker_account_name": SCHWAB,
                    "source_fill_id": "schwab-long-decision-fill",
                    "broker_order_id": "schwab-entry-1",
                    "evaluated_at_ms": "1",
                    "atr_state": "long",
                    "should_exit": False,
                    "entry_slot": "first",
                }
            )
        }
    )

    assert service._confirmation_exit_pending == {}
    assert _sell_accounts(sf) == []
    lines = [
        line
        for line in service.logger.lines
        if "[OMS-V2-CONFIRMATION-EXIT-FANOUT]" in line
    ]
    assert len(lines) == 1
    assert "legs_total=2" in lines[0]
    assert "legs_state_long=2" in lines[0]


@pytest.mark.asyncio
async def test_inconsistent_non_fire_still_names_every_intended_leg() -> None:
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()

    await service._handle_stream_message(
        {
            "data": json.dumps(
                {
                    "event_type": "v2_confirmation_exit",
                    "symbol": SYMBOL,
                    "broker_account_name": SCHWAB,
                    "source_fill_id": "schwab-inconsistent-decision-fill",
                    "broker_order_id": "schwab-entry-1",
                    "evaluated_at_ms": "1",
                    "atr_state": "unknown",
                    "should_exit": False,
                    "entry_slot": "first",
                }
            )
        }
    )

    assert service._confirmation_exit_pending == {}
    assert _sell_accounts(sf) == []
    lines = [
        line
        for line in service.logger.lines
        if "[OMS-V2-CONFIRMATION-EXIT-FANOUT]" in line
    ]
    assert len(lines) == 1
    assert "legs_total=2" in lines[0]
    assert "legs_refused=2" in lines[0]
    assert "live:schwab_1m_v2:evaluation_refused" in lines[0]
    assert "live:orb:evaluation_refused" in lines[0]


def test_released_unprotected_interval_tracks_duration_and_current_count(monkeypatch) -> None:
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    decision = service_module._ConfirmationFanoutDecision(
        symbol=SYMBOL,
        source_fill_id="measured-interval",
        accounts=(WEBULL,),
    )
    clock = {"now": datetime(2026, 9, 6, 14, 0, tzinfo=UTC)}
    monkeypatch.setattr(service_module, "utcnow", lambda: clock["now"])

    service._start_confirmation_unprotected_interval(decision, WEBULL, SYMBOL)
    clock["now"] += timedelta(seconds=3.25)

    assert service._observe_confirmation_unprotected_interval(
        decision, WEBULL, SYMBOL
    ) == pytest.approx(3.25)
    assert len(service._confirmation_unprotected_since) == 1

    assert service._end_confirmation_unprotected_interval(
        decision, WEBULL, SYMBOL, resolution="reprotected"
    ) == pytest.approx(3.25)
    assert service._confirmation_unprotected_since == {}
    assert "released_unprotected_seconds=3.250" in "\n".join(service.logger.lines)
    assert "released_unprotected_current=0" in "\n".join(service.logger.lines)


@pytest.mark.asyncio
async def test_released_webull_pair_fresh_flat_preserves_interval_in_summary(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    clock = {"now": datetime(2026, 9, 6, 14, 0, tzinfo=UTC)}
    monkeypatch.setattr(service_module, "utcnow", lambda: clock["now"])
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    await _arm_decision(service)
    service._latest_quotes_by_symbol[SYMBOL]["received_at"] = clock["now"]

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)

    def _fresh_flat_after_broker_refresh(*args, **kwargs) -> str:
        clock["now"] += timedelta(seconds=4)
        return "fresh_flat"

    monkeypatch.setattr(
        service, "_post_exit_stale_held_action", _fresh_flat_after_broker_refresh
    )
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    summaries = [
        line
        for line in service.logger.lines
        if "[OMS-V2-CONFIRMATION-EXIT-FANOUT]" in line
    ]
    assert len(summaries) == 1
    assert "live:orb:flat" in summaries[0]
    assert "released_unprotected_seconds_max=4.000" in summaries[0]
    assert "released_unprotected_current=0" in summaries[0]


@pytest.mark.asyncio
async def test_recovery_flat_finalizes_released_unprotected_interval(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    clock = {"now": datetime(2026, 9, 6, 14, 0, tzinfo=UTC)}
    monkeypatch.setattr(service_module, "utcnow", lambda: clock["now"])
    adapter = _FanoutAdapter(reject_accounts={WEBULL})
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"

    async def _flat_after_recovery_read(*args, **kwargs):
        clock["now"] += timedelta(seconds=4)
        return service_module._PositionRead.FLAT_CONFIRMED

    monkeypatch.setattr(service, "_broker_symbol_position_state", _flat_after_recovery_read)
    await _arm_decision(service)
    service._latest_quotes_by_symbol[SYMBOL]["received_at"] = clock["now"]

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    await service._confirmation_exit_recovery_tasks.pop()

    assert _sell_accounts(sf) == [SCHWAB, WEBULL]
    assert (WEBULL, SYMBOL) not in service._confirmation_unprotected_since
    with sf() as session:
        webull_row = service.store.get_open_managed_position(
            session, broker_account_name=WEBULL, symbol=SYMBOL
        )
    assert webull_row is None
    text = "\n".join(service.logger.lines)
    assert "[OMS-V2-CONFIRMATION-EXIT-COVERAGE-RESTORED]" in text
    assert "resolution=managed_episode_closed" in text
    assert "released_unprotected_seconds=4.000" in text
    assert "live:orb:flat" in text
    assert "released_unprotected_seconds_max=4.000" in text
    assert "released_unprotected_current=0" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fanout", "expected_accounts"),
    [(False, [SCHWAB]), (True, [SCHWAB, WEBULL])],
)
async def test_controlled_fanout_flag_pair_closes_only_managed_accounts(
    monkeypatch, fanout: bool, expected_accounts: list[str]
) -> None:
    """One variable only: OFF is Schwab-only; ON executes the same decision on both legs."""
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=fanout, adapter=adapter)
    await _arm_decision(service)

    pending = service._confirmation_exit_pending
    bound = _open_row_ids(sf)
    assert list(pending) == [(account, SYMBOL) for account in expected_accounts]
    if fanout:
        assert pending[(SCHWAB, SYMBOL)]["bound_managed_row_id"] == bound[SCHWAB]
        assert pending[(WEBULL, SYMBOL)]["bound_managed_row_id"] == bound[WEBULL]
        assert bound[SCHWAB] != bound[WEBULL]

    for account in expected_accounts:
        await service._evaluate_v2_managed_exit(account, SYMBOL)

    assert _sell_accounts(sf) == expected_accounts
    assert [request.broker_account_name for request in adapter.submitted] == expected_accounts
    metadata = _confirmation_close_metadata(sf)
    for account in expected_accounts:
        assert metadata[account]["flip_owner_confirmation_exit"] == "true"
        assert metadata[account]["confirmation_fanout_slot_id"] == "imrn-first-slot"
        assert metadata[account]["confirmation_managed_row_id"] == bound[account]


@pytest.mark.asyncio
async def test_delayed_confirmation_fill_keeps_owner_identity_for_the_position_book(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _DelayedFillAdapter()
    service, sf = _service(fanout=False, adapter=adapter)
    await _arm_decision(service)
    expected_row_id = _open_row_ids(sf)[SCHWAB]

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)
    assert _confirmation_close_metadata(sf)[SCHWAB]["confirmation_managed_row_id"] == (
        expected_row_id
    )
    await service.sync_broker_orders(account_names=[SCHWAB])

    bot = SchwabV2BotService(
        Settings(
            strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
            strategy_schwab_1m_v2_account_name=SCHWAB,
            strategy_schwab_1m_v2_webull_account_name=WEBULL,
        ),
        session_factory=sf,
    )
    book = bot._fetch_flip_position_book()
    close = book.confirmation_closes_by_symbol[SYMBOL][0]
    assert close.account_name == SCHWAB
    assert close.managed_row_id == expected_row_id
    assert close.fanout_slot_id == "imrn-first-slot"


@pytest.mark.asyncio
async def test_rejecting_webull_leg_does_not_block_schwab_and_reports_one_denominator_line(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter(reject_accounts={WEBULL})
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    await _arm_decision(service)

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    assert _sell_accounts(sf) == [SCHWAB, WEBULL]
    assert [request.broker_account_name for request in adapter.submitted] == [SCHWAB, WEBULL]
    lines = [
        line
        for line in service.logger.lines
        if "[OMS-V2-CONFIRMATION-EXIT-FANOUT]" in line
    ]
    assert len(lines) == 1
    assert (
        "legs_total=2 legs_closed=1 legs_close_submitted=0 legs_refused=1 "
        "legs_no_open_row=0"
    ) in lines[0]
    assert "live:schwab_1m_v2:closed" in lines[0]
    assert "live:orb:refused" in lines[0]


@pytest.mark.asyncio
async def test_bare_webull_stop_limit_with_no_bracket_is_a_normal_close(monkeypatch) -> None:
    """Production majority shape: qty 1, STOP_LIMIT entry, no attached pair handle."""
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    await _arm_decision(service)

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    assert adapter.cancel_pair_calls == []
    assert _sell_accounts(sf) == [SCHWAB, WEBULL]


@pytest.mark.asyncio
async def test_bare_webull_position_can_close_outside_rth_without_releasing_a_pair(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: False)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    await _arm_decision(service)

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    assert adapter.cancel_pair_calls == []
    assert _sell_accounts(sf) == [SCHWAB, WEBULL]


@pytest.mark.asyncio
async def test_stale_quote_cannot_release_webull_protection(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    await _arm_decision(service)
    service._latest_quotes_by_symbol[SYMBOL]["received_at"] = datetime.now(UTC) - timedelta(
        seconds=10
    )

    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    assert adapter.cancel_pair_calls == []
    assert _sell_accounts(sf) == []
    assert (WEBULL, SYMBOL) in service._confirmation_exit_pending


@pytest.mark.asyncio
async def test_webull_pair_is_not_released_outside_rth_and_schwab_still_closes(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: False)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    await _arm_decision(service)

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    assert adapter.cancel_pair_calls == []
    assert _sell_accounts(sf) == [SCHWAB]
    text = "\n".join(service.logger.lines)
    assert "outside_rth_pair_release_would_be_irreversible" in text
    assert "legs_total=2 legs_closed=1 legs_close_submitted=0 legs_refused=1" in text


@pytest.mark.asyncio
async def test_released_webull_leg_that_rejects_is_reprotected(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter(reject_accounts={WEBULL})
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    reprotected: list[tuple[str, str]] = []

    async def _attach(**kwargs) -> bool:
        reprotected.append((kwargs["broker_account_name"], kwargs["symbol"]))
        return True

    monkeypatch.setattr(service, "_attach_webull_protection", _attach)
    await _arm_decision(service)

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    await service._confirmation_exit_recovery_tasks.pop()

    assert adapter.cancel_pair_calls == [(WEBULL, SYMBOL, "known-protect-base")]
    assert reprotected == [(WEBULL, SYMBOL)]
    assert (WEBULL, SYMBOL) not in service._exit_reservation_released
    assert (WEBULL, SYMBOL) not in service._confirmation_exit_pending
    assert "[OMS-V2-CONFIRMATION-EXIT-PAGE]" in "\n".join(service.logger.lines)
    assert "released_unprotected_current=0" in "\n".join(service.logger.lines)
    summary = "\n".join(service.logger.lines)
    assert "legs_closed=1" in summary
    assert "legs_released=2 legs_reprotected=1 legs_uncovered=0" in summary
    with sf() as session:
        incidents = session.scalars(select(service_module.SystemIncident)).all()
    assert len(incidents) == 1
    assert incidents[0].payload["source"] == "oms_v2_confirmation_exit_reprotected"


@pytest.mark.asyncio
async def test_released_webull_leg_is_reprotected_when_a_pre_send_guard_stops_it(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    reprotected: list[tuple[str, str]] = []
    original_read = service._read_v2_managed_snapshot

    def _dedup_snapshot(session, acct, symbol, close_on_fill):
        snapshot = original_read(session, acct, symbol, close_on_fill)
        if acct == WEBULL and snapshot is not None:
            return replace(snapshot, dedup_active=True)
        return snapshot

    async def _attach(**kwargs) -> bool:
        reprotected.append((kwargs["broker_account_name"], kwargs["symbol"]))
        return True

    monkeypatch.setattr(service, "_read_v2_managed_snapshot", _dedup_snapshot)
    monkeypatch.setattr(service, "_attach_webull_protection", _attach)
    await _arm_decision(service)

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    await service._confirmation_exit_recovery_tasks.pop()

    assert adapter.cancel_pair_calls == [(WEBULL, SYMBOL, "known-protect-base")]
    assert reprotected == [(WEBULL, SYMBOL)]
    assert _sell_accounts(sf) == [SCHWAB]
    assert (WEBULL, SYMBOL) not in service._confirmation_exit_pending
    assert "legs_released=2 legs_reprotected=1 legs_uncovered=0" in "\n".join(
        service.logger.lines
    )


@pytest.mark.asyncio
async def test_confirmed_webull_release_is_idempotent_for_the_exact_episode(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    row_id = _open_row_ids(sf)[WEBULL]

    assert await _prepare_webull_leg(service, expected_row_id=row_id) == "released"
    assert await _prepare_webull_leg(service, expected_row_id=row_id) == "released"

    assert adapter.cancel_pair_calls == [(WEBULL, SYMBOL, "known-protect-base")]


@pytest.mark.asyncio
async def test_gipr_already_absent_reports_count_only_after_exact_release(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    row_id = _open_row_ids(sf)[WEBULL]
    key = (WEBULL, SYMBOL)
    service._exit_reservation_released.add(key)
    service._confirmation_webull_released_episode[key] = (
        row_id,
        "known-protect-base",
    )
    adapter.release_results.append(
        ExitPairReleaseResult(
            outcome="reserved",
            reports=(
                gipr_170707_order_cannot_cancel("known-protect-base", "T", symbol=SYMBOL),
                gipr_170707_order_cannot_cancel("known-protect-base", "S", symbol=SYMBOL),
            ),
        )
    )

    assert await _prepare_webull_leg(service, expected_row_id=row_id) == "released"
    assert adapter.cancel_pair_calls == [], "an exact released episode was cancelled twice"


@pytest.mark.asyncio
async def test_rate_limit_is_not_absence_even_when_an_exact_release_won_the_race(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    row_id = _open_row_ids(sf)[WEBULL]

    reports = tuple(
        gipr_170707_detail_rate_limited("known-protect-base", suffix, symbol=SYMBOL)
        for suffix in ("T", "S")
    )
    adapter.release_results.extend(
        ExitPairReleaseResult(outcome="unanswerable", reports=reports) for _ in range(4)
    )

    async def _no_sleep(_seconds: float) -> None:
        return None

    async def _attach(**kwargs) -> bool:
        return False

    monkeypatch.setattr(service_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(service, "_attach_webull_protection", _attach)

    assert await _prepare_webull_leg(service, expected_row_id=row_id) == "uncovered"
    assert (WEBULL, SYMBOL) not in service._exit_reservation_released


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["ORDER_CAN_NOT_BE_CANCEL", "TOO_MANY_REQUESTS"])
async def test_cancel_reject_is_not_absence_without_prior_exact_release(
    monkeypatch, code: str
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    row_id = _open_row_ids(sf)[WEBULL]

    reports = tuple(
        (
            gipr_170707_order_cannot_cancel("known-protect-base", suffix, symbol=SYMBOL)
            if code == "ORDER_CAN_NOT_BE_CANCEL"
            else gipr_170707_detail_rate_limited("known-protect-base", suffix, symbol=SYMBOL)
        )
        for suffix in ("T", "S")
    )
    adapter.release_results.extend(
        ExitPairReleaseResult(outcome="reserved", reports=reports) for _ in range(4)
    )

    async def _no_sleep(_seconds: float) -> None:
        return None

    async def _attach(**kwargs) -> bool:
        return False

    monkeypatch.setattr(service_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(service, "_attach_webull_protection", _attach)

    assert await _prepare_webull_leg(service, expected_row_id=row_id) == "uncovered"
    assert (WEBULL, SYMBOL) not in service._exit_reservation_released


@pytest.mark.asyncio
async def test_unknown_after_released_rejected_sell_is_explicitly_uncovered(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter(reject_accounts={WEBULL})
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"

    async def _unknown(*args, **kwargs):
        return service_module._PositionRead.UNKNOWN

    async def _attach(**kwargs) -> bool:
        return False

    monkeypatch.setattr(service, "_broker_symbol_position_state", _unknown)
    monkeypatch.setattr(service, "_attach_webull_protection", _attach)
    await _arm_decision(service)

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    await service._confirmation_exit_recovery_tasks.pop()

    text = "\n".join(service.logger.lines)
    assert "[OMS-V2-CONFIRMATION-EXIT-REPROTECTED]" in text
    assert "protected=0" in text
    assert "[OMS-V2-CONFIRMATION-EXIT-PAGE]" in text
    assert "released_unprotected_seconds_max=" in text
    assert "released_unprotected_current=1" in text
    assert "legs_released=2 legs_reprotected=0 legs_uncovered=1" in text
    with sf() as session:
        incidents = session.scalars(select(service_module.SystemIncident)).all()
    assert len(incidents) == 1
    assert incidents[0].payload["protection_restored"] is False


@pytest.mark.asyncio
async def test_only_one_confirmed_child_reprotects_and_pages_instead_of_giving_up(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    reports = imcc_174604_cancelled_and_rate_limited("known-protect-base", symbol=SYMBOL)
    adapter.release_results.extend(
        ExitPairReleaseResult(outcome="unanswerable", reports=reports) for _ in range(4)
    )

    async def _no_sleep(_seconds: float) -> None:
        return None

    async def _attach(**_kwargs) -> bool:
        return True

    monkeypatch.setattr(service_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(service, "_attach_webull_protection", _attach)
    await _arm_decision(service)

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    assert _sell_accounts(sf) == [SCHWAB]
    assert (WEBULL, SYMBOL) not in service._exit_reservation_released
    assert (WEBULL, SYMBOL) not in service._confirmation_exit_pending
    assert "legs_reprotected=1" in "\n".join(service.logger.lines)
    with sf() as session:
        incidents = session.scalars(select(service_module.SystemIncident)).all()
    assert len(incidents) == 1
    assert incidents[0].payload["protection_restored"] is True


@pytest.mark.asyncio
async def test_concurrent_quote_task_cannot_cancel_the_same_episode_twice(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    await _arm_decision(service)
    entered = service_module.asyncio.Event()
    release = service_module.asyncio.Event()
    row_id = _open_row_ids(sf)[WEBULL]
    bound_reads = 0

    async def _slow_bound(_acct: str, _symbol: str) -> str:
        nonlocal bound_reads
        bound_reads += 1
        entered.set()
        await release.wait()
        return row_id

    monkeypatch.setattr(service, "_confirmation_bound_managed_row_id", _slow_bound)
    first = service_module.asyncio.create_task(
        service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
    )
    second = None
    try:
        await service_module.asyncio.wait_for(entered.wait(), timeout=1.0)
        second = service_module.asyncio.create_task(
            service._evaluate_v2_managed_exit(WEBULL, SYMBOL)
        )
        await service_module.asyncio.sleep(0)
        release.set()
        await service_module.asyncio.wait_for(
            service_module.asyncio.gather(first, second), timeout=1.0
        )
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await service_module.asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )

    assert adapter.cancel_pair_calls == [(WEBULL, SYMBOL, "known-protect-base")]
    assert bound_reads == 1, "the second quote entered before the decision was claimed"
    assert _sell_accounts(sf) == [WEBULL]


@pytest.mark.asyncio
async def test_imcc_one_of_two_cancel_reads_retries_instead_of_abandoning(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    row_id = _open_row_ids(sf)[WEBULL]
    one_cancelled = cancelled_leg("known-protect-base", "T", symbol=SYMBOL)
    one_working = working_leg_after_cancel_request(
        "known-protect-base", "S", symbol=SYMBOL
    )
    adapter.release_results.extend(
        (
            ExitPairReleaseResult(
                outcome="reserved", reports=(one_cancelled, one_working)
            ),
            ExitPairReleaseResult(
                outcome="released", reports=(one_cancelled, replace(one_working, event_type="cancelled"))
            ),
        )
    )
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(service_module.asyncio, "sleep", _sleep)

    assert await _prepare_webull_leg(service, expected_row_id=row_id) == "released"
    assert sleeps == [1.0]
    assert len(adapter.cancel_pair_calls) == 2


@pytest.mark.asyncio
async def test_order_cannot_cancel_is_not_absent_when_pair_read_says_working(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    row_id = _open_row_ids(sf)[WEBULL]
    working = replace(
        gipr_170707_order_cannot_cancel("known-protect-base", "S", symbol=SYMBOL),
        event_type="accepted",
        metadata={
            "webull_error_code": "ORDER_CAN_NOT_BE_CANCEL",
            "cancel_outcome": "not_confirmed",
        },
    )
    terminal = cancelled_leg("known-protect-base", "T", symbol=SYMBOL)
    adapter.release_results.extend(
        ExitPairReleaseResult(outcome="reserved", reports=(terminal, working))
        for _ in range(4)
    )

    async def _no_sleep(_seconds: float) -> None:
        return None

    async def _attach(**_kwargs) -> bool:
        return False

    monkeypatch.setattr(service_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(service, "_attach_webull_protection", _attach)

    assert await _prepare_webull_leg(service, expected_row_id=row_id) == "uncovered"
    assert (WEBULL, SYMBOL) not in service._exit_reservation_released


@pytest.mark.asyncio
async def test_release_budget_exhaustion_reprotects_and_opens_one_pager_incident(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    row_id = _open_row_ids(sf)[WEBULL]
    uncertain = ExecutionReport(
        event_type="accepted",
        origin="unknown",
        client_order_id="known-protect-baseT",
        symbol=SYMBOL,
        side="sell",
        intent_type="cancel",
        metadata={"cancel_outcome": "could_not_tell"},
    )
    adapter.release_results.extend(
        ExitPairReleaseResult(outcome="unanswerable", reports=(uncertain, uncertain))
        for _ in range(4)
    )
    attached: list[str] = []

    async def _no_sleep(_seconds: float) -> None:
        return None

    async def _attach(**kwargs) -> bool:
        attached.append(kwargs["symbol"])
        return True

    monkeypatch.setattr(service_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(service, "_attach_webull_protection", _attach)

    assert await _prepare_webull_leg(service, expected_row_id=row_id) == "reprotected"
    assert attached == [SYMBOL]
    with sf() as session:
        incidents = session.scalars(select(service_module.SystemIncident)).all()
    assert len(incidents) == 1
    assert incidents[0].payload["protection_restored"] is True
    assert incidents[0].payload["missing_legs"] == "unknown:target,stop"


@pytest.mark.asyncio
async def test_reject_ceiling_blocks_webull_before_pair_release(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    await _arm_decision(service)
    service._v2_exit_reject_total[(WEBULL, SYMBOL)] = service._V2_EXIT_MAX_REJECTS_PER_EPISODE

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    assert adapter.cancel_pair_calls == []
    assert _sell_accounts(sf) == [SCHWAB]
    assert (WEBULL, SYMBOL) not in service._confirmation_exit_pending


@pytest.mark.asyncio
async def test_missing_webull_row_is_counted_without_blocking_schwab(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()
    service, sf = _service(fanout=True, adapter=adapter)
    service.logger = _CapturedLogger()
    with sf() as session:
        webull_row = service.store.get_open_managed_position(
            session, broker_account_name=WEBULL, symbol=SYMBOL
        )
        service.store.close_managed_position(session, webull_row)
        session.commit()

    await _arm_decision(service)
    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)

    assert _sell_accounts(sf) == [SCHWAB]
    assert "legs_total=2 legs_closed=1 legs_close_submitted=0 legs_refused=0" in "\n".join(
        service.logger.lines
    )
    assert "legs_no_open_row=1" in "\n".join(service.logger.lines)
