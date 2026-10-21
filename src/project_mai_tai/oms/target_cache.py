"""Committed episode identity for target decisions; never authority to sell."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from threading import RLock

from sqlalchemy import event, select

from project_mai_tai.db.models import OmsManagedPosition
from project_mai_tai.oms.price_binding import stored_price


_READ_TICKETS = "mai_tai_bound_target_read_tickets"


@event.listens_for(OmsManagedPosition, "load")
def _publish_loaded_target(row, context):
    # Load context retains the individual SELECT's dispatch fence, even for lazy results.
    if context is None:
        return  # merge(load=False) supplies no committed-read evidence.
    for cache, generation in context.execution_options.get(_READ_TICKETS, ()):
        cache._loaded(context.session, row, generation)


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
        self._generation = 0  # Constant-size fence, not an unbounded row-ID tombstone map.
        self._pending_key = ("cw_target_created", id(self))
        self._invalidated_key = ("cw_target_invalidated", id(self))
        self._savepoints_key = ("cw_target_savepoints", id(self))
        self._commit_key = ("cw_target_commit_fence", id(self))
        event.listen(factory, "before_flush", self._before_flush)
        event.listen(factory, "after_flush", self._after_flush)
        event.listen(factory, "before_commit", self._before_commit)
        event.listen(factory, "after_commit", self._after_commit)
        event.listen(factory, "after_rollback", self._after_rollback)
        event.listen(factory, "after_transaction_create", self._transaction_created)
        event.listen(factory, "after_transaction_end", self._transaction_ended)
        event.listen(factory, "do_orm_execute", self._executing)

    def get(self, key):
        with self._lock:
            return self._rows.get(key)

    def drop_row(self, row_id):
        with self._lock:
            self._generation += 1
            for key, row in list(self._rows.items()):
                if row.managed_row_id == str(row_id):
                    self._rows.pop(key, None)

    def _value(self, row):
        if (row.strategy_code != "schwab_1m_v2" or row.broker_account_name not in self._accounts
                or row.status != "open" or not row.entry_order_id
                or row.current_quantity <= 0):
            return None
        try:
            price = stored_price(row.entry_price)
        except (InvalidOperation, ValueError):
            return None  # Invalid cache evidence must not abort a fill transaction.
        return BoundTargetRow(str(row.id), str(row.entry_order_id), price, int(row.current_quantity))

    def _publish(self, key, value, generation):
        with self._lock:
            if generation != self._generation:
                return  # An invalidation after read dispatch wins over this loaded image.
            # Object identity fences writes even when a later load has equal values.
            if self._rows.get(key) != value:
                self._rows[key] = value

    def _invalidate(self, session, row_id):
        session.info.setdefault(self._invalidated_key, set()).add(str(row_id))
        self.drop_row(row_id)

    def _finish_invalidations(self, session):
        for row_id in session.info.pop(self._invalidated_key, ()):
            self.drop_row(row_id)

    def _transaction_created(self, session, transaction):
        if transaction.nested:
            session.info.setdefault(self._savepoints_key, {})[transaction] = (
                dict(session.info.get(self._pending_key, {})),
                set(session.info.get(self._invalidated_key, ())),
            )

    def _transaction_ended(self, session, transaction):
        if transaction.nested:
            session.info.get(self._savepoints_key, {}).pop(transaction, None)
        elif transaction.parent is None:
            # Session.close also ends a transaction without an after_rollback notification.
            self._finish_invalidations(session)
            for key in (self._pending_key, self._savepoints_key, self._commit_key):
                session.info.pop(key, None)

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

    def _before_commit(self, session):
        if not session.in_nested_transaction():
            session.flush()
            with self._lock:
                session.info[self._commit_key] = self._generation

    def _after_commit(self, session):
        if session.in_nested_transaction():
            return  # Releasing a SAVEPOINT is not a committed database image.
        with self._lock:
            unchanged = session.info.pop(self._commit_key, None) == self._generation
            self._finish_invalidations(session)
            for key, value in session.info.pop(self._pending_key, {}).values():
                if value is not None and unchanged:
                    self._publish(key, value, self._generation)

    def _after_rollback(self, session):
        nested = session.get_nested_transaction()
        if nested is not None:
            prior = session.info.get(self._savepoints_key, {}).get(nested)
            if prior is not None:
                for row_id in set(session.info.get(self._invalidated_key, ())).union(
                    session.info.get(self._pending_key, {})
                ):
                    self.drop_row(row_id)
                session.info[self._pending_key], session.info[self._invalidated_key] = prior
            return
        self._finish_invalidations(session)
        session.info.pop(self._pending_key, None)

    def _loaded(self, session, row, generation):
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
                self._publish((row.broker_account_name, row.symbol), value, generation)

    def _executing(self, state):
        if state.is_select:
            with self._lock:
                ticket = (self, self._generation)
            tickets = state.local_execution_options.get(_READ_TICKETS, ())
            state.update_execution_options(**{_READ_TICKETS: (*tickets, ticket)})
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
