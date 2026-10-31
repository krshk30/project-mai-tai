"""Install chunk/source controls, not4097 PG rehearsal or boot permission."""

from contextlib import contextmanager
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from project_mai_tai.brokerdecides1_install_chunks import (
    read_install_chunk, run_committed_chunk, unprojected_install_count, validate_bounds,
)
from project_mai_tai.brokerdecides1_retirement import BATCH, unresolved_query
from project_mai_tai.db.models import BrokerDecidesDispatchRetirement as Retirement, DashboardSnapshot
from tests.unit.test_brokerdecides1_local_census import ACCOUNTS, attempt, census as census
from tests.unit.test_brokerdecides1_retirement import observed, seed_old_closed


@pytest.mark.parametrize("after,upper", [(None, None), (None, "uuid"), (True, uuid4()),
    (UUID(int=2), UUID(int=1))])
def test_invalid_key_bounds_refused(after, upper):
    with pytest.raises(ValueError, match="install_key_bounds_UNKNOWN"):
        validate_bounds(after, upper)


def test_chunk_traversal_resumes_keeps_unknown_and_does_not_refresh(census):
    session, accounts, strategy = census
    unknown = attempt(session)
    unknown_id = unknown.id
    seed_old_closed(session, accounts, strategy, 65)
    identities = tuple(session.scalars(select(DashboardSnapshot.id)))
    upper = max(identities, key=lambda identity: identity.int)
    first = read_install_chunk(session, ACCOUNTS, after_id=None, upper_id=upper, observed_ms=observed())
    session.commit()
    original_count = session.scalar(select(func.count()).select_from(Retirement))
    replay = read_install_chunk(session, ACCOUNTS, after_id=None, upper_id=upper, observed_ms=observed())
    session.commit()
    assert replay.new_retirements == 0
    assert replay.verified == first.verified and replay.unknown == first.unknown
    assert session.scalar(select(func.count()).select_from(Retirement)) == original_count
    visited, unknown_ids = set(), set(first.unknown)
    visited.update((*first.verified, *first.unknown))
    cursor = first.next_id
    for _ in range(4):
        chunk = read_install_chunk(session, ACCOUNTS, after_id=cursor, upper_id=upper, observed_ms=observed())
        session.commit()
        assert chunk.visited <= BATCH <= 500
        if chunk.exhausted:
            break
        visited.update((*chunk.verified, *chunk.unknown))
        unknown_ids.update(chunk.unknown)
        cursor = chunk.next_id
    else:
        pytest.fail("bounded source traversal did not exhaust")
    assert visited == set(identities)
    assert unknown_ids == {unknown_id}
    assert session.get(Retirement, unknown_id) is None
    assert session.scalar(select(func.count()).select_from(Retirement)) == 65
    assert unknown_id in {row.id for row in session.scalars(unresolved_query(ACCOUNTS))}


@pytest.mark.parametrize("proof", [False, None, "true"])
def test_runner_requires_explicit_stopped_proof_before_opening_session(proof):
    factory, journal = Mock(), Mock()
    with pytest.raises(ValueError, match="not_proven_stopped"):
        run_committed_chunk(factory, ACCOUNTS, after_id=None, upper_id=uuid4(), observed_ms=1,
            require_stopped_writers=lambda: proof, journal_committed=journal)
    factory.assert_not_called()
    journal.assert_not_called()


def test_changed_writer_proof_rolls_back_no_journal(monkeypatch):
    from project_mai_tai import brokerdecides1_install_chunks as module

    session, journal = Mock(), Mock()

    @contextmanager
    def factory():
        try:
            yield session
        except Exception:
            session.rollback()
            raise

    proof = Mock(side_effect=[True, False])
    monkeypatch.setattr(module, "read_install_chunk", lambda *args, **kwargs: "UNCOMMITTED")
    with pytest.raises(ValueError, match="changed_before_commit"):
        run_committed_chunk(factory, ACCOUNTS, after_id=None, upper_id=uuid4(), observed_ms=1,
            require_stopped_writers=proof, journal_committed=journal)
    session.commit.assert_not_called()
    session.rollback.assert_called_once()
    journal.assert_not_called()


def test_unknown_commit_reply_does_not_advance_journal(monkeypatch):
    from project_mai_tai import brokerdecides1_install_chunks as module

    session, journal = Mock(), Mock()
    session.commit.side_effect = RuntimeError("unknown_commit_reply")

    @contextmanager
    def factory():
        yield session

    monkeypatch.setattr(module, "read_install_chunk", lambda *args, **kwargs: "UNCOMMITTED")
    with pytest.raises(RuntimeError, match="unknown_commit_reply"):
        run_committed_chunk(factory, ACCOUNTS, after_id=None, upper_id=uuid4(), observed_ms=1,
            require_stopped_writers=lambda: True, journal_committed=journal)
    journal.assert_not_called()


@pytest.mark.parametrize("identities", [(), (uuid4(),) * 2, tuple(uuid4() for _ in range(501)), ("bad",)])
def test_structural_boot_count_requires_bounded_explicit_unique_ids(identities):
    session = Mock()
    with pytest.raises(ValueError, match="eligible_ids_UNKNOWN"):
        unprojected_install_count(session, identities)
    session.scalar.assert_not_called()
