"""Reusable real-Postgres runner for windowed acceptance SQL.

The acceptance scripts use psql variables and ``COPY (...) TO STDOUT``.  This
runner deliberately invokes psql with the module's own ``SQL`` constant rather
than translating or copying the query into a test-only dialect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import subprocess
from types import ModuleType
from typing import Any

from sqlalchemy import Engine, create_engine, insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.sql.schema import Table


class PostgresHarnessError(RuntimeError):
    """The real database query could not be executed or interpreted."""


@dataclass(frozen=True)
class SeedRow:
    table: Table
    values: dict[str, Any]


@dataclass(frozen=True)
class SqlWindow:
    since: datetime
    until: datetime


@dataclass(frozen=True)
class AcceptanceSqlCase:
    module: ModuleType
    seed_rows: tuple[SeedRow, ...]
    window: SqlWindow


class PostgresAcceptanceHarness:
    """Reset, seed, and execute a module's emitted SQL against PostgreSQL."""

    _RESET_TABLES = (
        "broker_order_events",
        "fills",
        "broker_orders",
        "trade_intents",
        "strategies",
        "broker_accounts",
    )

    def __init__(self, database_url: str, *, psql_executable: str = "psql") -> None:
        self.database_url = database_url
        self.psql_executable = psql_executable
        self.engine: Engine = create_engine(database_url, future=True)
        url = make_url(database_url).set(drivername="postgresql")
        self.psql_dsn = url.render_as_string(hide_password=False)

    def assert_available(self) -> None:
        """Fail the integration run when its required service is unavailable."""

        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover - exact driver failure is environment-specific
            raise PostgresHarnessError(f"required PostgreSQL service is unavailable: {exc}") from exc

    def reset_and_seed(self, seed_rows: tuple[SeedRow, ...]) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("TRUNCATE TABLE " + ", ".join(self._RESET_TABLES) + " CASCADE")
            )
            for row in seed_rows:
                connection.execute(insert(row.table).values(**row.values))

    def execute(self, case: AcceptanceSqlCase, *, sql: str | None = None) -> str:
        self.reset_and_seed(case.seed_rows)
        emitted_sql = case.module.SQL if sql is None else sql
        if not isinstance(emitted_sql, str) or not emitted_sql.strip():
            raise PostgresHarnessError(f"{case.module.__name__}.SQL is empty or unreadable")
        command = [
            self.psql_executable,
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            f"window_since={case.window.since.isoformat()}",
            "-v",
            f"window_until={case.window.until.isoformat()}",
            "-d",
            self.psql_dsn,
            "-f",
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                input=emitted_sql,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PostgresHarnessError(f"could not execute psql: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or "psql exited non-zero without an error"
            raise PostgresHarnessError(detail)
        if result.stderr.strip():
            raise PostgresHarnessError(f"psql wrote unexpected stderr: {result.stderr.strip()}")
        return result.stdout


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]
