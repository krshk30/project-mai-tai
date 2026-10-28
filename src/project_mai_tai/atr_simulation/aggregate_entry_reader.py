"""Separate bounded read-only aggregate port; no default source or writer."""

from dataclasses import dataclass
from datetime import date, datetime
import re

import sqlalchemy as sa
from sqlalchemy.pool import QueuePool

from .aggregate_entry_contract import VERSION
from .aggregate_entry_publication import aggregate_writer_arguments
from .models import consumers, rows, runs
from .repository import digest
from .ui_reader import CONSUMER_LIMIT, ROW_LIMIT, SimulationUIReader, unavailable_storage


START = date(2026, 9, 22)
END = date(2026, 10, 23)
RUN_FIELDS = ("source_id", "input_sha256", "policy_version", "source_refs", "coverage", "result_sha256")


@dataclass(frozen=True)
class AggregateReadBinding:
    source_id: str
    run_sha256: str

    def __post_init__(self):
        if (re.fullmatch(r"aggregate-entry:[0-9a-f]{64}", self.source_id or "") is None
                or re.fullmatch(r"[0-9a-f]{64}", self.run_sha256 or "") is None):
            raise ValueError("explicit_aggregate_read_binding_required")


def publication_read_binding(prepared, *, producer):
    """Bind the reviewed immutable publication, never whatever is in storage."""
    args = aggregate_writer_arguments(prepared, producer=producer, expected_revision=0)
    proposed = dict(args["publish"])
    proposed["result_sha256"] = digest(proposed.pop("result_rows"))
    if proposed["policy_version"] != VERSION:
        raise ValueError("aggregate_read_policy_required")
    return AggregateReadBinding(proposed["source_id"], digest(proposed))


def bound_run(run, binding):
    if run is None or not hasattr(run, "keys"):
        return False
    try:
        return digest({key: run[key] for key in RUN_FIELDS}) == binding.run_sha256
    except (KeyError, TypeError, ValueError):
        return False


def validate_scope(source_id, start_day, end_day, limit, binding):
    if (source_id != binding.source_id
            or not isinstance(start_day, date) or isinstance(start_day, datetime)
            or not isinstance(end_day, date) or isinstance(end_day, datetime)
            or not START <= start_day <= end_day <= END
            or not 0 <= (end_day - start_day).days <= 31
            or type(limit) is not int or not 1 <= limit <= ROW_LIMIT):
        raise ValueError("bounded_bound_aggregate_seed_scope_required")


def _engine(database_url):
    return sa.create_engine(database_url, poolclass=QueuePool, pool_size=1,
        max_overflow=0, pool_timeout=1, pool_pre_ping=False,
        connect_args={"connect_timeout": 2}, hide_parameters=True)


class AggregateEntryUIReader(SimulationUIReader):
    """Lazy one-connection pool, SELECT-only, externally pinned run identity."""

    def __init__(self, database_url=None, *, binding, engine_factory=None):
        if not isinstance(binding, AggregateReadBinding):
            raise ValueError("explicit_aggregate_read_binding_required")
        super().__init__(database_url, engine_factory=_engine if engine_factory is None else engine_factory)
        self.binding = binding

    def read(self, source_id, start_day, end_day, *, limit=ROW_LIMIT):
        validate_scope(source_id, start_day, end_day, limit, self.binding)
        try:
            engine = self._get_engine()
            if engine is None:
                return unavailable_storage("AGGREGATE_DATABASE_UNCONFIGURED_OR_NOT_PG")
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    if connection.dialect.name != "postgresql":
                        return unavailable_storage("AGGREGATE_READER_REQUIRES_PG")
                    connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    connection.exec_driver_sql("SET LOCAL statement_timeout = '750ms'")
                    connection.exec_driver_sql("SET LOCAL lock_timeout = '250ms'")
                    run = connection.execute(sa.select(runs).where(
                        runs.c.source_id == source_id).limit(1)).mappings().one_or_none()
                    if not bound_run(run, self.binding):
                        return unavailable_storage("AGGREGATE_STORED_RUN_BINDING_UNKNOWN")
                    page = connection.execute(sa.select(rows.c.payload).where(
                        rows.c.source_id == source_id, rows.c.day >= start_day, rows.c.day <= end_day)
                        .order_by(rows.c.day, rows.c.row_key).limit(limit + 1)).scalars().all()
                    statuses = connection.execute(sa.select(consumers).order_by(
                        consumers.c.consumer_id).limit(CONSUMER_LIMIT + 1)).mappings().all()
                    return {"run": dict(run), "rows": page[:limit], "truncated": len(page) > limit,
                        "consumers": [dict(item) for item in statuses[:CONSUMER_LIMIT]],
                        "workers_truncated": len(statuses) > CONSUMER_LIMIT,
                        "read_status": "AVAILABLE", "read_issues": []}
                finally:
                    transaction.rollback()
        except (sa.exc.SQLAlchemyError, OSError, TimeoutError, ImportError, ValueError, TypeError, KeyError):
            return unavailable_storage("AGGREGATE_STORAGE_UNAVAILABLE")
