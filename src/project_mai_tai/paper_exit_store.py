"""Persistence and authoritative fill census for the paper-exit harness."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
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

LIVE_V2_PROVIDER = "schwab"
LIVE_V2_ACCOUNT_NAME = "live:schwab_1m_v2"
LIVE_ACCOUNT_ENVIRONMENTS = frozenset({"live", "production"})
PAPER_EXIT_EVIDENCE_CUTOVER_SHA = "028817d8be8639c8e48aad648ef822a0abd18de5"
PAPER_EXIT_INITIAL_CONFIG_AUTHOR = "migration-initial-v1"


@dataclass(frozen=True)
class PaperReportWindow:
    status: str
    start: datetime
    end: datetime
    boundary_at: datetime | None

    @property
    def evidence_start(self) -> datetime | None:
        if self.boundary_at is None or self.end < self.boundary_at:
            return None
        return max(self.start, self.boundary_at)

    def payload(self) -> dict[str, object]:
        boundary_at = self.boundary_at.isoformat() if self.boundary_at is not None else ""
        if self.status == "READY":
            reason = "window is wholly after the paper-evidence cutover"
        elif self.status == "REFUSED_SPANS_EVIDENCE_CUTOVER":
            reason = (
                "daily report window crosses the paper evidence-table cutover; "
                "split the reading at the named SHA"
            )
        elif self.status == "REFUSED_BEFORE_EVIDENCE_CUTOVER":
            reason = "paper_exit_events is not the evidence source before the named SHA"
        else:
            reason = "migration-seeded cutover timestamp is missing; report boundary is unknowable"
        return {
            "status": self.status,
            "window_start": self.start.isoformat(),
            "window_end": self.end.isoformat(),
            "boundary_sha": PAPER_EXIT_EVIDENCE_CUTOVER_SHA,
            "boundary_at": boundary_at,
            "reason": reason,
        }


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

    def report_window(self, *, start: datetime, end: datetime) -> PaperReportWindow:
        """Refuse reports that would mix legacy order tables with paper_exit_events."""
        with self.session_factory() as session:
            boundary_at = session.scalar(
                select(PaperExitRuleConfig.created_at)
                .where(PaperExitRuleConfig.changed_by == PAPER_EXIT_INITIAL_CONFIG_AUTHOR)
                .order_by(PaperExitRuleConfig.created_at)
                .limit(1)
            )
        if boundary_at is None:
            return PaperReportWindow("COULD_NOT_TELL", start, end, None)
        if boundary_at.tzinfo is None:
            boundary_at = boundary_at.replace(tzinfo=UTC)
        if end < boundary_at:
            status = "REFUSED_BEFORE_EVIDENCE_CUTOVER"
        elif start < boundary_at <= end:
            status = "REFUSED_SPANS_EVIDENCE_CUTOVER"
        else:
            status = "READY"
        return PaperReportWindow(status, start, end, boundary_at)

    def daily_grade(
        self,
        *,
        report_window: PaperReportWindow,
        source_fills: list[PaperSourceFill],
    ) -> dict[str, object]:
        """Return totals only when the complete report window is post-cutover."""
        boundary = report_window.payload()
        if report_window.status != "READY":
            return {
                **boundary,
                "matched": None,
                "total": None,
                "paper_pct": "",
                "real_pct": "",
                "rows": [],
                "halt_suppression": {
                    "status": "COULD_NOT_TELL",
                    "suppressed_triggers": None,
                    "confirmed_halts": None,
                    "denominator": None,
                },
            }
        grades = self.mirror_grades(
            start=report_window.start,
            end=report_window.end,
            source_fills=source_fills,
        )
        halt_suppression = self.halt_suppression_grade(
            start=report_window.start,
            end=report_window.end,
        )
        gradable = [row for row in grades if bool(row.get("gradable"))]
        return {
            **boundary,
            "matched": len(gradable),
            "total": len(grades),
            "paper_pct": str(
                sum((Decimal(str(row["paper_pct"])) for row in gradable), Decimal("0"))
            ),
            "real_pct": str(
                sum((Decimal(str(row["real_pct"])) for row in gradable), Decimal("0"))
            ),
            "rows": grades,
            "halt_suppression": halt_suppression,
        }

    def halt_suppression_grade(self, *, start: datetime, end: datetime) -> dict[str, object]:
        """Report trigger suppression against confirmed halt windows, never as a pass."""
        with self.session_factory() as session:
            event_types = list(
                session.scalars(
                    select(PaperExitEvent.event_type).where(
                        PaperExitEvent.observed_at >= start,
                        PaperExitEvent.observed_at <= end,
                        PaperExitEvent.event_type.in_(
                            ("HALT_CONFIRMED", "HALT_TRIGGER_SUPPRESSED")
                        ),
                    )
                )
            )
        confirmed = event_types.count("HALT_CONFIRMED")
        suppressed = event_types.count("HALT_TRIGGER_SUPPRESSED")
        return {
            "status": "MEASURED" if confirmed else "UNEXERCISED",
            "suppressed_triggers": suppressed,
            "confirmed_halts": confirmed,
            "denominator": confirmed,
        }

    def ensure_initial_config(
        self,
        *,
        target_pct: Decimal,
        stop_pct: Decimal,
        effective_at: datetime,
        confirmation_bars: int = 1,
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
            config = PaperRuleConfig(
                uuid4(), target_pct, stop_pct, effective_at, confirmation_bars
            )
            session.add(
                PaperExitRuleConfig(
                    id=config.id,
                    target_pct=config.target_pct,
                    stop_pct=config.stop_pct,
                    confirmation_bars=config.confirmation_bars,
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
        confirmation_bars: int = 1,
    ) -> PaperRuleConfig:
        config = PaperRuleConfig(
            uuid4(), target_pct, stop_pct, effective_at, confirmation_bars
        )
        with self.session_factory() as session:
            session.add(
                PaperExitRuleConfig(
                    id=config.id,
                    target_pct=config.target_pct,
                    stop_pct=config.stop_pct,
                    confirmation_bars=config.confirmation_bars,
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
            rows = list(
                session.scalars(
                    select(PaperExitEvent).where(
                        PaperExitEvent.arm == "mirror",
                        PaperExitEvent.observed_at >= start,
                        PaperExitEvent.observed_at <= end,
                        PaperExitEvent.event_type.in_(
                            ("MIRROR_ENTRY", "LATE_MIRROR")
                        ),
                        PaperExitEvent.source_fill_id.is_not(None),
                    )
                )
            )
        return {
            row.source_fill_id
            for row in rows
            if row.source_fill_id is not None
            and row.venue == "schwab"
            and str((row.payload or {}).get("entry_slot", "")).lower() == "first"
        }

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
                BrokerAccount.provider == LIVE_V2_PROVIDER,
                BrokerAccount.name == LIVE_V2_ACCOUNT_NAME,
                BrokerAccount.environment.in_(LIVE_ACCOUNT_ENVIRONMENTS),
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
            # The paper population is exactly v2's stamped first-slot resting fills.
            # Reclaims and all other paths are outside the denominator, not refusals.
            if str(metadata.get("cw_entry_slot", "")).strip().lower() != "first":
                continue
            eligible, source = resting_fill_classification(metadata)
            if not eligible:
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
                        venue="schwab",
                        source_fill_id=fill.id,
                        broker_fill_id=fill.broker_fill_id,
                        detail={"reason": source},
                    )
                )
                continue
            if intent is None or intent.intent_type != "open":
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
                        venue="schwab",
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
            entry_slot = str(metadata.get("cw_entry_slot", "")).strip().lower()
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
                        venue="schwab",
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
                    venue="schwab",
                    symbol=fill.symbol,
                    quantity=fill.quantity,
                    price=fill.price,
                    filled_at=filled_at,
                    fanout_slot_id=slot_id,
                    entry_slot=entry_slot,  # type: ignore[arg-type]
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
                        PaperExitEvent.arm == "mirror",
                        PaperExitEvent.observed_at >= start,
                        PaperExitEvent.observed_at <= end,
                    )
                    .order_by(PaperExitEvent.observed_at, PaperExitEvent.created_at)
                )
            )

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
                        BrokerAccount.provider == LIVE_V2_PROVIDER,
                        BrokerAccount.name == LIVE_V2_ACCOUNT_NAME,
                        BrokerAccount.environment.in_(LIVE_ACCOUNT_ENVIRONMENTS),
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
                reason = "paper exit predates source fill"
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
        return PaperRuleConfig(
            row.id,
            Decimal(row.target_pct),
            Decimal(row.stop_pct),
            effective_at,
            int(row.confirmation_bars or 1),
        )
