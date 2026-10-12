"""Committed episode identity for target decisions; never authority to sell."""
from dataclasses import dataclass
from decimal import Decimal
from threading import RLock

from sqlalchemy import event, select

from project_mai_tai.db.models import OmsManagedPosition


@dataclass(frozen=True)
class BoundTargetRow:
    managed_row_id: str
    entry_order_id: str
    entry_price: Decimal
    quantity: int


class ManagedTargetCache:
    def __init__(self, factory, accounts):
        self._accounts = frozenset(accounts)
        self._rows = {}
        self._lock = RLock()
        self._pending_key = ("cw_target_created", id(self))
        self._invalidated_key = ("cw_target_invalidated", id(self))
        event.listen(factory, "before_flush", self._before_flush)
        event.listen(factory, "after_flush", self._after_flush)
        event.listen(factory, "after_commit", self._after_commit)
        event.listen(factory, "after_rollback", self._after_rollback)
        event.listen(factory, "loaded_as_persistent", self._loaded)
        event.listen(factory, "do_orm_execute", self._executing)

    def get(self, key):
        with self._lock:
            return self._rows.get(key)

    def drop_row(self, row_id):
        with self._lock:
            for key, row in list(self._rows.items()):
                if row.managed_row_id == str(row_id):
                    self._rows.pop(key, None)

    def _value(self, row):
        if (row.strategy_code != "schwab_1m_v2" or row.broker_account_name not in self._accounts
                or row.status != "open" or not row.entry_order_id
                or row.current_quantity <= 0 or row.entry_price <= 0):
            return None
        return BoundTargetRow(str(row.id), str(row.entry_order_id),
            Decimal(str(row.entry_price)), int(row.current_quantity))

    def _publish(self, key, value):
        with self._lock:
            # Object identity fences writes even when a later load has equal values.
            if self._rows.get(key) != value:
                self._rows[key] = value

    def _invalidate(self, session, row_id):
        session.info.setdefault(self._invalidated_key, set()).add(str(row_id))
        self.drop_row(row_id)

    def _finish_invalidations(self, session):
        for row_id in session.info.pop(self._invalidated_key, ()):
            self.drop_row(row_id)

    def _before_flush(self, session, *_args):
        for row in session.dirty.union(session.deleted):
            if isinstance(row, OmsManagedPosition) and (
                row in session.deleted or session.is_modified(row, include_collections=False)
            ):
                self._invalidate(session, row.id)

    def _after_flush(self, session, *_args):
        pending = session.info.setdefault(self._pending_key, {})
        for row in session.new.union(session.dirty).union(session.deleted):
            if not isinstance(row, OmsManagedPosition):
                continue
            if row in session.new or str(row.id) in pending:
                value = None if row in session.deleted else self._value(row)
                pending[str(row.id)] = ((row.broker_account_name, row.symbol), value)

    def _after_commit(self, session):
        self._finish_invalidations(session)
        for key, value in session.info.pop(self._pending_key, {}).values():
            if value is not None:
                self._publish(key, value)

    def _after_rollback(self, session):
        self._finish_invalidations(session)
        session.info.pop(self._pending_key, None)

    def _loaded(self, session, row):
        if isinstance(row, OmsManagedPosition):
            # A read of this transaction's own writes is not committed evidence.
            if (str(row.id) in session.info.get(self._invalidated_key, ())
                    or str(row.id) in session.info.get(self._pending_key, {})):
                self.drop_row(row.id)
                return
            value = self._value(row)
            if value is None:
                self.drop_row(row.id)
            else:
                self._publish((row.broker_account_name, row.symbol), value)

    def _executing(self, state):
        if (state.is_update or state.is_delete) and state.bind_mapper is not None and (
            state.bind_mapper.class_ is OmsManagedPosition
        ):
            # Existing false-flip writes use ORM bulk updates, outside flush events.
            # Read affected IDs on that writer's transaction, never on quote intake.
            statement = select(OmsManagedPosition.id)
            if state.statement.whereclause is not None:
                statement = statement.where(state.statement.whereclause)
            for row_id in state.session.scalars(statement, state.parameters or {}):
                self._invalidate(state.session, row_id)
