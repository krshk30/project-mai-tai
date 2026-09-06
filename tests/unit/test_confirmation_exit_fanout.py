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
)
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, OmsManagedPosition, TradeIntent
from project_mai_tai.oms import service as service_module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

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

    async def submit_order(self, request):
        self.submitted.append(request)
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

    async def list_account_positions(self, broker_account_name: str):
        return [
            BrokerPositionSnapshot(
                broker_account_name=broker_account_name,
                symbol=SYMBOL,
                quantity=self.position_state[broker_account_name],
                average_price=Decimal("10"),
            )
        ]


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


async def _arm_decision(service: OmsRiskService) -> None:
    await service._handle_stream_message(
        {
            "data": json.dumps(
                {
                    "event_type": "v2_confirmation_exit",
                    "symbol": SYMBOL,
                    "broker_account_name": SCHWAB,
                    "source_fill_id": "schwab-decision-fill",
                    "broker_order_id": "schwab-entry-1",
                    "evaluated_at_ms": "1",
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
    service, _sf = _service(fanout=True, adapter=adapter)
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
    assert list(pending) == [(account, SYMBOL) for account in expected_accounts]
    if fanout:
        bound = _open_row_ids(sf)
        assert pending[(SCHWAB, SYMBOL)]["bound_managed_row_id"] == bound[SCHWAB]
        assert pending[(WEBULL, SYMBOL)]["bound_managed_row_id"] == bound[WEBULL]
        assert bound[SCHWAB] != bound[WEBULL]

    for account in expected_accounts:
        await service._evaluate_v2_managed_exit(account, SYMBOL)

    assert _sell_accounts(sf) == expected_accounts
    assert [request.broker_account_name for request in adapter.submitted] == expected_accounts


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
    assert "legs_released=2 legs_reprotected=1 legs_uncovered=0" in "\n".join(
        service.logger.lines
    )
    assert "released_unprotected_current=0" in "\n".join(service.logger.lines)


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
    assert "legs_released=2 legs_reprotected=1 legs_uncovered=0" in "\n".join(
        service.logger.lines
    )


@pytest.mark.asyncio
async def test_unknown_after_released_rejected_sell_is_explicitly_uncovered(
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter(reject_accounts={WEBULL})
    service, _sf = _service(fanout=True, adapter=adapter)
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
    assert "[OMS-V2-CONFIRMATION-EXIT-UNCOVERED]" in text
    assert "released_unprotected_seconds=" in text
    assert "released_unprotected_current=1" in text
    assert "legs_released=2 legs_reprotected=0 legs_uncovered=1" in text


@pytest.mark.asyncio
async def test_uncertain_webull_pair_cancel_refuses_only_that_leg(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    adapter = _FanoutAdapter()

    async def _uncertain_cancel(**kwargs):
        return [
            ExecutionReport(
                event_type="cancelled",
                origin="broker",
                client_order_id="known-protect-baseT",
                symbol=SYMBOL,
                side="sell",
                intent_type="cancel",
                reason="only one child confirmed",
            )
        ]

    monkeypatch.setattr(adapter, "cancel_exit_pair", _uncertain_cancel)
    service, sf = _service(fanout=True, adapter=adapter)
    service._webull_protect_base[(WEBULL, SYMBOL)] = "known-protect-base"
    await _arm_decision(service)

    await service._evaluate_v2_managed_exit(SCHWAB, SYMBOL)
    await service._evaluate_v2_managed_exit(WEBULL, SYMBOL)

    assert _sell_accounts(sf) == [SCHWAB]
    assert (WEBULL, SYMBOL) not in service._exit_reservation_released
    assert (WEBULL, SYMBOL) not in service._confirmation_exit_pending


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
