"""Real PostgreSQL coverage for the isolated live decision tape."""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.base import Base
from project_mai_tai.orb_live_decisions import LiveDecisionTape, read_decisions


def test_live_tape_postgres_bounded_roundtrip():
    dsn = os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not dsn.startswith("postgresql"):
        pytest.fail("MAI_TAI_DATABASE_URL must name the required PostgreSQL CI service")
    engine = create_engine(dsn)
    schema = f"orb_tape_{uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = engine.execution_options(schema_translate_map={None: schema})
    try:
        Base.metadata.create_all(isolated)
        factory = sessionmaker(isolated, expire_on_commit=False)
        tape = LiveDecisionTape(factory)
        at = datetime(2026, 10, 9, 13, 30, tzinfo=UTC)
        tape._persist([{"strategy_code": "orb_schwab", "symbol": "VIVK",
                        "reason": "bar_missing", "evaluated_at": (at + timedelta(seconds=i)).isoformat()}
                       for i in range(60)])
        with factory() as session:
            rows = read_decisions(session, at.replace(hour=0), at.replace(hour=0) + timedelta(days=1))
            assert len(rows) == 50
            assert rows[0]["evaluated_at"] == "2026-10-09T13:30:59+00:00"
            assert rows[-1]["evaluated_at"] == "2026-10-09T13:30:10+00:00"
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()
