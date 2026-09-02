"""Persistence and authoritative fill census for the paper-exit harness."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from collections import defaultdict, deque
from uuid import UUID, uuid4

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import (
    BrokerAccount,
    BrokerOrder,
    Fill,
    PaperExitEvent,
    PaperExitRuleConfig,
    Strategy,
    TradeIntent,
)
from project_mai_tai.paper_exit import (
    EASTERN,
    PaperDecision,
    PaperRuleConfig,
    PaperSourceFill,
    logical_mirror_id,
    resting_fill_classification,
)

LIVE_VENUES = frozenset({"schwab", "webull"})


class PaperExitStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def active_config(self, at: datetime) -> PaperRuleConfig:
        with self.session_factory() as session:
            row = session.scalar(
                select(PaperExitRuleConfig)
                .where(PaperExitRuleConfig.effective_at <= at)
                .order_by(PaperExitRuleConfig.effective_at.desc(), PaperExitRuleConfig.created_at.desc())
                .limit(1)
            )
        if row is None:
            raise RuntimeError("no paper-exit rule config is effective")
        return self._config(row)

    def latest_config(self) -> PaperRuleConfig:
        with self.session_factory() as session:
            row = session.scalar(
                select(PaperExitRuleConfig)
                .order_by(PaperExitRuleConfig.effective_at.desc(), PaperExitRuleConfig.created_at.desc())
                .limit(1)
            )
        if row is None:
            raise RuntimeError("no paper-exit rule config exists")
        return self._config(row)

    def configs(self) -> list[PaperRuleConfig]:
        with self.session_factory() as session:
            rows = list(
                session.scalars(
                    select(PaperExitRuleConfig).order_by(
                        PaperExitRuleConfig.effective_at,
                        PaperExitRuleConfig.created_at,
                    )
                )
            )
        return [self._config(row) for row in rows]

    def ensure_initial_config(
        self,
        *,
        target_pct: Decimal,
        stop_pct: Decimal,
        effective_at: datetime,
    ) -> PaperRuleConfig:
        """Create the bootstrap row once; every later change remains append-only."""
        with self.session_factory() as session:
            row = session.scalar(
                select(PaperExitRuleConfig)
                .order_by(
                    PaperExitRuleConfig.effective_at.desc(),
                    PaperExitRuleConfig.created_at.desc(),
                )
                .limit(1)
            )
            if row is not None:
                return self._config(row)
            config = PaperRuleConfig(uuid4(), target_pct, stop_pct, effective_at)
            session.add(
                PaperExitRuleConfig(
                    id=config.id,
                    target_pct=config.target_pct,
                    stop_pct=config.stop_pct,
                    effective_at=config.effective_at,
                    changed_by="runtime-bootstrap",
                )
            )
            session.commit()
            return config

    def append_config(
        self,
        *,
        target_pct: Decimal,
        stop_pct: Decimal,
        effective_at: datetime,
        changed_by: str,
    ) -> PaperRuleConfig:
        config = PaperRuleConfig(uuid4(), target_pct, stop_pct, effective_at)
        with self.session_factory() as session:
            session.add(
                PaperExitRuleConfig(
                    id=config.id,
                    target_pct=config.target_pct,
                    stop_pct=config.stop_pct,
                    effective_at=config.effective_at,
                    changed_by=changed_by,
                )
            )
            session.commit()
        return config

    def append_decisions(self, decisions: list[PaperDecision]) -> int:
        if not decisions:
            return 0
        inserted = 0
        with self.session_factory() as session:
            existing = set(
                session.scalars(
                    select(PaperExitEvent.event_key).where(
                        PaperExitEvent.event_key.in_([decision.event_key for decision in decisions])
                    )
                )
            )
            for decision in decisions:
                if decision.event_key in existing:
                    continue
                session.add(
                    PaperExitEvent(
                        event_key=decision.event_key,
                        logical_id=decision.logical_id,
                        arm=decision.arm,
                        event_type=decision.event_type,
                        session_date=decision.session_date,
                        symbol=decision.symbol,
                        venue=decision.venue,
                        source_fill_id=decision.source_fill_id,
                        broker_fill_id=decision.broker_fill_id,
                        config_id=decision.config_id,
                        observed_at=decision.observed_at,
                        price=decision.price,
                        quantity=decision.quantity,
                        payload=dict(decision.detail),
                    )
                )
                inserted += 1
            session.commit()
        return inserted

    def event_fill_ids(self, *, start: datetime, end: datetime) -> set[UUID]:
        with self.session_factory() as session:
            return set(
                session.scalars(
                    select(PaperExitEvent.source_fill_id).where(
                        PaperExitEvent.observed_at >= start,
                        PaperExitEvent.observed_at <= end,
                        PaperExitEvent.event_type.in_(
                            ("MIRROR_ENTRY", "LATE_MIRROR", "MIRROR_LEG_COLLAPSED")
                        ),
                        PaperExitEvent.source_fill_id.is_not(None),
                    )
                )
            )

    def live_resting_fills(
        self, *, start: datetime, end: datetime
    ) -> tuple[list[PaperSourceFill], list[PaperDecision]]:
        statement: Select[tuple[Fill, BrokerOrder, BrokerAccount, TradeIntent | None]] = (
            select(Fill, BrokerOrder, BrokerAccount, TradeIntent)
            .join(BrokerOrder, BrokerOrder.id == Fill.order_id)
            .join(BrokerAccount, BrokerAccount.id == Fill.broker_account_id)
            .join(Strategy, Strategy.id == Fill.strategy_id)
            .outerjoin(TradeIntent, TradeIntent.id == BrokerOrder.intent_id)
            .where(
                Strategy.code == "schwab_1m_v2",
                BrokerAccount.provider.in_(LIVE_VENUES),
                BrokerAccount.environment == "live",
                Fill.side == "buy",
                BrokerOrder.side == "buy",
                Fill.filled_at >= start,
                Fill.filled_at <= end,
            )
            .order_by(Fill.filled_at, Fill.id)
        )
        fills: list[PaperSourceFill] = []
        refused: list[PaperDecision] = []
        with self.session_factory() as session:
            rows = session.execute(statement).all()
        for fill, order, account, intent in rows:
            filled_at = fill.filled_at
            if filled_at.tzinfo is None:
                filled_at = filled_at.replace(tzinfo=UTC)
            payload = dict(fill.payload or {})
            order_payload = dict(order.payload or {})
            metadata = {
                **dict(order_payload.get("metadata") or order_payload),
                **dict(payload.get("metadata") or {}),
            }
            eligible, source = resting_fill_classification(metadata)
            if not eligible:
                slot = str(metadata.get("cw_entry_slot", "")).strip().lower()
                if slot == "reclaim":
                    continue
                venue = "schwab" if account.provider == "schwab" else "webull"
                refused.append(
                    PaperDecision(
                        event_key=f"UNANSWERABLE:{fill.id}:classification",
                        logical_id=f"unanswerable:{fill.id}",
                        arm="mirror",
                        event_type="UNANSWERABLE",
                        session_date=filled_at.astimezone(EASTERN).date(),
                        symbol=fill.symbol,
                        observed_at=filled_at,
                        price=fill.price,
                        quantity=fill.quantity,
                        config_id=None,
                        venue=venue,
                        source_fill_id=fill.id,
                        broker_fill_id=fill.broker_fill_id,
                        detail={"reason": source},
                    )
                )
                continue
            if intent is None or intent.intent_type != "open":
                venue = "schwab" if account.provider == "schwab" else "webull"
                refused.append(
                    PaperDecision(
                        event_key=f"UNANSWERABLE:{fill.id}:intent-provenance",
                        logical_id=f"unanswerable:{fill.id}",
                        arm="mirror",
                        event_type="UNANSWERABLE",
                        session_date=filled_at.astimezone(EASTERN).date(),
                        symbol=fill.symbol,
                        observed_at=filled_at,
                        price=fill.price,
                        quantity=fill.quantity,
                        config_id=None,
                        venue=venue,
                        source_fill_id=fill.id,
                        broker_fill_id=fill.broker_fill_id,
                        detail={
                            "reason": (
                                "missing trade intent provenance"
                                if intent is None
                                else f"intent_type={intent.intent_type}"
                            )
                        },
                    )
                )
                continue
            broker_fill_id = str(fill.broker_fill_id or "").strip()
            slot_id = str(metadata.get("fanout_slot_id", "") or "").strip()
            venue = "schwab" if account.provider == "schwab" else "webull"
            if not broker_fill_id or not slot_id:
                detail = "missing broker_fill_id" if not broker_fill_id else "missing fanout_slot_id"
                refused.append(
                    PaperDecision(
                        event_key=f"UNANSWERABLE:{fill.id}:{detail}",
                        logical_id=f"unanswerable:{fill.id}",
                        arm="mirror",
                        event_type="UNANSWERABLE",
                        session_date=filled_at.astimezone(EASTERN).date(),
                        symbol=fill.symbol,
                        observed_at=filled_at,
                        price=fill.price,
                        quantity=fill.quantity,
                        config_id=None,
                        venue=venue,
                        source_fill_id=fill.id,
                        broker_fill_id=fill.broker_fill_id,
                        detail={"reason": detail},
                    )
                )
                continue
            fills.append(
                PaperSourceFill(
                    fill_id=fill.id,
                    broker_fill_id=broker_fill_id,
                    broker_account_name=account.name,
                    venue=venue,
                    symbol=fill.symbol,
                    quantity=fill.quantity,
                    price=fill.price,
                    filled_at=filled_at,
                    fanout_slot_id=slot_id,
                    source=source,
                )
            )
        return fills, refused

    def session_events(self, *, start: datetime, end: datetime) -> list[PaperExitEvent]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(PaperExitEvent)
                    .where(
                        PaperExitEvent.observed_at >= start,
                        PaperExitEvent.observed_at <= end,
                    )
                    .order_by(PaperExitEvent.observed_at, PaperExitEvent.created_at)
                )
            )

    def entry_assumption_rows(
        self,
        *,
        start: datetime,
        end: datetime,
        source_fills: list[PaperSourceFill],
    ) -> list[dict[str, object]]:
        """Put modelled ask fills beside actual mirror fills without pooling the arms."""
        with self.session_factory() as session:
            independent_entries = list(
                session.scalars(
                    select(PaperExitEvent)
                    .where(
                        PaperExitEvent.arm == "independent",
                        PaperExitEvent.event_type == "INDEPENDENT_ENTRY",
                        PaperExitEvent.observed_at >= start,
                        PaperExitEvent.observed_at <= end,
                    )
                    .order_by(PaperExitEvent.observed_at, PaperExitEvent.created_at)
                )
            )

        mirror_groups: dict[tuple[object, str, str], list[PaperSourceFill]] = defaultdict(list)
        for source in source_fills:
            key = (source.session_date, source.symbol.upper(), source.fanout_slot_id)
            mirror_groups[key].append(source)
        independent_groups: dict[tuple[object, str, str], list[PaperExitEvent]] = defaultdict(list)
        for event in independent_entries:
            attempt_id = str((event.payload or {}).get("independent_attempt_id", "")).strip()
            key = (event.session_date, event.symbol.upper(), attempt_id)
            independent_groups[key].append(event)

        rows: list[dict[str, object]] = []
        for key in sorted(
            mirror_groups.keys() | independent_groups.keys(),
            key=lambda item: (str(item[0]), item[1], item[2]),
        ):
            mirror_legs = mirror_groups.get(key, [])
            independent = independent_groups.get(key, [])
            mirror_quantity = sum((leg.quantity for leg in mirror_legs), Decimal("0"))
            mirror_price = (
                sum((leg.price * leg.quantity for leg in mirror_legs), Decimal("0"))
                / mirror_quantity
                if mirror_quantity > 0
                else None
            )
            assumption = independent[0] if len(independent) == 1 else None
            assumed_price = Decimal(assumption.price) if assumption and assumption.price else None
            if not mirror_legs:
                status = "INDEPENDENT_ONLY"
            elif not independent:
                status = "NO_INDEPENDENT_FILL"
            elif len(independent) > 1:
                status = f"AMBIGUOUS_{len(independent)}_INDEPENDENT_FILLS"
            else:
                status = "MATCHED_ASSUMPTION"
            assumed_vs_actual_pct = (
                ((assumed_price / mirror_price) - Decimal("1")) * Decimal("100")
                if assumed_price is not None and mirror_price is not None
                else None
            )
            rows.append(
                {
                    "session_date": str(key[0]),
                    "symbol": key[1],
                    "fanout_slot_id": key[2] or "missing",
                    "mirror_fill_price": str(mirror_price) if mirror_price is not None else "",
                    "mirror_first_fill_at": (
                        min(leg.filled_at for leg in mirror_legs).isoformat()
                        if mirror_legs
                        else ""
                    ),
                    "mirror_last_fill_at": (
                        max(leg.filled_at for leg in mirror_legs).isoformat()
                        if mirror_legs
                        else ""
                    ),
                    "mirror_venues": sorted({leg.venue for leg in mirror_legs}),
                    "mirror_legs": len(mirror_legs),
                    "independent_assumed_fill": (
                        str(assumed_price) if assumed_price is not None else ""
                    ),
                    "independent_assumed_at": (
                        assumption.observed_at.isoformat() if assumption is not None else ""
                    ),
                    "independent_fill_count": len(independent),
                    "assumed_vs_actual_pct": (
                        str(assumed_vs_actual_pct)
                        if assumed_vs_actual_pct is not None
                        else ""
                    ),
                    "status": status,
                }
            )
        return rows

    def mirror_grades(
        self,
        *,
        start: datetime,
        end: datetime,
        source_fills: list[PaperSourceFill],
    ) -> list[dict[str, object]]:
        """Join complete broker exits to exact mirror legs without inventing a close."""
        if not source_fills:
            return []
        tracked_ids = {item.fill_id for item in source_fills}
        symbols = {item.symbol.upper() for item in source_fills}
        with self.session_factory() as session:
            fills = list(
                session.execute(
                    select(Fill, BrokerAccount)
                    .join(BrokerAccount, BrokerAccount.id == Fill.broker_account_id)
                    .join(Strategy, Strategy.id == Fill.strategy_id)
                    .where(
                        Strategy.code == "schwab_1m_v2",
                        BrokerAccount.provider.in_(LIVE_VENUES),
                        BrokerAccount.environment == "live",
                        Fill.symbol.in_(symbols),
                        Fill.filled_at <= end,
                    )
                    .order_by(Fill.filled_at, Fill.id)
                )
            )
            paper_exits = list(
                session.scalars(
                    select(PaperExitEvent)
                    .where(
                        PaperExitEvent.arm == "mirror",
                        PaperExitEvent.event_type == "PAPER_EXIT",
                        PaperExitEvent.observed_at >= start,
                        PaperExitEvent.observed_at <= end,
                    )
                    .order_by(PaperExitEvent.observed_at, PaperExitEvent.created_at)
                )
            )
            unanswerable_ids = set(
                session.scalars(
                    select(PaperExitEvent.logical_id).where(
                        PaperExitEvent.arm == "mirror",
                        PaperExitEvent.event_type == "UNANSWERABLE",
                        PaperExitEvent.observed_at >= start,
                        PaperExitEvent.observed_at <= end,
                    )
                )
            )

        inventory: dict[tuple[UUID, str], deque[dict[str, object]]] = defaultdict(deque)
        exits: dict[UUID, dict[str, object]] = {
            fill_id: {"quantity": Decimal("0"), "proceeds": Decimal("0"), "exit_at": None}
            for fill_id in tracked_ids
        }
        for row, account in fills:
            filled_at = row.filled_at
            if filled_at.tzinfo is None:
                filled_at = filled_at.replace(tzinfo=UTC)
            key = (account.id, row.symbol.upper())
            if row.side == "buy":
                inventory[key].append(
                    {
                        "fill_id": row.id,
                        "remaining": Decimal(row.quantity),
                        "price": Decimal(row.price),
                    }
                )
                continue
            if row.side != "sell":
                continue
            remaining = Decimal(row.quantity)
            while remaining > 0 and inventory[key]:
                lot = inventory[key][0]
                take = min(remaining, Decimal(lot["remaining"]))
                fill_id = lot["fill_id"]
                if fill_id in tracked_ids:
                    exits[fill_id]["quantity"] = Decimal(exits[fill_id]["quantity"]) + take
                    exits[fill_id]["proceeds"] = (
                        Decimal(exits[fill_id]["proceeds"]) + take * Decimal(row.price)
                    )
                    exits[fill_id]["exit_at"] = filled_at
                lot["remaining"] = Decimal(lot["remaining"]) - take
                remaining -= take
                if Decimal(lot["remaining"]) <= 0:
                    inventory[key].popleft()

        paper_by_logical = {row.logical_id: row for row in paper_exits}
        grouped: dict[str, list[PaperSourceFill]] = defaultdict(list)
        for source in source_fills:
            grouped[logical_mirror_id(source)].append(source)
        grades: list[dict[str, object]] = []
        for logical_id, legs in sorted(
            grouped.items(), key=lambda item: min(leg.filled_at for leg in item[1])
        ):
            entry_cost = sum((leg.quantity * leg.price for leg in legs), Decimal("0"))
            quantity = sum((leg.quantity for leg in legs), Decimal("0"))
            exited_quantity = sum(
                (Decimal(exits[leg.fill_id]["quantity"]) for leg in legs), Decimal("0")
            )
            proceeds = sum(
                (Decimal(exits[leg.fill_id]["proceeds"]) for leg in legs), Decimal("0")
            )
            paper = paper_by_logical.get(logical_id)
            reason = ""
            if logical_id in unanswerable_ids:
                reason = "paper exit unanswerable"
            elif paper is None or paper.price is None:
                reason = "paper exit missing"
            elif paper.observed_at.replace(tzinfo=paper.observed_at.tzinfo or UTC) < max(
                leg.filled_at for leg in legs
            ):
                reason = "paper exit predates a collapsed source leg"
            elif paper.quantity is None or Decimal(paper.quantity) != quantity:
                reason = f"paper exit quantity mismatch ({paper.quantity}/{quantity})"
            elif exited_quantity != quantity:
                reason = f"live exit incomplete ({exited_quantity}/{quantity})"
            paper_pct = (
                ((Decimal(paper.price) * quantity / entry_cost) - Decimal("1")) * Decimal("100")
                if not reason and entry_cost > 0
                else None
            )
            real_pct = (
                ((proceeds / entry_cost) - Decimal("1")) * Decimal("100")
                if not reason and entry_cost > 0
                else None
            )
            grades.append(
                {
                    "logical_id": logical_id,
                    "symbol": legs[0].symbol,
                    "venues": sorted({leg.venue for leg in legs}),
                    "source_legs": len(legs),
                    "quantity": str(quantity),
                    "paper_pct": str(paper_pct) if paper_pct is not None else "",
                    "real_pct": str(real_pct) if real_pct is not None else "",
                    "gradable": not reason,
                    "reason": reason,
                }
            )
        return grades

    @staticmethod
    def _config(row: PaperExitRuleConfig) -> PaperRuleConfig:
        effective_at = row.effective_at
        if effective_at.tzinfo is None:
            effective_at = effective_at.replace(tzinfo=UTC)
        return PaperRuleConfig(row.id, Decimal(row.target_pct), Decimal(row.stop_pct), effective_at)
