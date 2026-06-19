"""Tests for the flag-gated batched persist-offload path in the strategy engine.

The persist methods (`_persist_bar_history` / `_persist_revised_closed_bar`)
write `strategy_bar_history` synchronously by default. When
``strategy_persist_offload_enabled`` is True they instead capture a fully
resolved payload and buffer it; ``flush_pending_persists()`` runs the buffered
SELECT+upsert+commit off the event loop (one session, in order, per-item
isolated). These tests prove flag-OFF == inline, flag-ON == deferred-then-flush,
in-order read-after-write, mid-batch failure isolation, and no regression.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import StrategyBarHistory
from project_mai_tai.services.strategy_engine_app import (
    StrategyBotRuntime,
    StrategyDefinition,
)
from project_mai_tai.strategy_core import IndicatorConfig, OHLCVBar, TradingConfig


def build_test_session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_runtime(
    *,
    session_factory: sessionmaker[Session],
    persist_offload_enabled: bool,
) -> StrategyBotRuntime:
    return StrategyBotRuntime(
        StrategyDefinition(
            code="macd_30s",
            display_name="30s",
            account_name="paper:test",
            interval_secs=30,
            trading_config=TradingConfig(),
            indicator_config=IndicatorConfig(),
        ),
        now_provider=lambda: datetime(2026, 4, 18, 10, 0, tzinfo=UTC),
        session_factory=session_factory,
        persist_offload_enabled=persist_offload_enabled,
    )


def ensure_builder(runtime: StrategyBotRuntime, symbol: str) -> None:
    """_persist_bar_history early-returns if the symbol has no builder. Tests
    that drive the persist method directly (with an explicit completed_bar)
    create the builder up front so the persist path is exercised."""
    runtime.builder_manager.get_or_create(symbol)


def make_bar(*, ts: int, close: float = 2.5, volume: int = 1000, trade_count: int = 7) -> OHLCVBar:
    return OHLCVBar(
        open=2.0,
        high=3.0,
        low=1.5,
        close=close,
        volume=volume,
        timestamp=ts,
        trade_count=trade_count,
    )


def count_rows(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return len(list(session.scalars(select(StrategyBarHistory))))


def fetch_row(session_factory: sessionmaker[Session], symbol: str, bar_time: datetime) -> StrategyBarHistory | None:
    with session_factory() as session:
        return session.scalar(
            select(StrategyBarHistory).where(
                StrategyBarHistory.symbol == symbol,
                StrategyBarHistory.bar_time == bar_time,
            )
        )


# ---------------------------------------------------------------------------
# 1. Flag OFF == inline write (buffer stays empty, row present immediately)
# ---------------------------------------------------------------------------
def test_flag_off_persist_bar_history_writes_inline() -> None:
    session_factory = build_test_session_factory()
    runtime = make_runtime(session_factory=session_factory, persist_offload_enabled=False)
    ensure_builder(runtime, "ELAB")

    ts = 1_700_000_400
    runtime._persist_bar_history(
        symbol="ELAB",
        indicators={"price": 2.5},
        decision={"status": "idle", "reason": "no entry path matched"},
        completed_bar=make_bar(ts=ts),
    )

    assert runtime._pending_persist_writes == []
    row = fetch_row(session_factory, "ELAB", datetime.fromtimestamp(ts, UTC))
    assert row is not None
    assert row.decision_status == "idle"
    assert int(row.volume) == 1000


def test_flag_off_revised_bar_writes_inline() -> None:
    session_factory = build_test_session_factory()
    runtime = make_runtime(session_factory=session_factory, persist_offload_enabled=False)

    ts = 1_700_000_400
    runtime._persist_revised_closed_bar(symbol="ELAB", bar=make_bar(ts=ts, volume=4242))

    assert runtime._pending_persist_writes == []
    row = fetch_row(session_factory, "ELAB", datetime.fromtimestamp(ts, UTC))
    assert row is not None
    assert int(row.volume) == 4242
    # Insert path stamps the late_revision sentinel.
    assert row.decision_status == "late_revision"


# ---------------------------------------------------------------------------
# 2. Flag ON == deferred; flush commits everything
# ---------------------------------------------------------------------------
def test_flag_on_defers_then_flush_commits() -> None:
    session_factory = build_test_session_factory()
    runtime = make_runtime(session_factory=session_factory, persist_offload_enabled=True)
    ensure_builder(runtime, "ELAB")

    ts = 1_700_000_400
    runtime._persist_bar_history(
        symbol="ELAB",
        indicators={"price": 2.5},
        decision={"status": "idle", "reason": "no entry path matched"},
        completed_bar=make_bar(ts=ts),
    )

    # Nothing written yet; buffered instead.
    assert count_rows(session_factory) == 0
    assert len(runtime._pending_persist_writes) == 1
    assert runtime._pending_persist_writes[0][0] == "bar"

    runtime.flush_pending_persists()

    assert runtime._pending_persist_writes == []
    row = fetch_row(session_factory, "ELAB", datetime.fromtimestamp(ts, UTC))
    assert row is not None
    assert int(row.volume) == 1000
    assert row.decision_status == "idle"


def test_flush_noop_when_buffer_empty() -> None:
    session_factory = build_test_session_factory()
    runtime = make_runtime(session_factory=session_factory, persist_offload_enabled=True)
    # Must not raise / must not open a session needlessly.
    runtime.flush_pending_persists()
    assert count_rows(session_factory) == 0


# ---------------------------------------------------------------------------
# 3. Ordering / read-after-write: base insert then revise of same (sym, t)
# ---------------------------------------------------------------------------
def test_flush_applies_revise_on_top_of_base_in_order() -> None:
    session_factory = build_test_session_factory()
    runtime = make_runtime(session_factory=session_factory, persist_offload_enabled=True)
    ensure_builder(runtime, "ELAB")

    ts = 1_700_000_400
    bar_time = datetime.fromtimestamp(ts, UTC)

    # Base bar-history insert with a real entry decision recorded.
    runtime._persist_bar_history(
        symbol="ELAB",
        indicators={"price": 2.5},
        decision={"status": "entry", "reason": "P1 cross", "path": "P1", "score": "9"},
        completed_bar=make_bar(ts=ts, close=2.5, volume=1000, trade_count=7),
    )
    # Then a late-trade revision for the SAME bucket with corrected OHLCV.
    runtime._persist_revised_closed_bar(
        symbol="ELAB",
        bar=make_bar(ts=ts, close=2.9, volume=5000, trade_count=42),
    )

    assert count_rows(session_factory) == 0
    assert [kind for kind, _ in runtime._pending_persist_writes] == ["bar", "revise"]

    runtime.flush_pending_persists()

    assert runtime._pending_persist_writes == []
    assert count_rows(session_factory) == 1  # one row, not two
    row = fetch_row(session_factory, "ELAB", bar_time)
    assert row is not None
    # Revise applied on top of the base: OHLCV/volume/trade_count updated...
    assert int(row.volume) == 5000
    assert int(row.trade_count) == 42
    assert float(row.close_price) == 2.9
    # ...but the original entry decision is preserved (NOT overwritten by the
    # revise, and NOT stamped late_revision because the row already existed).
    assert row.decision_status == "entry"
    assert row.decision_path == "P1"


# ---------------------------------------------------------------------------
# 4. Mid-batch failure isolation: one bad write doesn't lose the others
# ---------------------------------------------------------------------------
def test_flush_isolates_mid_batch_failure(monkeypatch) -> None:
    session_factory = build_test_session_factory()
    runtime = make_runtime(session_factory=session_factory, persist_offload_enabled=True)
    ensure_builder(runtime, "ELAB")

    ts1, ts2, ts3 = 1_700_000_400, 1_700_000_430, 1_700_000_460
    for ts in (ts1, ts2, ts3):
        runtime._persist_bar_history(
            symbol="ELAB",
            indicators={"price": 2.5},
            decision={"status": "idle", "reason": "no entry path matched"},
            completed_bar=make_bar(ts=ts),
        )
    assert len(runtime._pending_persist_writes) == 3

    real_write = runtime._write_bar_history_record
    call_state = {"n": 0}

    def flaky_write(payload, session):
        call_state["n"] += 1
        if call_state["n"] == 2:
            raise RuntimeError("boom on the 2nd buffered write")
        return real_write(payload, session)

    monkeypatch.setattr(runtime, "_write_bar_history_record", flaky_write)

    # Must not raise — per-item try/except swallows the failure.
    runtime.flush_pending_persists()

    assert runtime._pending_persist_writes == []
    # Items 1 and 3 committed; item 2 was rolled back / dropped.
    assert count_rows(session_factory) == 2
    assert fetch_row(session_factory, "ELAB", datetime.fromtimestamp(ts1, UTC)) is not None
    assert fetch_row(session_factory, "ELAB", datetime.fromtimestamp(ts2, UTC)) is None
    assert fetch_row(session_factory, "ELAB", datetime.fromtimestamp(ts3, UTC)) is not None


# ---------------------------------------------------------------------------
# 5. No regression: flag OFF end-to-end via handle_trade_tick revise path
# ---------------------------------------------------------------------------
def test_no_regression_flag_off_handle_trade_tick_persists_inline() -> None:
    """An end-to-end handler test with the flag OFF must behave identically to
    today: the bar reaches the DB synchronously and the buffer stays empty."""
    session_factory = build_test_session_factory()
    runtime = make_runtime(session_factory=session_factory, persist_offload_enabled=False)
    runtime.set_watchlist(["ELAB"])

    # Two ticks in the same 30s bucket then one in the next bucket so the first
    # bucket closes and gets evaluated + persisted.
    base_ns = 1_700_000_400_000_000_000
    runtime.handle_trade_tick(symbol="ELAB", price=2.50, size=100, timestamp_ns=base_ns)
    runtime.handle_trade_tick(symbol="ELAB", price=2.55, size=100, timestamp_ns=base_ns + 5_000_000_000)
    runtime.handle_trade_tick(symbol="ELAB", price=2.60, size=100, timestamp_ns=base_ns + 35_000_000_000)

    # With the flag OFF, persistence is inline: buffer never fills.
    assert runtime._pending_persist_writes == []
    # At least one closed bar should have been persisted synchronously.
    assert count_rows(session_factory) >= 1


def test_flag_on_handle_trade_tick_buffers_until_flush() -> None:
    """Same handler flow with flag ON: rows are buffered (DB empty) until an
    explicit flush, after which they are present."""
    session_factory = build_test_session_factory()
    runtime = make_runtime(session_factory=session_factory, persist_offload_enabled=True)
    runtime.set_watchlist(["ELAB"])

    base_ns = 1_700_000_400_000_000_000
    runtime.handle_trade_tick(symbol="ELAB", price=2.50, size=100, timestamp_ns=base_ns)
    runtime.handle_trade_tick(symbol="ELAB", price=2.55, size=100, timestamp_ns=base_ns + 5_000_000_000)
    runtime.handle_trade_tick(symbol="ELAB", price=2.60, size=100, timestamp_ns=base_ns + 35_000_000_000)

    # Deferred: DB still empty, buffer non-empty.
    assert count_rows(session_factory) == 0
    buffered = len(runtime._pending_persist_writes)
    assert buffered >= 1

    runtime.flush_pending_persists()

    assert runtime._pending_persist_writes == []
    assert count_rows(session_factory) >= 1
