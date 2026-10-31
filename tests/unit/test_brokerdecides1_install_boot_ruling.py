"""21:01 boot0 excludes UNKNOWN; runtime re-invalidation remains mandatory."""

from dataclasses import replace
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import delete, select

from project_mai_tai.brokerdecides1_install_chunks import (
    InstallBootReport, InstallChunk, committed_install_partition, finalize_install_before_start,
    read_install_chunk, resume_install,
    verify_install_boot,
)
from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai.db.models import (
    BrokerDecidesDispatchEvidence as Evidence, BrokerDecidesDispatchRetirement as Retirement,
    BrokerOrderEvent, DashboardSnapshot, Fill,
)
from tests.unit.test_brokerdecides1_card_boot_restart import controlled_service, publish_complete
from tests.unit.test_brokerdecides1_local_census import ACCOUNTS, attempt, census as census
from tests.unit.test_brokerdecides1_retirement import observed, seal_one, seed_old_closed


def installed_case(census):
    session, accounts, strategy = census
    unknown = attempt(session)
    unknown.created_at = datetime.now(UTC) - timedelta(days=3)
    unknown_id = unknown.id
    seed_old_closed(session, accounts, strategy, 3)
    upper = max(session.scalars(select(DashboardSnapshot.id)), key=lambda identity: identity.int)
    first = read_install_chunk(session, ACCOUNTS, after_id=None, upper_id=upper, observed_ms=observed())
    session.commit()
    final = read_install_chunk(session, ACCOUNTS, after_id=first.next_id, upper_id=upper, observed_ms=observed())
    session.commit()
    assert final.exhausted
    return unknown_id, (first, final)


def test_boot_zero_retireable_keeps_older_unknown_symbol_age_and_source(census):
    session, _, _ = census
    unknown_id, chunks = installed_case(census)
    sink = Mock()
    report = verify_install_boot(session, ACCOUNTS, chunks, observed_ms=observed(), report_unknown=sink)
    assert report.install_complete and report.retireable_remaining == 0
    assert report.verified_count == 3
    assert len(report.unknown) == 1
    fence = report.unknown[0]
    assert fence.identity == unknown_id and fence.symbol == "WORX"
    assert fence.age_ms >= 3 * 86400_000
    sink.assert_called_once_with(fence)
    assert session.get(Retirement, unknown_id) is None
    inflight, _, _ = local_evidence(session, ACCOUNTS)
    assert inflight == frozenset({"WORX"})
    assert "UNRELATED" not in inflight


def test_boot_missing_positive_projection_is_nonzero_not_unknown_retirement(census):
    session, _, _ = census
    _, chunks = installed_case(census)
    identity = chunks[0].verified[0]
    session.execute(delete(Evidence).where(Evidence.attempt_id == identity))
    session.execute(delete(Retirement).where(Retirement.attempt_id == identity))
    report = verify_install_boot(session, ACCOUNTS, chunks, observed_ms=observed(), report_unknown=Mock())
    assert report.retireable_remaining == 1 and not report.install_complete
    assert report.unknown[0].symbol == "WORX"


@pytest.mark.parametrize("change", ["late_fill", "ownership", "event_missing", "new_event"])
def test_late_changes_reactivate_own_symbol_even_when_projection_exists(census, change):
    session, accounts, strategy = census
    journal, child = seal_one(census)
    assert local_evidence(session, ACCOUNTS)[0] == frozenset()
    if change == "late_fill":
        session.add(Fill(order_id=child.id, strategy_id=strategy.id,
            broker_account_id=accounts[0].id, broker_fill_id="install-late",
            symbol="WORX", side="buy", quantity=Decimal("0.5"), price=Decimal("3"),
            filled_at=datetime.now(UTC)))
    elif change == "ownership":
        accounts[0].external_account_id = "changed-native-owner"
    elif change == "event_missing":
        session.execute(delete(BrokerOrderEvent).where(BrokerOrderEvent.order_id == child.id))
    else:
        session.add(BrokerOrderEvent(order_id=child.id, event_type="pending_cancel",
            event_source="broker", payload={}, event_at=datetime.now(UTC)))
    session.flush()
    assert session.get(Retirement, journal.id) is not None
    assert local_evidence(session, ACCOUNTS)[0] == frozenset({"WORX"})


@pytest.mark.parametrize("change", ["no_end", "duplicate", "omit_unknown", "wrong_cursor",
    "new_upper", "missing_first", "bad_visited"])
def test_boot_rejects_partial_or_rewritten_committed_partition(change):
    first = InstallChunk(UUID(int=9), None, UUID(int=2), 2, 1,
        (UUID(int=1),), (UUID(int=2),), False)
    end = InstallChunk(UUID(int=9), UUID(int=2), UUID(int=2), 0, 0, (), (), True)
    chunks = [first, end]
    if change == "no_end":
        chunks.pop()
    elif change == "duplicate":
        chunks[0] = replace(first, unknown=first.verified)
    elif change == "omit_unknown":
        chunks[0] = replace(first, unknown=())
    elif change == "wrong_cursor":
        chunks[0] = replace(first, next_id=UUID(int=3))
    elif change == "new_upper":
        chunks[1] = replace(end, upper_id=UUID(int=10))
    elif change == "missing_first":
        chunks = [end]
    else:
        chunks[0] = replace(first, visited=True)
    with pytest.raises(ValueError):
        committed_install_partition(chunks)


@pytest.mark.parametrize("budget", [0, 501, True])
def test_resumable_install_requires_finite_integer_chunk_budget(budget):
    factory = Mock()
    with pytest.raises(ValueError, match="chunk_budget_UNKNOWN"):
        resume_install(factory, ACCOUNTS, upper_id=UUID(int=9), committed_chunks=(),
            chunk_budget=budget, observe_ms=Mock(), require_stopped_writers=Mock(), journal_committed=Mock())
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_normal_native_sync_no_longer_creates_retirement_projections(monkeypatch, census):
    producer, consumer, _, _, stages = controlled_service(monkeypatch, census)
    await publish_complete(producer, consumer)
    assert [commit for kind, commit in stages if kind == "db"] == [False]
    assert consumer.strategy._brokerdecides1_memory.snapshot.local_complete


def test_finalize_refuses_missing_stopped_proof_before_db():
    factory = Mock()
    with pytest.raises(ValueError, match="boot_writers_not_proven_stopped"):
        finalize_install_before_start(factory, ACCOUNTS, (), observed_ms=1,
            require_stopped_writers=lambda: None, report_unknown=Mock())
    factory.assert_not_called()


@pytest.mark.parametrize("remaining", [0, 1])
def test_finalize_cannot_complete_nonzero_retireable_count(monkeypatch, remaining):
    from project_mai_tai import brokerdecides1_install_chunks as module

    session = Mock()
    session.in_transaction.return_value = False
    report = InstallBootReport(remaining, 3, ())
    monkeypatch.setattr(module, "verify_install_boot", lambda *args, **kwargs: report)

    @contextmanager
    def factory():
        yield session

    kwargs = dict(observed_ms=1, require_stopped_writers=lambda: True, report_unknown=Mock())
    if remaining:
        with pytest.raises(ValueError, match="retireable_old_notes_remain"):
            finalize_install_before_start(factory, ACCOUNTS, (), **kwargs)
    else:
        assert finalize_install_before_start(factory, ACCOUNTS, (), **kwargs) is report
        session.rollback.assert_called_once()
    session.commit.assert_not_called()
