"""Lazy SELECT-only UI port over SIM tables; never a repository writer."""

from datetime import date, datetime
import re
from threading import Lock
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.pool import QueuePool

from .models import consumers, rows, runs


SOURCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
ROW_LIMIT = 10000
CONSUMER_LIMIT = 100
POOL_SIZE = 2
POOL_TIMEOUT_SECONDS = 1
CONNECT_TIMEOUT_SECONDS = 2
STATEMENT_TIMEOUT_MS = 750
LOCK_TIMEOUT_MS = 250


class SimulationUIReadPort(Protocol):
    def read(self, source_id: str, start_day: date, end_day: date, *, limit: int = ROW_LIMIT) -> dict: ...


def validate_read_scope(source_id, start_day, end_day, limit):
    if (not isinstance(source_id, str) or re.fullmatch(SOURCE_ID_PATTERN, source_id) is None
            or not isinstance(start_day, date) or isinstance(start_day, datetime)
            or not isinstance(end_day, date) or isinstance(end_day, datetime)
            or not 0 <= (end_day - start_day).days <= 6
            or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= ROW_LIMIT):
        raise ValueError("bounded_simulation_source_day_scope_required")


def unavailable_storage(issue):
    return {"run": None, "rows": [], "consumers": [], "truncated": False,
        "read_status": "UNAVAILABLE", "read_issues": [issue], "workers_truncated": False}


def _new_engine(database_url):
    # Called only on a request, never during app/router/page factory construction.
    return sa.create_engine(database_url, poolclass=QueuePool, pool_size=POOL_SIZE,
        max_overflow=0, pool_timeout=POOL_TIMEOUT_SECONDS, pool_pre_ping=False,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS}, hide_parameters=True)


class SimulationUIReader:
    def __init__(self, database_url=None, *, engine_factory=None):
        self._database_url = database_url
        self._engine_factory = _new_engine if engine_factory is None else engine_factory
        self._engine = None
        self._lock = Lock()

    def _get_engine(self):
        with self._lock:
            if self._engine is None:
                if self._database_url is None:
                    return None
                url = sa.engine.make_url(self._database_url)
                if url.drivername not in {"postgresql", "postgresql+psycopg"}:
                    return None
                if url.drivername == "postgresql":
                    url = url.set(drivername="postgresql+psycopg")
                self._engine = self._engine_factory(url)
            return self._engine

    def close(self):
        """Dispose only this UI reader's pool; never shared live sessions."""
        with self._lock:
            engine, self._engine = self._engine, None
        if engine is not None:
            engine.dispose()

    def read(self, source_id, start_day, end_day, *, limit=ROW_LIMIT):
        validate_read_scope(source_id, start_day, end_day, limit)
        try:
            engine = self._get_engine()
            if engine is None:
                return unavailable_storage("SIMULATION_DATABASE_UNCONFIGURED_OR_NOT_PG")
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    if connection.dialect.name != "postgresql":
                        return unavailable_storage("SIMULATION_READER_REQUIRES_PG")
                    connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    connection.exec_driver_sql("SET LOCAL statement_timeout = '750ms'")
                    connection.exec_driver_sql("SET LOCAL lock_timeout = '250ms'")
                    run = connection.execute(sa.select(runs).where(
                        runs.c.source_id == source_id).limit(1)).mappings().one_or_none()
                    page = connection.execute(sa.select(rows.c.payload).where(
                        rows.c.source_id == source_id, rows.c.day >= start_day, rows.c.day <= end_day)
                        .order_by(rows.c.day, rows.c.row_key).limit(limit + 1)).scalars().all()
                    statuses = connection.execute(sa.select(consumers).order_by(
                        consumers.c.consumer_id).limit(CONSUMER_LIMIT + 1)).mappings().all()
                    status = "UNKNOWN_SOURCE" if run is None else "NO_ROWS_UNKNOWN" if not page else "AVAILABLE"
                    return {"run": dict(run) if run is not None else None,
                        "rows": page[:limit] if run is not None else [], "truncated": len(page) > limit,
                        "consumers": [dict(item) for item in statuses[:CONSUMER_LIMIT]],
                        "workers_truncated": len(statuses) > CONSUMER_LIMIT,
                        "read_status": status,
                        "read_issues": [] if status == "AVAILABLE" else [status]}
                finally:
                    transaction.rollback()
        except (sa.exc.SQLAlchemyError, OSError, TimeoutError, ImportError, ValueError, TypeError) as error:
            code = getattr(getattr(error, "orig", None), "sqlstate", None)
            issue = "SIMULATION_MIGRATION_UNAVAILABLE" if code == "42P01" else "SIMULATION_STORAGE_UNAVAILABLE"
            return unavailable_storage(issue)
