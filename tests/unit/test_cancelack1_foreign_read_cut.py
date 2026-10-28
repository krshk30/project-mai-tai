"""UNRUN author controls: explicit read cut never rewrites original dispatch scope."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from project_mai_tai.cancelack1 import load_proven_accounts, optional_proven_accounts
from project_mai_tai.db.models import BrokerAccount, DashboardSnapshot, Strategy
from project_mai_tai.v2_removed_wait import DISPATCH_SNAPSHOT_TYPE
from tests.unit.test_cancelack1 import synthetic_case
from tests.unit.test_cancelack1_restart_review import later_closed_episode
from tests.unit.test_v2_fresh_sell_owner_release import MIRROR, PRIMARY, instant


@pytest.mark.parametrize("required", [(PRIMARY,), (MIRROR,)])
@pytest.mark.parametrize("fault", ["none", "closed_bound", "unknown_owner", "missing_segment", "same_episode",
    "same_token", "wrong_order_token", "orphan_order", "possibly_sent", "still_managed"])
def test_later_episode_needs_current_complete_identity_and_closure(monkeypatch, required, fault):
    # All native rows/receipts in this test are synthetic controls, not WORX facts.
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, required=required)
    original = request.payload(active=True)
    try:
        later_account = MIRROR if required == (PRIMARY,) else PRIMARY
        later_closed_episode(bot, request, clock, later_account, fault=fault)
        with bot.session_factory() as session:
            accounts = {a.id: a.name for a in session.scalars(select(BrokerAccount))}
            strategy = session.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
            # No implicit wall clock/default widening: old API remains conservative.
            with pytest.raises(ValueError):
                load_proven_accounts(session, request, accounts, strategy)
            expected = fault in {"none", "closed_bound"}
            if expected:
                assert load_proven_accounts(session, request, accounts, strategy,
                    observed_at_ms=clock[0]) == frozenset(required)
            else:
                with pytest.raises(ValueError):
                    load_proven_accounts(session, request, accounts, strategy, observed_at_ms=clock[0])
        proof, = bot._fetch_fresh_sell_release_evidence((request,))
        assert proof.clear is expected
        assert request.payload(active=True) == original
        assert state.flip_owner_phase == "bound" and writes == []  # Read does not release.
    finally:
        engine.dispose()


@pytest.mark.parametrize("bad_cut", [-1, True, "latest"])
def test_invalid_read_cut_stays_unknown(monkeypatch, bad_cut):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch)
    try:
        with bot.session_factory() as session:
            accounts = {a.id: a.name for a in session.scalars(select(BrokerAccount))}
            strategy = session.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
            with pytest.raises(ValueError, match="clock_UNKNOWN"):
                load_proven_accounts(session, request, accounts, strategy, observed_at_ms=bad_cut)
            assert optional_proven_accounts(session, request, accounts, strategy, observed_at_ms=bad_cut) is None
    finally:
        engine.dispose()


def test_original_episode_journal_cannot_borrow_later_read_cut(monkeypatch):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, required=(MIRROR,))
    try:
        later_closed_episode(bot, request, clock, PRIMARY)
        with bot.session_factory() as session:
            attempt = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == DISPATCH_SNAPSHOT_TYPE,
                DashboardSnapshot.payload["opportunity_id"].as_string() == str(request.opportunity_id),
                DashboardSnapshot.payload["kind"].as_string() == "attempt"))
            attempt.created_at = instant(request.requested_at_ms + 1)
            session.commit()
        with bot.session_factory() as session:
            accounts = {a.id: a.name for a in session.scalars(select(BrokerAccount))}
            strategy = session.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
            with pytest.raises(ValueError):
                load_proven_accounts(session, request, accounts, strategy, observed_at_ms=clock[0])
    finally:
        engine.dispose()


def test_actual_archive_projection_has_no_missing_atr_segments():
    # A field inventory only: this does NOT assert closure/native acceptance.
    data = json.loads((Path(__file__).parents[1] /
        "fixtures/cancelack1_worx_foreign_archive_20261021.json").read_text())
    rows = data["atr_buy_open_rows"]
    assert len(rows) == 8 and len({row["id"] for row in rows}) == 8
    assert all(isinstance(row["segment"], str) and row["segment"].isdigit() for row in rows)
    own = data["cancel"]["opportunity"]
    assert sum(row["segment"] == own for row in rows) == 3
    assert sum(int(row["segment"]) > int(own) for row in rows) == 5
