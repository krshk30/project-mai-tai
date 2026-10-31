"""Private P6 source controls, not PostgreSQL/native/history qualification.

Main alone may execute. The large test deliberately retains original economic
history and uses actual durable native-cancel proof, never mocked flat/closure.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, insert, select

from project_mai_tai.brokerdecides1 import AdmissionMemory, AdmissionSnapshot, NativeAccountBook, encode
from project_mai_tai.brokerdecides1_retirement import (
    BATCH, load_terminal_tokens, retire_batch, unresolved_query, valid_retirement,
)
from project_mai_tai.brokerdecides1_sync import local_evidence, refresh_consumer
from project_mai_tai.db.models import (
    BrokerDecidesDispatchEvidence as Evidence, BrokerDecidesDispatchRetirement as Retirement,
    BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, OmsManagedPosition, TradeIntent,
)
from test_brokerdecides1_local_census import ACCOUNTS, attempt, census as census, order


def observed():
    return int(datetime.now(UTC).timestamp() * 1000)


def seal_one(census):
    session, accounts, strategy = census
    journal = attempt(session)
    child = order(session, accounts, strategy)
    cursor, retired, visited = retire_batch(session, ACCOUNTS, None, observed())
    assert retired == visited == 1 and cursor is not None
    session.commit()
    return journal, child


def is_valid(session, identity):
    return session.scalar(select(valid_retirement()).select_from(DashboardSnapshot)
        .where(DashboardSnapshot.id == identity))


def test_positive_receipt_keeps_original_rows_and_exact_token(census):
    session, accounts, strategy = census
    journal, child = seal_one(census)
    assert is_valid(session, journal.id) is True
    assert session.scalar(select(func.count()).select_from(BrokerOrder)) == 1
    assert session.get(BrokerOrder, child.id).status == "cancelled"
    assert session.get(DashboardSnapshot, journal.id).payload["kind"] == "attempt"
    assert list(session.scalars(unresolved_query(ACCOUNTS))) == []
    assert load_terminal_tokens(session, ACCOUNTS, {"persisted-attempt"}) == frozenset({
        ("WORX", "persisted-attempt")})
    assert not load_terminal_tokens(session, ACCOUNTS, {"foreign-token"})


@pytest.mark.parametrize("change", [
    "journal", "order_payload", "native_id", "order_quantity", "parent_payload",
    "event_source", "event_payload", "account_identity", "strategy_identity",
    "new_event", "late_fill", "missing_event", "new_token_order", "replacement_job",
])
def test_any_changed_or_new_preimage_reopens_attempt_without_refresh(census, change):
    session, accounts, strategy = census
    journal, child = seal_one(census)
    original = session.get(Retirement, journal.id).attempt_state
    event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == child.id))
    if change == "journal":
        journal.payload = {**journal.payload, "changed": True}
    elif change == "order_payload":
        child.payload = {**child.payload, "changed": True}
    elif change == "native_id":
        child.broker_order_id = "changed-native"
    elif change == "order_quantity":
        child.quantity = Decimal("2")
    elif change == "parent_payload":
        parent = session.get(TradeIntent, child.intent_id)
        parent.payload = {**parent.payload, "changed": True}
    elif change == "event_source":
        event.event_source = "unknown"
    elif change == "event_payload":
        event.payload = {**event.payload, "changed": True}
    elif change == "account_identity":
        accounts[0].external_account_id = "changed-native-account"
    elif change == "strategy_identity":
        strategy.code = "foreign_changed_strategy"
    elif change == "new_event":
        session.add(BrokerOrderEvent(order_id=child.id, event_type="pending_cancel",
            event_source="broker", payload={}, event_at=datetime.now(UTC)))
    elif change == "late_fill":
        session.add(Fill(order_id=child.id, strategy_id=strategy.id, broker_account_id=accounts[0].id,
            broker_fill_id="late", symbol="WORX", side="buy", quantity=Decimal("0.5"),
            price=Decimal("3"), filled_at=datetime.now(UTC)))
    elif change == "missing_event":
        session.execute(delete(BrokerOrderEvent).where(BrokerOrderEvent.id == event.id))
    elif change == "new_token_order":
        session.add(BrokerOrder(intent_id=child.intent_id, strategy_id=strategy.id,
            broker_account_id=accounts[0].id, client_order_id="new-unknown-child", symbol="WORX",
            side="buy", quantity=Decimal("1"), order_type="limit", time_in_force="day",
            status="pending", payload=dict(child.payload)))
    else:
        session.add(DashboardSnapshot(snapshot_type="atr_reprice_handoff", payload={
            "old": {"client_order_id": child.client_order_id, "symbol": "WORX"}, "phase": "unknown"}))
    session.flush()
    assert is_valid(session, journal.id) is False
    assert journal.id in {row.id for row in session.scalars(unresolved_query(ACCOUNTS))}
    assert not load_terminal_tokens(session, ACCOUNTS, {"persisted-attempt"})
    if change == "strategy_identity":
        with pytest.raises(ValueError, match="strategy_identity_UNKNOWN"):
            local_evidence(session, ACCOUNTS)
        with pytest.raises(ValueError, match="retirement_scope_UNKNOWN"):
            retire_batch(session, ACCOUNTS, None, observed())
        retired = 0
    else:
        assert "WORX" in local_evidence(session, ACCOUNTS)[0]
        _, retired, _ = retire_batch(session, ACCOUNTS, None, observed())
    assert retired == 0 and session.get(Retirement, journal.id).attempt_state == original


@pytest.mark.parametrize("source", [None, "unknown", "client"])
def test_terminal_label_without_native_proof_is_never_projected(census, source):
    session, accounts, strategy = census
    journal = attempt(session)
    order(session, accounts, strategy, source=source)
    _, retired, visited = retire_batch(session, ACCOUNTS, None, observed())
    assert retired == 0 and visited == 1
    assert session.get(Retirement, journal.id) is None
    assert "WORX" in local_evidence(session, ACCOUNTS)[0]


def test_no_order_crossday_attempt_is_not_retired_or_session_dropped(census):
    session, accounts, strategy = census
    journal = attempt(session)
    journal.created_at = datetime.now(UTC) - timedelta(days=100)
    session.flush()
    cursor, retired, visited = retire_batch(session, ACCOUNTS, None, observed())
    assert visited == 1 and retired == 0
    assert retire_batch(session, ACCOUNTS, cursor, observed()) == (None, 0, 0)
    assert "WORX" in local_evidence(session, ACCOUNTS)[0]


def test_missing_account_in_scope_is_unknown_not_skipped(census):
    session, accounts, strategy = census
    journal = attempt(session)
    data = dict(journal.payload)
    data.pop("account_name")
    journal.payload = data
    session.flush()
    with pytest.raises(ValueError, match="account_UNKNOWN"):
        local_evidence(session, ACCOUNTS)


def test_new_replacement_order_cannot_be_hidden_by_retirement(census):
    session, accounts, strategy = census
    journal = attempt(session)
    child = order(session, accounts, strategy)
    session.add(DashboardSnapshot(snapshot_type="atr_reprice_handoff", payload={
        "phase": "refused", "old": {"client_order_id": child.client_order_id, "symbol": "WORX"},
        "replacement": {"client_order_id": "unknown-successor"}}))
    session.flush()
    assert retire_batch(session, ACCOUNTS, None, observed())[1] == 0
    assert session.get(Retirement, journal.id) is None


@pytest.mark.parametrize("managed_closed", [False, True])
def test_retired_filled_order_still_has_real_same_segment_fill(census, managed_closed):
    session, accounts, strategy = census
    journal = attempt(session)
    child = order(session, accounts, strategy, status="filled")
    session.add(Fill(order_id=child.id, strategy_id=strategy.id, broker_account_id=accounts[0].id,
        broker_fill_id="complete", symbol="WORX", side="buy", quantity=Decimal("1"),
        price=Decimal("3"), filled_at=datetime.now(UTC) - timedelta(seconds=40)))
    managed = OmsManagedPosition(strategy_code=strategy.code, broker_account_name=accounts[0].name,
        symbol="WORX", entry_order_id=child.id, entry_client_order_id=child.client_order_id,
        entry_price=Decimal("3"), original_quantity=1, current_quantity=0 if managed_closed else 1,
        status="closed" if managed_closed else "open")
    session.add(managed)
    session.flush()
    assert retire_batch(session, ACCOUNTS, None, observed())[1] == int(managed_closed)
    session.commit()
    inflight, fills, terminals = local_evidence(session, ACCOUNTS)
    assert ("WORX", 900) in fills
    if managed_closed:
        assert is_valid(session, journal.id) is True
        managed.current_quantity, managed.status = 1, "open"
        session.flush()
        assert is_valid(session, journal.id) is False
        assert "WORX" in local_evidence(session, ACCOUNTS)[0]
    else:
        assert "WORX" in inflight and session.get(Retirement, journal.id) is None


def test_closed_empty_replacement_job_projection_preserves_original_job(census):
    from project_mai_tai.brokerdecides1_retirement import unresolved_jobs_query

    session, accounts, strategy = census
    journal = attempt(session)
    child = order(session, accounts, strategy)
    job = DashboardSnapshot(snapshot_type="atr_reprice_handoff", payload={"phase": "expired",
        "old": {"client_order_id": child.client_order_id, "symbol": "WORX", "side": "buy",
            "broker_account_name": accounts[0].name, "strategy_code": strategy.code, "quantity": "1"}})
    session.add(job)
    session.flush()
    assert retire_batch(session, ACCOUNTS, None, observed())[1] == 1
    session.commit()
    assert not list(session.scalars(unresolved_jobs_query(ACCOUNTS)))
    assert session.get(DashboardSnapshot, job.id).payload["phase"] == "expired"
    job.payload = {**job.payload, "replacement": {"client_order_id": "new-pending-successor"}}
    session.flush()
    assert is_valid(session, journal.id) is False
    assert job.id in {row.id for row in session.scalars(unresolved_jobs_query(ACCOUNTS))}


def test_evidence_cap_is_unknown_not_silently_truncated(census):
    session, accounts, strategy = census
    journal = attempt(session)
    child = order(session, accounts, strategy)
    session.add_all(BrokerOrderEvent(order_id=child.id, event_type="cancelled", event_source="broker",
        event_at=datetime.now(UTC) - timedelta(seconds=30), payload={}) for _ in range(64))
    session.flush()
    assert retire_batch(session, ACCOUNTS, None, observed())[1] == 0
    assert session.get(Retirement, journal.id) is None


def seed_old_closed(session, accounts, strategy, count, *, alternate_account=None):
    now = datetime.now(UTC)
    opened, closed = now - timedelta(days=2, minutes=1), now - timedelta(days=2)
    journals, parents, orders, events = [], [], [], []
    for index in range(count):
        account = alternate_account if alternate_account is not None and index % 2 else accounts[0]
        token, parent_id, order_id = f"old-token-{index}", uuid4(), uuid4()
        metadata = {"clearwait_dispatch_token": token, "fanout_segment_id": "100",
                    "brokerdecides_line_segment_ms": "900"}
        journals.append(dict(id=uuid4(), snapshot_type="v2_wait_dispatch", created_at=opened,
            payload={"schema_version": 1, "kind": "attempt", "strategy_code": "schwab_1m_v2",
                "symbol": "OLD", "account_name": account.name,
                "account_names": [account.name, accounts[1].name],
                "opportunity_id": "100", "attempt_token": token}))
        parents.append(dict(id=parent_id, strategy_id=strategy.id, broker_account_id=account.id,
            symbol="OLD", side="buy", intent_type="open", quantity=Decimal("1"), reason="ATR Flip",
            status="cancelled", payload={"metadata": metadata}, created_at=opened, updated_at=closed))
        orders.append(dict(id=order_id, intent_id=parent_id, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id=f"old-client-{index}",
            broker_order_id=f"old-native-{index}", symbol="OLD", side="buy", quantity=Decimal("1"),
            order_type="stop_limit", time_in_force="day", status="cancelled", payload=metadata,
            submitted_at=opened, updated_at=closed))
        events.append(dict(id=uuid4(), order_id=order_id, event_type="cancelled", event_source="broker",
            event_at=closed, payload={"client_order_id": f"old-client-{index}",
                                      "broker_order_id": f"old-native-{index}"}))
    for model, values in ((DashboardSnapshot, journals), (TradeIntent, parents),
                          (BrokerOrder, orders), (BrokerOrderEvent, events)):
        session.execute(insert(model), values)
    session.commit()


def test_p6_over_4096_closed_history_old_unknown_and_new_admission(census):
    session, accounts, strategy = census
    count = 4097
    unknown = attempt(session)
    unknown.created_at = datetime.now(UTC) - timedelta(days=3)
    old_alias = BrokerAccount(name="live:retired-v2-alias", provider="schwab", environment="production")
    session.add(old_alias)
    session.flush()
    seed_old_closed(session, accounts, strategy, count, alternate_account=old_alias)
    with pytest.raises(ValueError, match="dispatch_attempt_census_overflow"):
        local_evidence(session, ACCOUNTS)
    session.rollback()
    cursor, total = None, 0
    # Keyset visits past the older UNKNOWN; finite batches, not an unbounded loop.
    for _ in range((count + 1 + BATCH - 1) // BATCH):
        cursor, retired, visited = retire_batch(session, ACCOUNTS, cursor, observed())
        assert 0 < visited <= BATCH and 0 <= retired <= visited
        total += retired
        session.commit()
    assert total == count
    assert retire_batch(session, ACCOUNTS, cursor, observed()) == (None, 0, 0)
    inflight, fills, terminals = local_evidence(session, ACCOUNTS)
    assert inflight == frozenset({"WORX"}) and not fills and not terminals
    assert session.get(Retirement, unknown.id) is None
    assert session.scalar(select(func.count()).select_from(Retirement)) == count
    assert set(session.scalars(select(Retirement.account_name).distinct())) == {ACCOUNTS[0], old_alias.name}
    assert session.scalar(select(func.count()).select_from(DashboardSnapshot)) == count + 1
    assert session.scalar(select(func.count()).select_from(BrokerOrder)) == count
    assert session.scalar(select(func.count()).select_from(TradeIntent)) == count
    assert session.scalar(select(func.count()).select_from(BrokerOrderEvent)) == count
    assert session.scalar(select(func.count()).select_from(Fill)) == 0
    # Synthetic two-book shapes qualify ONLY this local decision control.
    books = tuple(NativeAccountBook(name, provider, 1000, 1100, frozenset(), frozenset(),
        True, "private synthetic") for name, provider in zip(ACCOUNTS, ("schwab", "webull"), strict=True))
    memory = AdmissionMemory()
    memory.apply(AdmissionSnapshot("private-epoch", 1, 1000, 1200, books,
        inflight, fills, True, "private synthetic"))
    assert memory.assess(accounts=ACCOUNTS, symbol="NEW", segment=900,
        now_ms=1300, max_age_ms=20_000).allowed
    assert not memory.assess(accounts=ACCOUNTS, symbol="WORX", segment=900,
        now_ms=1300, max_age_ms=20_000).allowed
    # A missed publisher pass cannot lose an old exact terminal token.
    assert load_terminal_tokens(session, ACCOUNTS, {"old-token-0"}) == frozenset({("OLD", "old-token-0")})
    assert load_terminal_tokens(session, ACCOUNTS, {"persisted-attempt"}) == frozenset()


def test_projection_missing_evidence_is_unknown_never_partial_success(census):
    session, accounts, strategy = census
    journal, child = seal_one(census)
    session.execute(delete(Evidence).where(Evidence.attempt_id == journal.id,
                                         Evidence.entity_kind == "event"))
    session.flush()
    assert is_valid(session, journal.id) is False
    assert "WORX" in local_evidence(session, ACCOUNTS)[0]
    assert not load_terminal_tokens(session, ACCOUNTS, {"persisted-attempt"})


def test_unrelated_reference_configuration_does_not_reopen_closed_economic_history(census):
    session, accounts, strategy = census
    journal, child = seal_one(census)
    strategy.metadata_json = {"unrelated_configuration": "changed"}
    accounts[0].is_active = False
    session.flush()
    assert is_valid(session, journal.id) is True
    # This validates past closure, not permission to send on a disabled account.
    # Original route/control/owner flags still decide current admission.


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "foreign_token", "changed_proof"])
async def test_actual_consumer_reads_its_exact_old_terminal_receipt(census, monkeypatch, fault):
    session, accounts, strategy = census
    journal, child = seal_one(census)
    if fault == "changed_proof":
        child.payload = {**child.payload, "changed": True}
        session.flush()
    memory = AdmissionMemory()
    memory.reserve("WORX", "foreign-token" if fault == "foreign_token" else "persisted-attempt")
    value = AdmissionSnapshot("private-epoch", 1, 1000, 1200, (),
        frozenset(), frozenset(), True, "private closure lookup only")

    class Redis:
        async def get(self, key):
            return encode(value)

    @contextmanager
    def factory():
        yield session

    calls = []

    async def private_thread(function):
        calls.append("private DB delegate")
        return function()

    # Controlled private thread injection; no production session/engine involved.
    monkeypatch.setattr("project_mai_tai.brokerdecides1_sync.asyncio.to_thread", private_thread)
    settings = SimpleNamespace(strategy_schwab_1m_v2_brokerdecides1_enabled=True,
        strategy_schwab_1m_v2_account_name=ACCOUNTS[0],
        strategy_schwab_1m_v2_webull_account_name=ACCOUNTS[1], redis_stream_prefix="private")
    service = SimpleNamespace(settings=settings, redis=Redis(), session_factory=factory,
        strategy=SimpleNamespace(_brokerdecides1_memory=memory))
    await refresh_consumer(service)
    assert calls == ["private DB delegate"]
    assert bool(memory.reservation_tokens()) == (fault != "none")
