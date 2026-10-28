"""Private SQLite controls for actual durable census; not native/PG qualification."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, Strategy, TradeIntent,
)

ACCOUNTS = ("live:schwab", "live:webull")


@pytest.fixture
def census():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        accounts = [BrokerAccount(name=name, provider=provider, environment="live")
                    for name, provider in zip(ACCOUNTS, ("schwab", "webull"), strict=True)]
        strategy = Strategy(code="schwab_1m_v2", name="v2")
        session.add_all([*accounts, strategy])
        session.flush()
        yield session, accounts, strategy
    engine.dispose()


def attempt(session):
    row = DashboardSnapshot(snapshot_type="v2_wait_dispatch", payload={
        "schema_version": 1, "kind": "attempt", "strategy_code": "schwab_1m_v2",
        "symbol": "WORX", "account_name": ACCOUNTS[0], "account_names": list(ACCOUNTS),
        "opportunity_id": "100", "attempt_token": "persisted-attempt"})
    session.add(row)
    session.flush()
    return row


def order(session, accounts, strategy, *, status="cancelled", source="broker", line="900"):
    now = datetime.now(UTC)
    md = {"clearwait_dispatch_token": "persisted-attempt", "fanout_segment_id": "100",
        "brokerdecides_reservation_token": "persisted-attempt"}
    if line is not None:
        md["brokerdecides_line_segment_ms"] = line
    parent = TradeIntent(strategy_id=strategy.id, broker_account_id=accounts[0].id,
        symbol="WORX", side="buy", intent_type="open", quantity=Decimal("1"), reason="ATR Flip",
        status=status, payload={"metadata": md}, created_at=now - timedelta(seconds=60),
        updated_at=now - timedelta(seconds=30))
    session.add(parent)
    session.flush()
    row = BrokerOrder(intent_id=parent.id, strategy_id=strategy.id, broker_account_id=accounts[0].id,
        client_order_id="fixture-client", broker_order_id="fixture-native", symbol="WORX", side="buy",
        quantity=Decimal("1"), order_type="stop_limit", time_in_force="day", status=status, payload=md,
        submitted_at=now - timedelta(seconds=60), updated_at=now - timedelta(seconds=30))
    session.add(row)
    session.flush()
    if source is not None:
        session.add(BrokerOrderEvent(order_id=row.id, event_type=status, event_source=source,
            event_at=now - timedelta(seconds=30), payload={"client_order_id": row.client_order_id,
                "broker_order_id": row.broker_order_id}))
    session.flush()
    return row


def test_restart_attempt_without_order_stays_unknown(census):
    session, accounts, strategy = census
    original = attempt(session)
    inflight, fills, terminals = local_evidence(session, ACCOUNTS)
    assert inflight == frozenset({"WORX"}) and not fills and not terminals
    assert original.payload["kind"] == "attempt"  # Observation never retires the journal.


@pytest.mark.parametrize("source", [None, "unknown", "client"])
def test_terminal_label_without_native_closure_never_releases(census, source):
    session, accounts, strategy = census
    attempt(session)
    order(session, accounts, strategy, source=source)
    inflight, fills, terminals = local_evidence(session, ACCOUNTS)
    assert "WORX" in inflight and not terminals


def test_exact_native_zero_fill_closure_emits_exact_token(census):
    session, accounts, strategy = census
    attempt(session)
    order(session, accounts, strategy)
    inflight, fills, terminals = local_evidence(session, ACCOUNTS)
    assert not inflight and not fills
    assert terminals == frozenset({("WORX", "persisted-attempt")})


@pytest.mark.parametrize("mismatch", ["account", "opportunity", "symbol"])
def test_foreign_closure_cannot_retire_attempt(census, mismatch):
    session, accounts, strategy = census
    attempt(session)
    row = order(session, accounts, strategy)
    if mismatch == "account":
        row.broker_account_id = accounts[1].id
    elif mismatch == "opportunity":
        row.payload = {**row.payload, "fanout_segment_id": "foreign"}
    else:
        row.symbol = "OTHER"
    session.flush()
    inflight, fills, terminals = local_evidence(session, ACCOUNTS)
    assert "WORX" in inflight and not terminals


@pytest.mark.parametrize("line", [None, "900"])
def test_positive_buy_fill_never_inferred_from_slot_note(census, line):
    session, accounts, strategy = census
    row = order(session, accounts, strategy, status="filled", line=line)
    session.add(Fill(order_id=row.id, strategy_id=strategy.id, broker_account_id=accounts[0].id,
        broker_fill_id="fixture-fill", symbol="WORX", side="buy", quantity=Decimal("1"),
        price=Decimal("3"), filled_at=datetime.now(UTC)))
    session.flush()
    inflight, fills, terminals = local_evidence(session, ACCOUNTS)
    if line is None:
        assert "WORX" in inflight and not fills
    else:
        assert ("WORX", 900) in fills


def test_partial_cancel_without_closed_fill_attribution_stays_held(census):
    session, accounts, strategy = census
    attempt(session)
    row = order(session, accounts, strategy)
    session.add(Fill(order_id=row.id, strategy_id=strategy.id, broker_account_id=accounts[0].id,
        broker_fill_id="fixture-partial", symbol="WORX", side="buy", quantity=Decimal("0.5"),
        price=Decimal("3"), filled_at=datetime.now(UTC) - timedelta(seconds=40)))
    session.flush()
    inflight, fills, terminals = local_evidence(session, ACCOUNTS)
    assert "WORX" in inflight and not terminals


def test_other_strategy_pending_buy_is_inflight_not_native_flat_evidence(census):
    session, accounts, strategy = census
    other = Strategy(code="momentum_warn_live", name="momentum")
    session.add(other)
    session.flush()
    order(session, accounts, other, status="pending", source=None)
    inflight, fills, terminals = local_evidence(session, ACCOUNTS)
    assert "WORX" in inflight


@pytest.mark.asyncio
async def test_unknown_logs_once_per_rowset_and_summary_each_publish_preserves_refusal(census, caplog, monkeypatch):
    from project_mai_tai.brokerdecides1 import assess, decode
    from project_mai_tai.brokerdecides1_sync import LOCAL_CENSUS_DIAGNOSTICS, begin_pass, finish_pass
    from project_mai_tai.db.models import BrokerDecidesDispatchRetirement
    from test_brokerdecides1_sync import native, service

    session, accounts, strategy = census
    # Six actual durable attempts, three symbols. Invalid projection preimages
    # must remain UNKNOWN: their mere existence cannot prove broker closure.
    originals = []
    for index, symbol in enumerate(("WORX", "WORX", "PFAI", "PFAI", "AIXI", "AIXI")):
        row = attempt(session)
        row.payload = {**row.payload, "symbol": symbol, "attempt_token": f"controlled-{index}"}
        originals.append((row, dict(row.payload)))
        if symbol == "AIXI":
            session.add(BrokerDecidesDispatchRetirement(attempt_id=row.id,
                proof_version=-1, attempt_state="CONTROLLED_INVALID_PREIMAGE",
                token=f"controlled-{index}", account_name=ACCOUNTS[0], symbol=symbol,
                observed_ms=1, counts={}))
    session.flush()
    expected = (frozenset({"WORX", "PFAI", "AIXI"}), frozenset(), frozenset())
    fail_reader = False

    async def reader(account):
        if fail_reader:
            raise ValueError("CONTROLLED_NATIVE_UNKNOWN")
        return native(account)

    value = service(monkeypatch, reader)

    async def run_db(function, **kwargs):
        assert kwargs == {"commit": False}
        return function(session)  # Existing SQLite fixture, no provider/production DB.

    value._run_db = run_db
    for generation in range(1, 5):
        if generation == 3:
            row = attempt(session)
            row.payload = {**row.payload, "attempt_token": "controlled-added-worx"}
            originals.append((row, dict(row.payload)))
            session.flush()
        elif generation == 4:
            fail_reader = True
        caplog.clear()
        with caplog.at_level(logging.INFO):
            actual_generation = await begin_pass(value)
            await finish_pass(value, actual_generation, sync_succeeded=True)
        assert actual_generation == generation
        assert LOCAL_CENSUS_DIAGNOSTICS.get() is None
        assert len(value._brokerdecides1_unknown_log_rows) == 3
        snapshot = decode(value.redis.raw)
        messages = [record.getMessage() for record in caplog.records]
        details = [message for message in messages if message.startswith("[BROKERDECIDES1-UNKNOWN]")]
        summaries = [message for message in messages
            if message.startswith("[BROKERDECIDES1-UNKNOWN-SUMMARY]")]
        assert len(details) == {1: 3, 2: 0, 3: 1, 4: 0}[generation]
        if generation == 1:
            assert sum("changed_projection=False" in message for message in details) == 2
            assert sum("changed_projection=True" in message for message in details) == 1
        elif generation == 3:
            assert "symbol=WORX " in details[0]
        counts = "unknown_symbols=UNKNOWN rows=UNKNOWN" if generation == 4 else (
            f"unknown_symbols=3 rows={7 if generation == 3 else 6}")
        assert summaries == [f"[BROKERDECIDES1-UNKNOWN-SUMMARY] generation={generation} {counts}"]
        assert all(row.payload == payload for row, payload in originals)
        if generation == 4:
            assert not snapshot.local_complete
            continue
        assert snapshot.local_complete
        assert (snapshot.inflight, snapshot.filled_segments, snapshot.terminal_reservations) == expected
        for symbol in expected[0]:
            decision = assess(snapshot, accounts=ACCOUNTS, symbol=symbol,
                segment=900, now_ms=1000, max_age_ms=20_000)
            assert not decision.allowed and decision.reason == "own_inflight_UNKNOWN_or_working"
