"""[codex] Read-only census shared by reconciliation and reviewed install gates."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, OmsManagedPosition, TradeIntent, VirtualPosition
from project_mai_tai.operator_holdings import operator_holding_basis, trading_session_start


POSITION_PROOF_MAX_AGE_SECONDS = 120
# Unknown/aborted/cancel-pending states are deliberately NOT affirmative terminal proof.
TERMINAL_ORDER_STATUSES = frozenset({"filled", "cancelled", "canceled", "rejected", "expired"})
TERMINAL_INTENT_STATUSES = TERMINAL_ORDER_STATUSES | {"completed"}


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def fresh_at(value: datetime | None, now: datetime) -> bool:
    return (isinstance(value, datetime) and isinstance(now, datetime)
            and value.tzinfo is not None and now.tzinfo is not None
            and 0 <= (now - value).total_seconds() <= POSITION_PROOF_MAX_AGE_SECONDS)


@dataclass
class BotActivity:
    session_orders: int = 0
    session_fills: int = 0
    pending_intents: int = 0
    pending_orders: int = 0
    unowned_sells: int = 0
    open_virtual_rows: int = 0
    open_managed_rows: int = 0
    virtual_quantity: Decimal = Decimal("0")
    managed_quantity: Decimal = Decimal("0")
    net_fill_balance: Decimal = Decimal("0")
    strategy_balances: dict[UUID, Decimal] = field(default_factory=dict)
    complete: bool = True

    def well_formed(self):
        return (self.complete is True and all(type(v) is int and v >= 0 for v in (
            self.session_orders, self.session_fills, self.pending_intents, self.pending_orders,
            self.unowned_sells, self.open_virtual_rows, self.open_managed_rows,
        )) and all(type(v) is Decimal and v.is_finite() for v in (
            self.virtual_quantity, self.managed_quantity, self.net_fill_balance,
            *self.strategy_balances.values(),
        )))

    def basis(self, *, account_name, symbol, quantity, now, source_fresh):
        if not self.well_formed():
            return None
        return operator_holding_basis(
            account_name=account_name, symbol=symbol, now=now, broker_quantity=quantity,
            virtual_quantity=self.virtual_quantity, managed_quantity=self.managed_quantity,
            net_fill_balance=self.net_fill_balance, session_orders=self.session_orders,
            session_fills=self.session_fills, pending_intents=self.pending_intents,
            pending_orders=self.pending_orders, unowned_sells=self.unowned_sells,
            open_virtual_rows=self.open_virtual_rows, open_managed_rows=self.open_managed_rows,
            source_fresh=source_fresh,
            activity_complete=self.complete is True and all(v == 0 for v in self.strategy_balances.values()),
        )

    def blocks_flat(self):
        return (not self.well_formed() or self.virtual_quantity != 0 or self.managed_quantity != 0
                or self.pending_orders != 0 or self.pending_intents != 0
                or self.unowned_sells != 0 or self.open_virtual_rows != 0
                or self.open_managed_rows != 0 or self.net_fill_balance != 0
                or any(v != 0 for v in self.strategy_balances.values()))


def collect_bot_activity(session: Session, accounts: dict[UUID, str], *, now: datetime,
                         fill_balance_since: datetime) -> dict[tuple[UUID, str], BotActivity]:
    """No tolerance, status alias whitelist, writes, or failed-query-to-zero fallback."""
    activity: dict[tuple[UUID, str], BotActivity] = defaultdict(BotActivity)
    ids = tuple(accounts)
    if not ids:
        raise ValueError("operator census requires an explicit account inventory")
    start = trading_session_start(now)
    if fill_balance_since.tzinfo is None or fill_balance_since > start:
        raise ValueError("operator census historical checkpoint is unknown or inside this session")
    for row in session.scalars(select(VirtualPosition).where(VirtualPosition.broker_account_id.in_(ids))):
        data = activity[row.broker_account_id, row.symbol]
        data.virtual_quantity += row.quantity
        if row.quantity != 0 or row.opened_at is not None:
            data.open_virtual_rows += 1
    by_name = {name: account_id for account_id, name in accounts.items()}
    for row in session.scalars(select(OmsManagedPosition).where(
        OmsManagedPosition.broker_account_name.in_(tuple(by_name)),
        OmsManagedPosition.status != "closed",
    )):
        data = activity[by_name[row.broker_account_name], row.symbol]
        data.open_managed_rows += 1
        data.managed_quantity += row.current_quantity

    order_time = func.coalesce(TradeIntent.created_at, BrokerOrder.submitted_at, BrokerOrder.updated_at)
    for row, timestamp in session.execute(select(BrokerOrder, order_time)
        .outerjoin(TradeIntent, BrokerOrder.intent_id == TradeIntent.id)
        .where(BrokerOrder.broker_account_id.in_(ids), or_(
            order_time >= start, BrokerOrder.submitted_at >= start, BrokerOrder.updated_at >= start,
            order_time.is_(None), BrokerOrder.status.is_(None),
            func.lower(BrokerOrder.status).not_in(TERMINAL_ORDER_STATUSES),
        ))):
        data = activity[row.broker_account_id, row.symbol]
        timestamp = _utc(timestamp)
        if timestamp is None or any(value is not None and value > now for value in (
            timestamp, _utc(row.submitted_at), _utc(row.updated_at),
        )):
            data.complete = False
        elif any(value is not None and value >= start for value in (
            timestamp, _utc(row.submitted_at), _utc(row.updated_at),
        )):
            data.session_orders += 1
        if row.status.strip().lower() not in TERMINAL_ORDER_STATUSES:
            data.pending_orders += 1
            if row.side.strip().lower().startswith("sell") and not (
                data.virtual_quantity > 0 or data.managed_quantity > 0
            ):
                data.unowned_sells += 1

    for row in session.scalars(select(Fill).where(Fill.broker_account_id.in_(ids), or_(
        Fill.filled_at >= fill_balance_since, Fill.filled_at >= start, Fill.filled_at.is_(None),
    ))):
        data = activity[row.broker_account_id, row.symbol]
        timestamp = _utc(row.filled_at)
        side = row.side.strip().lower()
        if timestamp is None or timestamp > now or side not in {"buy", "sell"}:
            data.complete = False
            continue
        if timestamp >= start:
            data.session_fills += 1
        if timestamp >= fill_balance_since:
            balance = row.quantity if side == "buy" else -row.quantity
            data.net_fill_balance += balance
            data.strategy_balances[row.strategy_id] = data.strategy_balances.get(row.strategy_id, Decimal("0")) + balance
    for row in session.scalars(select(TradeIntent).where(
        TradeIntent.broker_account_id.in_(ids),
        or_(TradeIntent.status.is_(None), func.lower(TradeIntent.status).not_in(TERMINAL_INTENT_STATUSES)),
    )):
        data = activity[row.broker_account_id, row.symbol]
        data.pending_intents += 1
        if row.side.strip().lower().startswith("sell") and not (
            data.virtual_quantity > 0 or data.managed_quantity > 0
        ):
            data.unowned_sells += 1
    return dict(activity)


@dataclass(frozen=True)
class DirectAccountPositions:
    """Install2 producer must validate the complete raw GET, not an OMS cache/list hint."""
    account_id: UUID
    account_name: str
    read_at: datetime
    complete: bool
    positions: tuple[tuple[str, Decimal], ...]


@dataclass(frozen=True)
class OperatorHoldingsProof:
    accounts: tuple[tuple[UUID, str], ...]
    direct_positions: tuple[DirectAccountPositions, ...]
    activity: dict[tuple[UUID, str], BotActivity]
    database_read_at: datetime
    database_complete: bool

    def failures(self, now: datetime) -> list[str]:
        failures = []
        if (type(self.accounts) is not tuple or type(self.direct_positions) is not tuple
                or type(self.activity) is not dict
                or any(type(p) is not DirectAccountPositions for p in self.direct_positions)
                or any(type(item) is not tuple or len(item) != 2 or type(item[0]) is not UUID
                       or type(item[1]) is not str for item in self.accounts)
                or any(type(key) is not tuple or len(key) != 2 for key in self.activity)):
            return ["operator proof structure is unknown or malformed"]
        expected = dict(self.accounts)
        names = [name for _, name in self.accounts]
        direct = {p.account_id: p for p in self.direct_positions}
        if (len(expected) != len(self.accounts) or len(set(names)) != len(names)
                or set(names) != {"live:schwab_1m_v2", "live:orb"}
                or set(direct) != set(expected) or len(direct) != len(self.direct_positions)):
            return ["operator proof has incomplete or duplicate account inventory"]
        db_fresh = fresh_at(self.database_read_at, now)
        if self.database_complete is not True or not db_fresh:
            failures.append("operator proof database census is unknown or stale")
        for (account_id, symbol), data in self.activity.items():
            if account_id not in expected or type(data) is not BotActivity or data.blocks_flat():
                failures.append(f"operator proof bot ownership/activity is not flat: {account_id}/{symbol}")
        for account_id, source in direct.items():
            if (type(source) is not DirectAccountPositions or source.account_name != expected[account_id]
                    or source.complete is not True or not fresh_at(source.read_at, now)
                    or not db_fresh or source.read_at > self.database_read_at):
                failures.append(f"operator proof direct broker read is unknown/stale: {expected[account_id]}")
                continue
            symbols = set()
            if (type(source.positions) is not tuple
                    or any(type(item) is not tuple or len(item) != 2 for item in source.positions)):
                failures.append("operator proof broker rows are unknown or malformed")
                continue
            for symbol, quantity in source.positions:
                if (type(symbol) is not str or not symbol or symbol != symbol.strip().upper() or symbol in symbols
                        or type(quantity) is not Decimal or not quantity.is_finite()):
                    failures.append("operator proof broker position shape is invalid/duplicate")
                    continue
                symbols.add(symbol)
                if quantity == 0:
                    continue
                data = self.activity.get((account_id, symbol))
                if data is None or data.basis(account_name=source.account_name, symbol=symbol,
                    quantity=quantity, now=now, source_fresh=True) is None:
                    failures.append(f"operator proof holding has no permitted basis: {source.account_name}/{symbol}")
        return failures

    def position_count(self) -> int:
        return sum(quantity != 0 for source in self.direct_positions for _, quantity in source.positions)


def build_operator_holdings_proof(session: Session, accounts: dict[UUID, str], *,
    direct_positions: tuple[DirectAccountPositions, ...], now: datetime,
    fill_balance_since: datetime) -> OperatorHoldingsProof:
    """Call AFTER the complete direct GETs in the reviewed read-only transaction.

    Exceptions propagate. There is no empty-proof recovery or bool-only waiver.
    The caller owns READ ONLY/REPEATABLE READ, actor fencing and the final recheck.
    """
    inventory = {row.id: row.name for row in session.scalars(select(BrokerAccount).where(
        BrokerAccount.is_active.is_(True), BrokerAccount.name.startswith("live:"),
    ))}
    if inventory != accounts:
        raise ValueError("operator proof inventory differs from active live DB accounts")
    activity = collect_bot_activity(session, accounts, now=now, fill_balance_since=fill_balance_since)
    for source in direct_positions:
        for symbol, _ in source.positions:
            activity.setdefault((source.account_id, symbol), BotActivity())
    return OperatorHoldingsProof(tuple(accounts.items()), direct_positions, activity, now, True)
