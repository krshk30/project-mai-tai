from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import desc
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from project_mai_tai.ai_trade_coach.models import EpisodeBarSnapshot
from project_mai_tai.ai_trade_coach.models import TradeCoachConfig
from project_mai_tai.ai_trade_coach.models import TradeEpisode
from project_mai_tai.db.models import AiTradeReview
from project_mai_tai.db.models import BrokerAccount
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.db.models import BrokerOrderEvent
from project_mai_tai.db.models import Fill
from project_mai_tai.db.models import RiskCheck
from project_mai_tai.db.models import Strategy
from project_mai_tai.db.models import StrategyBarHistory
from project_mai_tai.db.models import TradeIntent
from project_mai_tai.trade_episodes import CompletedTradeCycle
from project_mai_tai.trade_episodes import collect_completed_trade_cycles
from project_mai_tai.trade_episodes import parse_et_timestamp
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ


class TradeCoachRepository:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        config: TradeCoachConfig | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.config = config or TradeCoachConfig()

    def list_reviewable_cycles(
        self,
        *,
        strategy_accounts: list[tuple[str, str]],
        session_start: datetime,
        session_end: datetime,
        review_limit: int,
    ) -> list[CompletedTradeCycle]:
        with self.session_factory() as session:
            reviewed_cycle_keys = set(
                session.scalars(
                    select(AiTradeReview.cycle_key).where(
                        AiTradeReview.review_type == self.config.review_type,
                    )
                ).all()
            )

            cycles: list[CompletedTradeCycle] = []
            for strategy_code, broker_account_name in strategy_accounts:
                recent_orders = self._load_recent_orders(
                    session=session,
                    strategy_code=strategy_code,
                    broker_account_name=broker_account_name,
                    session_start=session_start,
                    session_end=session_end,
                )
                recent_fills = self._load_recent_fills(
                    session=session,
                    strategy_code=strategy_code,
                    broker_account_name=broker_account_name,
                    session_start=session_start,
                    session_end=session_end,
                )
                pair_cycles = collect_completed_trade_cycles(
                    strategy_code=strategy_code,
                    broker_account_name=broker_account_name,
                    recent_orders=recent_orders,
                    recent_fills=recent_fills,
                    closed_today=[],
                )
                for cycle in sorted(pair_cycles, key=lambda item: item.sort_time, reverse=True):
                    if cycle.cycle_key in reviewed_cycle_keys:
                        continue
                    cycles.append(cycle)
            cycles.sort(key=lambda item: parse_et_timestamp(item.sort_time), reverse=True)
            return cycles[:review_limit]

    def build_episode(self, *, cycle: CompletedTradeCycle) -> TradeEpisode:
        entry_dt = parse_et_timestamp(cycle.entry_time).astimezone(UTC)
        exit_dt = parse_et_timestamp(cycle.exit_time).astimezone(UTC)
        window_start = entry_dt - timedelta(minutes=5)
        window_end = exit_dt + timedelta(minutes=5)

        with self.session_factory() as session:
            strategy = session.scalar(select(Strategy).where(Strategy.code == cycle.strategy_code))
            broker_account = session.scalar(
                select(BrokerAccount).where(BrokerAccount.name == cycle.broker_account_name)
            )
            if strategy is None:
                raise RuntimeError(f"Unknown strategy_code={cycle.strategy_code}")
            if broker_account is None:
                raise RuntimeError(f"Unknown broker_account_name={cycle.broker_account_name}")

            intents = list(
                session.scalars(
                    select(TradeIntent)
                    .where(
                        TradeIntent.strategy_id == strategy.id,
                        TradeIntent.broker_account_id == broker_account.id,
                        TradeIntent.symbol == cycle.symbol,
                        TradeIntent.created_at >= window_start,
                        TradeIntent.created_at <= window_end,
                    )
                    .order_by(TradeIntent.created_at.asc())
                ).all()
            )

            orders = list(
                session.scalars(
                    select(BrokerOrder)
                    .where(
                        BrokerOrder.strategy_id == strategy.id,
                        BrokerOrder.broker_account_id == broker_account.id,
                        BrokerOrder.symbol == cycle.symbol,
                        BrokerOrder.updated_at >= window_start,
                        BrokerOrder.updated_at <= window_end,
                    )
                    .order_by(BrokerOrder.updated_at.asc())
                ).all()
            )
            order_ids = [order.id for order in orders]
            intent_ids = [str(intent.id) for intent in intents]

            risk_checks = list(
                session.scalars(
                    select(RiskCheck)
                    .where(RiskCheck.intent_id.in_([intent.id for intent in intents]))
                    .order_by(RiskCheck.created_at.asc())
                ).all()
                if intents
                else []
            )
            order_events = list(
                session.scalars(
                    select(BrokerOrderEvent)
                    .where(BrokerOrderEvent.order_id.in_(order_ids))
                    .order_by(BrokerOrderEvent.event_at.asc())
                ).all()
                if order_ids
                else []
            )
            fills = list(
                session.scalars(
                    select(Fill)
                    .where(
                        Fill.strategy_id == strategy.id,
                        Fill.broker_account_id == broker_account.id,
                        Fill.symbol == cycle.symbol,
                        Fill.filled_at >= window_start,
                        Fill.filled_at <= window_end,
                    )
                    .order_by(Fill.filled_at.asc())
                ).all()
            )

            interval_secs = self._infer_interval_secs(
                session=session,
                strategy_code=cycle.strategy_code,
                symbol=cycle.symbol,
                entry_dt=entry_dt,
            )
            bars_before, bars_during, bars_after = self._load_bar_context(
                session=session,
                strategy_code=cycle.strategy_code,
                symbol=cycle.symbol,
                interval_secs=interval_secs,
                entry_dt=entry_dt,
                exit_dt=exit_dt,
            )

            similar_reviews = list(
                session.scalars(
                    select(AiTradeReview)
                    .where(
                        AiTradeReview.strategy_code == cycle.strategy_code,
                        AiTradeReview.broker_account_name == cycle.broker_account_name,
                        AiTradeReview.symbol == cycle.symbol,
                    )
                    .order_by(desc(AiTradeReview.created_at))
                    .limit(self.config.max_similar_trades)
                ).all()
            )

            primary_intent = next(
                (intent for intent in intents if intent.intent_type == "open" and intent.side == "buy"),
                intents[0] if intents else None,
            )
            return TradeEpisode(
                strategy_code=cycle.strategy_code,
                broker_account_name=cycle.broker_account_name,
                symbol=cycle.symbol,
                cycle_key=cycle.cycle_key,
                entry_time=entry_dt,
                exit_time=exit_dt,
                path=cycle.path,
                quantity=Decimal(str(cycle.quantity)),
                entry_price=Decimal(str(cycle.entry_price)),
                exit_price=Decimal(str(cycle.exit_price)),
                pnl=Decimal(str(cycle.pnl)),
                pnl_pct=cycle.pnl_pct,
                summary=cycle.summary,
                intent_ids=intent_ids,
                primary_intent_id=str(primary_intent.id) if primary_intent is not None else None,
                intents=[
                    {
                        "id": str(intent.id),
                        "symbol": intent.symbol,
                        "side": intent.side,
                        "intent_type": intent.intent_type,
                        "quantity": str(intent.quantity),
                        "reason": intent.reason,
                        "status": intent.status,
                        "metadata": dict((intent.payload or {}).get("metadata", {}))
                        if isinstance((intent.payload or {}).get("metadata", {}), dict)
                        else {},
                        "created_at": intent.created_at,
                        "updated_at": intent.updated_at,
                    }
                    for intent in intents
                ],
                risk_checks=[
                    {
                        "id": str(check.id),
                        "outcome": check.outcome,
                        "reason": check.reason,
                        "payload": dict(check.payload or {}),
                        "created_at": check.created_at,
                    }
                    for check in risk_checks
                ],
                orders=[
                    {
                        "id": str(order.id),
                        "intent_id": str(order.intent_id) if order.intent_id else None,
                        "client_order_id": order.client_order_id,
                        "broker_order_id": order.broker_order_id,
                        "symbol": order.symbol,
                        "side": order.side,
                        "order_type": order.order_type,
                        "time_in_force": order.time_in_force,
                        "quantity": str(order.quantity),
                        "status": order.status,
                        "payload": dict(order.payload or {}),
                        "submitted_at": order.submitted_at,
                        "updated_at": order.updated_at,
                    }
                    for order in orders
                ],
                order_events=[
                    {
                        "id": str(event.id),
                        "order_id": str(event.order_id),
                        "event_type": event.event_type,
                        "event_at": event.event_at,
                        "payload": dict(event.payload or {}),
                    }
                    for event in order_events
                ],
                fills=[
                    {
                        "id": str(fill.id),
                        "order_id": str(fill.order_id),
                        "broker_fill_id": fill.broker_fill_id,
                        "side": fill.side,
                        "quantity": str(fill.quantity),
                        "price": str(fill.price),
                        "filled_at": fill.filled_at,
                        "payload": dict(fill.payload or {}),
                    }
                    for fill in fills
                ],
                bars_before=bars_before,
                bars_during=bars_during,
                bars_after=bars_after,
                similar_reviews=[
                    {
                        "cycle_key": review.cycle_key,
                        "verdict": review.verdict,
                        "action": review.action,
                        "summary": review.summary,
                        "created_at": review.created_at,
                    }
                    for review in similar_reviews
                ],
            )

    def save_review(
        self,
        *,
        cycle: CompletedTradeCycle,
        review_payload: dict[str, Any],
        provider: str,
        model: str,
        primary_intent_id: str | None,
    ) -> None:
        with self.session_factory() as session:
            session.add(
                AiTradeReview(
                    intent_id=UUID(primary_intent_id) if primary_intent_id else None,
                    strategy_code=cycle.strategy_code,
                    broker_account_name=cycle.broker_account_name,
                    symbol=cycle.symbol,
                    review_type=self.config.review_type,
                    cycle_key=cycle.cycle_key,
                    provider=provider,
                    model=model,
                    verdict=str(review_payload.get("verdict", "") or ""),
                    action=str(review_payload.get("action", "") or ""),
                    confidence=Decimal(str(review_payload.get("confidence", "0") or "0")),
                    summary=str(review_payload.get("concise_summary", "") or ""),
                    payload=review_payload,
                )
            )
            session.commit()

    def _load_recent_orders(
        self,
        *,
        session: Session,
        strategy_code: str,
        broker_account_name: str,
        session_start: datetime,
        session_end: datetime,
    ) -> list[dict[str, Any]]:
        strategy = session.scalar(select(Strategy).where(Strategy.code == strategy_code))
        broker_account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == broker_account_name))
        if strategy is None or broker_account is None:
            return []

        latest_order_event_by_order: dict[Any, BrokerOrderEvent] = {}
        for entry in session.scalars(
            select(BrokerOrderEvent)
            .join(BrokerOrder, BrokerOrder.id == BrokerOrderEvent.order_id)
            .where(
                BrokerOrder.strategy_id == strategy.id,
                BrokerOrder.broker_account_id == broker_account.id,
                BrokerOrderEvent.event_at >= session_start,
                BrokerOrderEvent.event_at < session_end,
            )
            .order_by(desc(BrokerOrderEvent.event_at))
        ).all():
            latest_order_event_by_order.setdefault(entry.order_id, entry)

        rows: list[dict[str, Any]] = []
        for order in session.scalars(
            select(BrokerOrder)
            .where(
                BrokerOrder.strategy_id == strategy.id,
                BrokerOrder.broker_account_id == broker_account.id,
                BrokerOrder.updated_at >= session_start,
                BrokerOrder.updated_at < session_end,
            )
            .order_by(desc(BrokerOrder.updated_at))
        ).all():
            intent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
            latest_event = latest_order_event_by_order.get(order.id)
            latest_event_payload = (
                latest_event.payload if latest_event is not None and isinstance(latest_event.payload, dict) else {}
            )
            intent_payload = intent.payload if intent is not None and isinstance(intent.payload, dict) else {}
            intent_metadata = (
                intent_payload.get("metadata", {})
                if isinstance(intent_payload.get("metadata", {}), dict)
                else {}
            )
            rows.append(
                {
                    "strategy_code": strategy_code,
                    "broker_account_name": broker_account_name,
                    "symbol": order.symbol,
                    "side": order.side,
                    "intent_type": intent.intent_type if intent is not None else "",
                    "quantity": str(order.quantity),
                    "price": str(latest_event_payload.get("fill_price") or ""),
                    "status": order.status,
                    "reason": str(latest_event_payload.get("reason") or (intent.reason if intent else "")),
                    "path": str(intent_metadata.get("path") or ""),
                    "updated_at": self._datetime_str(order.updated_at),
                }
            )
        return rows

    def _load_recent_fills(
        self,
        *,
        session: Session,
        strategy_code: str,
        broker_account_name: str,
        session_start: datetime,
        session_end: datetime,
    ) -> list[dict[str, Any]]:
        strategy = session.scalar(select(Strategy).where(Strategy.code == strategy_code))
        broker_account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == broker_account_name))
        if strategy is None or broker_account is None:
            return []

        rows: list[dict[str, Any]] = []
        for fill in session.scalars(
            select(Fill)
            .where(
                Fill.strategy_id == strategy.id,
                Fill.broker_account_id == broker_account.id,
                Fill.filled_at >= session_start,
                Fill.filled_at < session_end,
            )
            .order_by(desc(Fill.filled_at))
        ).all():
            rows.append(
                {
                    "strategy_code": strategy_code,
                    "broker_account_name": broker_account_name,
                    "symbol": fill.symbol,
                    "side": fill.side,
                    "quantity": str(fill.quantity),
                    "price": str(fill.price),
                    "filled_at": self._datetime_str(fill.filled_at),
                }
            )
        return rows

    def _infer_interval_secs(
        self,
        *,
        session: Session,
        strategy_code: str,
        symbol: str,
        entry_dt: datetime,
    ) -> int:
        row = session.scalar(
            select(StrategyBarHistory)
            .where(
                StrategyBarHistory.strategy_code == strategy_code,
                StrategyBarHistory.symbol == symbol,
                StrategyBarHistory.bar_time <= entry_dt,
            )
            .order_by(desc(StrategyBarHistory.bar_time))
        )
        return int(row.interval_secs) if row is not None else 30

    def _load_bar_context(
        self,
        *,
        session: Session,
        strategy_code: str,
        symbol: str,
        interval_secs: int,
        entry_dt: datetime,
        exit_dt: datetime,
    ) -> tuple[list[EpisodeBarSnapshot], list[EpisodeBarSnapshot], list[EpisodeBarSnapshot]]:
        before_start = entry_dt - timedelta(seconds=interval_secs * self.config.context_bars)
        after_end = exit_dt + timedelta(seconds=interval_secs * self.config.review_bars_after_exit)
        rows = list(
            session.scalars(
                select(StrategyBarHistory)
                .where(
                    StrategyBarHistory.strategy_code == strategy_code,
                    StrategyBarHistory.symbol == symbol,
                    StrategyBarHistory.interval_secs == interval_secs,
                    StrategyBarHistory.bar_time >= before_start,
                    StrategyBarHistory.bar_time <= after_end,
                )
                .order_by(StrategyBarHistory.bar_time.asc())
            ).all()
        )
        before: list[EpisodeBarSnapshot] = []
        during: list[EpisodeBarSnapshot] = []
        after: list[EpisodeBarSnapshot] = []
        for row in rows:
            snapshot = EpisodeBarSnapshot(
                bar_time=row.bar_time,
                interval_secs=row.interval_secs,
                open_price=row.open_price,
                high_price=row.high_price,
                low_price=row.low_price,
                close_price=row.close_price,
                volume=row.volume,
                trade_count=row.trade_count,
                position_state=row.position_state,
                position_quantity=row.position_quantity,
                decision_status=row.decision_status,
                decision_reason=row.decision_reason,
                decision_path=row.decision_path,
                decision_score=row.decision_score,
                decision_score_details=row.decision_score_details,
                indicators=dict(row.indicators_json or {}),
            )
            if row.bar_time < entry_dt:
                before.append(snapshot)
            elif row.bar_time <= exit_dt:
                during.append(snapshot)
            else:
                after.append(snapshot)
        return before, during, after

    @staticmethod
    def _datetime_str(value: datetime | None) -> str:
        if value is None:
            return ""
        return value.astimezone(UTC).astimezone(EASTERN_TZ).strftime("%Y-%m-%d %I:%M:%S %p ET")
