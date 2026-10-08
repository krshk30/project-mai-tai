"""FALSEFLIP1 off-loop evidence and CAS budget persistence. No broker operations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from threading import RLock

from sqlalchemy import func, select, tuple_, update

from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, DashboardSnapshot, Fill, OmsManagedPosition, Strategy,
)
from project_mai_tai.falseflip1 import EntryBarClose, EntryIdentity, classify_entry
from project_mai_tai.fanout_segment_store import current_session_anchor

BAR_TYPE = "v2_falseflip_entry_bar"
BUDGET_TYPE = "v2_falseflip_budget"
_BUDGET_LOCK = RLock()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class FalseBudget:
    symbol: str
    segment: int
    episodes: tuple[int, ...] = ()
    refunded_counts: tuple[int, ...] = ()
    skipped: bool = False
    revision: int = 0
    cancelled: tuple[int, ...] = ()
    drafted: tuple[int, ...] = ()

    @property
    def awaiting_rearm(self) -> int:
        return next((value for value in reversed(self.cancelled) if value not in self.drafted), 0)

    def cancellation_confirmed(self, opportunity: int) -> FalseBudget:
        if opportunity not in self.episodes:
            raise ValueError("cancellation has no classified episode")
        if opportunity in self.cancelled:
            return self
        return replace(self, cancelled=(*self.cancelled, opportunity), revision=self.revision + 1)

    def rest_drafted(self, opportunity: int) -> FalseBudget:
        if opportunity not in self.cancelled:
            raise ValueError("rest has no confirmed cancellation")
        if opportunity in self.drafted:
            return self
        return replace(self, drafted=(*self.drafted, opportunity), revision=self.revision + 1)

    @property
    def skip_due(self) -> bool:
        return len(self.episodes) >= 2 and not self.skipped

    def closed(self, opportunity: int, refund_count: int = 0) -> FalseBudget:
        if opportunity in self.episodes:
            return self
        refunds = (*self.refunded_counts, refund_count) if refund_count else self.refunded_counts
        return replace(self, episodes=(*self.episodes, opportunity), refunded_counts=refunds,
                       revision=self.revision + 1)

    def skip(self) -> FalseBudget:
        if not self.skip_due:
            raise ValueError("no false-flip cross owed")
        return replace(self, skipped=True, revision=self.revision + 1)

    def payload(self) -> dict:
        return {**asdict(self), "episodes": list(self.episodes),
                "refunded_counts": list(self.refunded_counts), "schema_version": 1}

    @classmethod
    def read(cls, payload: dict) -> FalseBudget:
        result = cls(str(payload["symbol"]), int(payload["segment"]),
                     tuple(int(value) for value in payload["episodes"]),
                     tuple(int(value) for value in payload["refunded_counts"]),
                     payload["skipped"], int(payload["revision"]),
                     tuple(int(value) for value in payload.get("cancelled", ())),
                     tuple(int(value) for value in payload.get("drafted", ())))
        if (payload.get("schema_version") != 1 or not result.symbol or result.segment <= 0
                or type(result.skipped) is not bool or result.revision < 0
                or len(set(result.episodes)) != len(result.episodes)
                or any(value <= 0 for value in result.episodes)
                or len(set(result.refunded_counts)) != len(result.refunded_counts)
                or any(value <= 0 for value in result.refunded_counts)
                or len(result.refunded_counts) > len(result.episodes)
                or len(set(result.cancelled)) != len(result.cancelled)
                or len(set(result.drafted)) != len(result.drafted)
                or not set(result.cancelled) <= set(result.episodes)
                or not set(result.drafted) <= set(result.cancelled)
                or (result.skipped and len(result.episodes) < 2)):
            raise ValueError("false-flip budget unreadable")
        return result


class FalseFlipStore:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def restore(self, *, now: datetime | None = None) -> dict[tuple[str, int], FalseBudget]:
        with self.session_factory() as session:
            rows = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == BUDGET_TYPE,
                DashboardSnapshot.created_at >= current_session_anchor(now),
            ).order_by(DashboardSnapshot.created_at, DashboardSnapshot.id).limit(4097)).all()
        if len(rows) > 4096:
            raise ValueError("false-flip budget journal exceeds bound")
        budgets = {}
        for row in rows:
            value = FalseBudget.read(row.payload)
            key = (value.symbol, value.segment)
            prior = budgets.get(key)
            if prior is not None and value.revision == prior.revision and value != prior:
                raise ValueError("false-flip budget revision conflicted")
            if prior is None or value.revision > prior.revision:
                budgets[key] = value
        return budgets

    def commit(self, before: FalseBudget, after: FalseBudget, *, now: datetime | None = None) -> None:
        if (before.symbol, before.segment) != (after.symbol, after.segment):
            raise ValueError("false-flip budget identity changed")
        at = now or datetime.now(UTC)
        with _BUDGET_LOCK, self.session_factory() as session:
            if session.bind.dialect.name == "postgresql":
                # Serialize the initial empty-key write too, not just existing rows.
                key = int.from_bytes(sha256(f"falseflip:{before.symbol}:{before.segment}".encode()).digest()[:8],
                                     "big", signed=True)
                session.execute(select(func.pg_advisory_xact_lock(key)))
            # Sole v2 writer; row lock also rejects an accidentally competing writer.
            rows = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == BUDGET_TYPE,
                DashboardSnapshot.created_at >= current_session_anchor(at),
                DashboardSnapshot.payload["symbol"].as_string() == before.symbol,
                DashboardSnapshot.payload["segment"].as_integer() == before.segment,
            ).order_by(DashboardSnapshot.payload["revision"].as_integer().desc())
                .limit(1).with_for_update()).all()
            stored = FalseBudget.read(rows[0].payload) if rows else FalseBudget(before.symbol, before.segment)
            if stored == after:
                return
            if stored != before or after.revision != before.revision + 1:
                raise ValueError("false-flip budget compare-and-set refused")
            session.add(DashboardSnapshot(snapshot_type=BUDGET_TYPE, payload=after.payload(), created_at=at))
            session.commit()


def record_bar(session_factory, payload: dict, *, now: datetime | None = None) -> None:
    bar = EntryBarClose(str(payload["symbol"]), int(payload["bar_ms"]),
                        int(payload["observed_at_ms"]), Decimal(payload["close"]),
                        Decimal(payload["trail"]), str(payload["state"]), str(payload["phase"]))
    now = now or datetime.now(UTC)
    if (bar.observation_phase != "live" or bar.bar_ms % 60000
            or bar.observed_at_ms < bar.bar_ms + 60000
            or bar.observed_at_ms > int(now.timestamp() * 1000)
            or current_session_anchor(datetime.fromtimestamp(bar.bar_ms / 1000, UTC))
            != current_session_anchor(now)):
        raise ValueError("entry-bar observation is not a completed live session bar")
    with session_factory() as session:
        rows = session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == BAR_TYPE,
            DashboardSnapshot.created_at >= current_session_anchor(now),
            DashboardSnapshot.payload["symbol"].as_string() == bar.symbol,
            DashboardSnapshot.payload["bar_ms"].as_integer() == bar.bar_ms,
        )).all()
        evidence = {"symbol": bar.symbol, "bar_ms": bar.bar_ms, "close": str(bar.close),
                    "trail": str(bar.trail), "state": bar.state, "phase": "live"}
        if rows:
            if any({key: row.payload.get(key) for key in evidence} != evidence for row in rows):
                for row in rows:
                    row.payload = {**row.payload, "conflicting": True}
                session.commit()
            return
        session.add(DashboardSnapshot(snapshot_type=BAR_TYPE,
                                      payload={**evidence, "observed_at_ms": bar.observed_at_ms}, created_at=now))
        session.commit()


def classify_managed_entries(session_factory, *, now: datetime | None = None) -> int:
    """OMS is the only managed-row writer, including retrospective closed entries."""
    anchor = current_session_anchor(now)
    changed = 0
    with session_factory() as session:
        entries = session.execute(select(OmsManagedPosition, BrokerOrder, BrokerAccount)
            .join(BrokerOrder, BrokerOrder.id == OmsManagedPosition.entry_order_id)
            .join(BrokerAccount, BrokerAccount.id == BrokerOrder.broker_account_id)
            .where(OmsManagedPosition.strategy_code == "schwab_1m_v2",
                   OmsManagedPosition.entry_time >= anchor,
                   OmsManagedPosition.entry_classification.is_(None)).limit(513)).all()
        if len(entries) > 512:
            raise ValueError("false-flip pending entry census exceeds bound")
        if not entries:
            return 0
        all_fills = session.scalars(select(Fill).where(
            Fill.order_id.in_([order.id for _, order, _ in entries]), Fill.side == "buy")
            .order_by(Fill.filled_at).limit(2049)).all()
        if len(all_fills) > 2048:
            raise ValueError("false-flip fill census exceeds bound")
        fill_map = {}
        for fill in all_fills:
            fill_map.setdefault(fill.order_id, []).append(fill)
        keys = {(fill.symbol, int(_utc(fill.filled_at).timestamp() * 1000) // 60000 * 60000)
                for fill in all_fills}
        bars = session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == BAR_TYPE, DashboardSnapshot.created_at >= anchor,
            tuple_(DashboardSnapshot.payload["symbol"].as_string(),
                   DashboardSnapshot.payload["bar_ms"].as_integer()).in_(keys)).limit(2049)).all()
        if len(bars) > 2048:
            raise ValueError("false-flip bar proof census exceeds bound")
        by_bar = {(p.payload["symbol"], int(p.payload["bar_ms"])): p.payload for p in bars}
        strategy_ids = set(session.scalars(select(Strategy.id).where(Strategy.code == "schwab_1m_v2")))
        for row, order, account in entries:
            fills = fill_map.get(order.id, [])
            if not fills:
                continue
            if (order.side != "buy" or order.strategy_id not in strategy_ids
                    or any(f.strategy_id != order.strategy_id or f.broker_account_id != account.id
                           or f.symbol != row.symbol for f in fills)
                    or sum(Decimal(f.quantity) for f in fills) != row.original_quantity):
                continue  # No classification refund from foreign or incomplete fill evidence.
            minutes = {int(_utc(f.filled_at).timestamp() * 1000) // 60000 * 60000 for f in fills}
            if len(minutes) != 1:
                # Multi-bar partial fills are not one provable entry-bar classification.
                session.execute(update(OmsManagedPosition).where(OmsManagedPosition.id == row.id).values(
                    entry_classification={"classification": "UNKNOWN", "reason": "multi_bar_fill"},
                    updated_at=OmsManagedPosition.updated_at))
                continue
            p = by_bar.get((row.symbol, next(iter(minutes))))
            if p is None:
                continue
            metadata = order.payload or {}
            identity = EntryIdentity(row.symbol, account.name, str(row.id), str(row.entry_order_id),
                row.entry_client_order_id or "", str(order.id), order.client_order_id,
                int(_utc(fills[0].filled_at).timestamp() * 1000),
                int(metadata.get("fanout_segment_id", 0)), str(metadata.get("fanout_slot_id", "")),
                row.broker_account_name, row.symbol, account.name, order.symbol)
            bar = EntryBarClose(p["symbol"], int(p["bar_ms"]), int(p["observed_at_ms"]),
                               Decimal(p["close"]), Decimal(p["trail"]),
                               "unknown" if p.get("conflicting") else p["state"])
            result = classify_entry(identity, bar).as_payload()
            if row.entry_classification != result:
                # Neither a retrospective label nor its order-side display copy is a
                # new close/fill/order event. Preserve both original timestamps.
                session.execute(update(OmsManagedPosition).where(OmsManagedPosition.id == row.id).values(
                    entry_classification=result, updated_at=OmsManagedPosition.updated_at))
                session.execute(update(BrokerOrder).where(BrokerOrder.id == order.id).values(
                    payload={**metadata, "entry_classification": result}, updated_at=BrokerOrder.updated_at))
                changed += 1
        session.commit()
    return changed
