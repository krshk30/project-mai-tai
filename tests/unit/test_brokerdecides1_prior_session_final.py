"""C7B-min: prior-session v2 attempts with no order row are final for admission (DAY-only)."""
from datetime import UTC, datetime, timedelta

from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai.fanout_segment_store import current_session_anchor
from tests.unit.test_brokerdecides1_local_census import ACCOUNTS, attempt, census, order  # noqa: F401


def _age(row, created_at):
    row.created_at = created_at
    return row


def test_prior_session_attempt_without_order_is_final_only_with_the_flag(census):
    session, _, _ = census
    # NCPL shape: a 10-12 attempt token that never produced an order row.
    _age(attempt(session), current_session_anchor() - timedelta(days=16))
    session.flush()
    assert local_evidence(session, ACCOUNTS)[0] == frozenset({"WORX"})
    inflight, fills, terminals = local_evidence(session, ACCOUNTS, prior_session_attempts_final=True)
    assert inflight == frozenset() and not fills and not terminals


def test_current_session_attempt_without_order_stays_unknown_with_the_flag(census):
    session, _, _ = census
    _age(attempt(session), current_session_anchor() + timedelta(minutes=1))  # a lost ACK today
    session.flush()
    assert local_evidence(session, ACCOUNTS, prior_session_attempts_final=True)[0] == frozenset({"WORX"})


def test_prior_session_attempt_with_an_open_order_row_is_not_finalized(census):
    session, accounts, strategy = census
    _age(attempt(session), current_session_anchor() - timedelta(days=1))
    order(session, accounts, strategy, status="accepted")
    session.flush()
    assert local_evidence(session, ACCOUNTS, prior_session_attempts_final=True)[0] == frozenset({"WORX"})


def test_naive_created_at_is_read_as_utc(census):
    session, _, _ = census
    stamp = (current_session_anchor() - timedelta(hours=1)).astimezone(UTC).replace(tzinfo=None)
    _age(attempt(session), stamp)
    session.flush()
    assert local_evidence(session, ACCOUNTS, prior_session_attempts_final=True)[0] == frozenset()


def test_prior_session_attempt_whose_order_is_terminal_is_final(census):
    # NCPL 10-12: the order row exists but cancel/reprice rewrote its payload metadata.
    session, accounts, strategy = census
    _age(attempt(session), current_session_anchor() - timedelta(days=16))
    row = order(session, accounts, strategy, status="cancelled", source=None)
    row.payload = {"clearwait_dispatch_token": "persisted-attempt", "resting_entry_cancel": "true",
                   "reason": "reprice"}  # token kept, fanout_segment_id lost
    session.flush()
    assert local_evidence(session, ACCOUNTS)[0] == frozenset({"WORX"})
    assert local_evidence(session, ACCOUNTS, prior_session_attempts_final=True)[0] == frozenset()
