"""Real recovery consumer with retained rows and CONTROLLED owner/native state."""
import os

import pytest
from sqlalchemy import select

from project_mai_tai import brokerdecides1_sync as sync
from project_mai_tai.db.models import DashboardSnapshot
from tests.unit.wbpa3_episode_port import ACCOUNT_NAMES, OPPORTUNITY
from tests.unit.test_wbpa3_positive_rejection_v3 import driver, episode, terminal_recovery

pytestmark = pytest.mark.skipif(not os.environ.get("WBPA3_SOURCE_DIRECTORY"),
    reason="Requires reviewed private retained CHOW inputs")


def test_real_unknown_owner_recovery_consumes_proven_historical_unfilled_set(episode):
    case = episode
    with case.factory() as session:
        terminal, _ = terminal_recovery(session)
        inflight, fills, tokens = sync.local_evidence(session, ACCOUNT_NAMES)
    assert terminal == {"CHOW": frozenset({OPPORTUNITY})}
    strategy, state = driver(case, inflight, fills, tokens, phase="unknown")
    # Missing proof retains the real process ownership, even with a controlled
    # fresh/flat card; no native fact or current checkpoint is inferred here.
    assert not strategy._recover_unknown_flip_owner(state, (), (), frozenset())
    assert state.flip_owner_phase == "unknown" and state.flip_owner_opportunity_id == OPPORTUNITY
    assert not strategy._recover_unknown_flip_owner(state, (), (), terminal["CHOW"])
    assert state.flip_owner_phase == "idle"
    assert state.flip_owner_opportunity_id == state.fanout_segment_id == 0
    with case.factory() as session:
        rows = list(session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "v2_flip_entry_ownership")))
        assert rows and rows[-1].payload["active"] is False
        assert rows[-1].payload["reason"] == "proven_empty_first_rest_opportunity"
