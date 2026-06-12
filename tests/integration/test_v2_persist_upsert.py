"""Integration test: schwab_1m_v2 bar persist is an ATOMIC upsert.

Reproduces the GLXG-class REST/streamer dup race at the persist seam and proves
the `ON CONFLICT DO UPDATE` fix survives it. Postgres-only (the upsert is
pg-specific); skipped when MAI_TAI_DATABASE_URL is unset or sqlite.

Why assert on logs, not exceptions: `_persist_bar` SWALLOWS its DB error (logs
"failed to persist bar history"). So the old non-atomic SELECT-then-INSERT, when
two writers pass the SELECT before either INSERTs, raises UniqueViolation that is
caught + logged + the write is LOST. The race symptom is therefore a logged
persist error, which this test asserts is absent.

Runs against the real DB with an isolated synthetic symbol, cleaned up.
"""
from __future__ import annotations

import logging
import os
import threading

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService

TEST_SYMBOL = "ZZUPSERTTEST"
LOGGER = "project_mai_tai.services.schwab_1m_v2_bot"


def _pg():
    dsn = os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not dsn or "sqlite" in dsn:
        pytest.skip("needs Postgres (MAI_TAI_DATABASE_URL); the upsert is pg-specific")
    engine = create_engine(dsn.replace("+psycopg", "+psycopg"), future=True)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


class _Stub:
    """Minimal carrier — _persist_bar only touches self.session_factory."""

    def __init__(self, sf):
        self.session_factory = sf


def _cleanup(engine) -> None:
    with engine.begin() as c:
        c.execute(text("DELETE FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' "
                       "AND symbol=:s"), {"s": TEST_SYMBOL})


def _bar(ts_ms: int, close: float) -> ChartBar:
    return ChartBar(TEST_SYMBOL, close, close + 0.1, close - 0.1, close, 1000, ts_ms)


def test_concurrent_dup_persist_loses_no_writes(caplog) -> None:
    sf, engine = _pg()
    stub = _Stub(sf)
    ts = 1700000000000  # fixed synthetic minute
    n_threads = 8       # wide enough that >=2 reliably SELECT before any INSERT commits
    _cleanup(engine)
    try:
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            for i in range(30):
                bar = _bar(ts, 5.0 + i * 0.01)
                _cleanup(engine)  # force every iteration to race the INSERT path (not UPDATE)
                barrier = threading.Barrier(n_threads)

                def worker():
                    barrier.wait()  # all threads hit the DB together
                    SchwabV2BotService._persist_bar(stub, TEST_SYMBOL, bar)

                threads = [threading.Thread(target=worker) for _ in range(n_threads)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

        persist_errors = [r for r in caplog.records if "failed to persist bar history" in r.getMessage()]
        assert persist_errors == [], (
            f"persist dup race NOT fixed — {len(persist_errors)} swallowed UniqueViolation(s). "
            "This is the assertion that fails on the old SELECT-then-INSERT code."
        )
        with sf() as s:
            n = s.execute(text("SELECT count(*) FROM strategy_bar_history WHERE "
                               "strategy_code='schwab_1m_v2' AND symbol=:s AND interval_secs=60"),
                          {"s": TEST_SYMBOL}).scalar()
        assert n == 1  # exactly one row despite 50 concurrent writes
    finally:
        _cleanup(engine)


def test_upsert_refreshes_ohlcv_and_preserves_decision_fields() -> None:
    sf, engine = _pg()
    stub = _Stub(sf)
    ts = 1700000060000
    _cleanup(engine)
    try:
        SchwabV2BotService._persist_bar(stub, TEST_SYMBOL, _bar(ts, 5.0))
        # Simulate a later decision write landing on the same bar row.
        with engine.begin() as c:
            c.execute(text("UPDATE strategy_bar_history SET decision_status='signal' WHERE "
                           "strategy_code='schwab_1m_v2' AND symbol=:s AND bar_time=to_timestamp(:t)"),
                      {"s": TEST_SYMBOL, "t": ts / 1000})
        # Re-persist the SAME key with new OHLCV -> ON CONFLICT updates OHLCV only.
        SchwabV2BotService._persist_bar(stub, TEST_SYMBOL, _bar(ts, 6.0))
        with sf() as s:
            close, decision = s.execute(
                text("SELECT close_price, decision_status FROM strategy_bar_history WHERE "
                     "strategy_code='schwab_1m_v2' AND symbol=:s AND bar_time=to_timestamp(:t)"),
                {"s": TEST_SYMBOL, "t": ts / 1000}).one()
        assert float(close) == 6.0       # OHLCV refreshed on conflict
        assert decision == "signal"       # decision_* preserved (set_ touches OHLCV only)
    finally:
        _cleanup(engine)
