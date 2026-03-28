from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, ExecutionReport
from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    BrokerOrderEvent,
    Fill,
    RiskCheck,
    Strategy,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.events import TradeIntentEvent


def utcnow() -> datetime:
    return datetime.now(UTC)


class OmsStore:
    def list_active_broker_accounts(self, session: Session) -> list[BrokerAccount]:
        return session.scalars(
            select(BrokerAccount)
            .where(BrokerAccount.is_active.is_(True))
            .order_by(BrokerAccount.name)
        ).all()

    def list_named_broker_accounts(self, session: Session, names: list[str]) -> list[BrokerAccount]:
        if not names:
            return []
        return session.scalars(
            select(BrokerAccount)
            .where(BrokerAccount.name.in_(names))
            .order_by(BrokerAccount.name)
        ).all()

    def ensure_strategy(
        self,
        session: Session,
        code: str,
        *,
        name: str | None = None,
        execution_mode: str = "paper",
        metadata_json: dict[str, object] | None = None,
        is_enabled: bool = True,
    ) -> Strategy:
        strategy = session.scalar(select(Strategy).where(Strategy.code == code))
        if strategy is None:
            strategy = Strategy(
                code=code,
                name=name or code.replace("_", " ").upper(),
                execution_mode=execution_mode,
                metadata_json=metadata_json or {},
                is_enabled=is_enabled,
            )
            session.add(strategy)
            session.flush()
            return strategy

        if name:
            strategy.name = name
        strategy.execution_mode = execution_mode
        strategy.is_enabled = is_enabled
        if metadata_json is not None:
            strategy.metadata_json = metadata_json
        session.flush()
        return strategy

    def ensure_broker_account(
        self,
        session: Session,
        name: str,
        *,
        provider: str,
        environment: str,
        external_account_id: str | None = None,
        is_active: bool = True,
    ) -> BrokerAccount:
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == name))
        if account is None:
            account = BrokerAccount(
                name=name,
                provider=provider,
                environment=environment,
                external_account_id=external_account_id,
                is_active=is_active,
            )
            session.add(account)
            session.flush()
            return account

        account.provider = provider
        account.environment = environment
        if external_account_id is not None:
            account.external_account_id = external_account_id
        account.is_active = is_active
        session.flush()
        return account

    def create_trade_intent(
        self,
        session: Session,
        *,
        strategy: Strategy,
        broker_account: BrokerAccount,
        event: TradeIntentEvent,
    ) -> TradeIntent:
        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=broker_account.id,
            symbol=event.payload.symbol,
            side=event.payload.side,
            intent_type=event.payload.intent_type,
            quantity=event.payload.quantity,
            reason=event.payload.reason,
            status="pending",
            payload={
                "event_id": str(event.event_id),
                "source_service": event.source_service,
                "metadata": dict(event.payload.metadata),
            },
        )
        session.add(intent)
        session.flush()
        return intent

    def record_risk_check(
        self,
        session: Session,
        *,
        intent: TradeIntent,
        strategy_id: UUID,
        broker_account_id: UUID,
        outcome: str,
        reason: str,
        payload: dict[str, object] | None = None,
    ) -> RiskCheck:
        check = RiskCheck(
            intent_id=intent.id,
            strategy_id=strategy_id,
            broker_account_id=broker_account_id,
            outcome=outcome,
            reason=reason,
            payload=payload or {},
        )
        session.add(check)
        session.flush()
        return check

    def get_or_create_order(
        self,
        session: Session,
        *,
        intent: TradeIntent,
        strategy_id: UUID,
        broker_account_id: UUID,
        client_order_id: str,
        symbol: str,
        side: str,
        quantity: Decimal,
        metadata: dict[str, str],
        broker_order_id: str | None = None,
        status: str = "pending",
        order_type: str = "market",
        time_in_force: str = "day",
    ) -> BrokerOrder:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == client_order_id))
        if order is None:
            order = BrokerOrder(
                intent_id=intent.id,
                strategy_id=strategy_id,
                broker_account_id=broker_account_id,
                client_order_id=client_order_id,
                broker_order_id=broker_order_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                time_in_force=time_in_force,
                quantity=quantity,
                status=status,
                payload=dict(metadata),
                submitted_at=utcnow(),
            )
            session.add(order)
            session.flush()
            return order

        order.status = status
        order.broker_order_id = broker_order_id or order.broker_order_id
        order.payload = dict(metadata)
        if order.submitted_at is None:
            order.submitted_at = utcnow()
        session.flush()
        return order

    def append_order_event(
        self,
        session: Session,
        *,
        order: BrokerOrder,
        report: ExecutionReport,
        payload: dict[str, object],
    ) -> BrokerOrderEvent:
        event = BrokerOrderEvent(
            order_id=order.id,
            event_type=report.event_type,
            event_at=report.reported_at,
            payload=payload,
        )
        session.add(event)
        session.flush()
        return event

    def record_fill_if_needed(
        self,
        session: Session,
        *,
        order: BrokerOrder,
        strategy_id: UUID,
        broker_account_id: UUID,
        report: ExecutionReport,
        payload: dict[str, object],
    ) -> Fill | None:
        if report.event_type not in {"filled", "partially_filled"}:
            return None
        if report.filled_quantity <= 0 or report.fill_price is None:
            return None
        if report.broker_fill_id:
            existing = session.scalar(select(Fill).where(Fill.broker_fill_id == report.broker_fill_id))
            if existing is not None:
                return existing

        fill = Fill(
            order_id=order.id,
            strategy_id=strategy_id,
            broker_account_id=broker_account_id,
            broker_fill_id=report.broker_fill_id,
            symbol=order.symbol,
            side=order.side,
            quantity=report.filled_quantity,
            price=report.fill_price,
            filled_at=report.reported_at,
            payload=payload,
        )
        session.add(fill)
        session.flush()
        return fill

    def apply_fill_to_positions(
        self,
        session: Session,
        *,
        strategy_id: UUID,
        broker_account_id: UUID,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        reported_at: datetime,
    ) -> None:
        virtual_position = session.scalar(
            select(VirtualPosition).where(
                VirtualPosition.strategy_id == strategy_id,
                VirtualPosition.broker_account_id == broker_account_id,
                VirtualPosition.symbol == symbol,
            )
        )
        if virtual_position is None:
            virtual_position = VirtualPosition(
                strategy_id=strategy_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
                quantity=Decimal("0"),
                average_price=Decimal("0"),
                realized_pnl=Decimal("0"),
                opened_at=None,
            )
            session.add(virtual_position)
            session.flush()

        account_position = session.scalar(
            select(AccountPosition).where(
                AccountPosition.broker_account_id == broker_account_id,
                AccountPosition.symbol == symbol,
            )
        )
        if account_position is None:
            account_position = AccountPosition(
                broker_account_id=broker_account_id,
                symbol=symbol,
                quantity=Decimal("0"),
                average_price=Decimal("0"),
                market_value=None,
                source_updated_at=None,
            )
            session.add(account_position)
            session.flush()

        self._apply_position_fill(
            quantity=quantity,
            price=price,
            side=side,
            position=virtual_position,
            track_realized_pnl=True,
            reported_at=reported_at,
        )
        self._apply_position_fill(
            quantity=quantity,
            price=price,
            side=side,
            position=account_position,
            track_realized_pnl=False,
            reported_at=reported_at,
        )
        session.flush()

    def mark_intent_status(self, intent: TradeIntent, status: str) -> None:
        intent.status = status

    def sync_account_positions(
        self,
        session: Session,
        *,
        broker_account_id: UUID,
        snapshots: list[BrokerPositionSnapshot],
    ) -> int:
        existing_positions = {
            position.symbol: position
            for position in session.scalars(
                select(AccountPosition).where(AccountPosition.broker_account_id == broker_account_id)
            ).all()
        }
        seen_symbols: set[str] = set()

        for snapshot in snapshots:
            seen_symbols.add(snapshot.symbol)
            position = existing_positions.get(snapshot.symbol)
            if position is None:
                position = AccountPosition(
                    broker_account_id=broker_account_id,
                    symbol=snapshot.symbol,
                    quantity=Decimal("0"),
                    average_price=Decimal("0"),
                    market_value=None,
                    source_updated_at=None,
                )
                session.add(position)
                session.flush()

            position.quantity = snapshot.quantity
            position.average_price = snapshot.average_price
            position.market_value = snapshot.market_value
            position.source_updated_at = snapshot.as_of

        for symbol, position in existing_positions.items():
            if symbol in seen_symbols:
                continue
            position.quantity = Decimal("0")
            position.average_price = Decimal("0")
            position.market_value = Decimal("0")
            position.source_updated_at = utcnow()

        session.flush()
        return len(snapshots)

    def _apply_position_fill(
        self,
        *,
        quantity: Decimal,
        price: Decimal,
        side: str,
        position,
        track_realized_pnl: bool,
        reported_at: datetime,
    ) -> None:
        if side == "buy":
            new_qty = position.quantity + quantity
            if new_qty > 0:
                weighted_cost = position.average_price * position.quantity + price * quantity
                position.average_price = weighted_cost / new_qty
            position.quantity = new_qty
            if hasattr(position, "opened_at") and position.opened_at is None:
                position.opened_at = reported_at
            if hasattr(position, "source_updated_at"):
                position.source_updated_at = reported_at
            return

        sell_qty = min(position.quantity, quantity)
        if track_realized_pnl and sell_qty > 0:
            position.realized_pnl += (price - position.average_price) * sell_qty
        position.quantity -= sell_qty
        if position.quantity <= 0:
            position.quantity = Decimal("0")
            position.average_price = Decimal("0")
            if hasattr(position, "opened_at"):
                position.opened_at = None
        if hasattr(position, "source_updated_at"):
            position.source_updated_at = reported_at
