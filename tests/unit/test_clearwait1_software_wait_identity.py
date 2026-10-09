"""New software-wait identity and exact idempotent retirement; not legacy proof."""

from dataclasses import replace

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.v2_removed_wait import RemovedWaitProof
from tests.unit.test_clearwait1_session_rollover import (
    NOW, ms, request, service, strategy,
)
from tests.unit.test_clearwait1_session_rollover import db as local_db

db = local_db


@pytest.mark.parametrize("fault", ["none", "legacy_request", "working_rest", "unknown",
    "owned", "owner_unreadable", "removal_unreadable", "identity_write",
    "identity_missing", "dispatch_missing", "first_rest"])
def test_new_software_wait_binds_only_current_readable_unowned_request(db, fault):
    req = replace(request(), symbol="JZ", opportunity_id=0, requested_at_ms=ms(NOW))
    strat = strategy(req)
    strat._removed_wait_requests = {}
    strat.configure_removed_wait(db[0].record, restored={}, readable=True,
                                dispatch_persist=db[0].record_dispatch)
    bot = service(strat, db[0])
    bot._configure_flip_entry_ownership_store({})
    identities = FanoutSegmentIdentityStore(db[1])
    def persist(symbol, generation, active, reason):
        if fault == "identity_write":
            raise RuntimeError("controlled identity persistence failure")
        identities.record(symbol, generation, active, reason, now=NOW)
    strat.configure_fanout_identity_persistence(persist)
    state = strat.watchlist_state("JZ")
    state.cw_armed = True
    if fault == "legacy_request":
        db[0].record(req, True)
        strat._removed_wait_requests["JZ"] = req
    elif fault == "working_rest":
        state.resting_active = True
    elif fault == "unknown":
        state.flip_owner_phase = "unknown"
    elif fault == "owned":
        state.flip_owner_phase = "consumed"
    elif fault == "owner_unreadable":
        strat._flip_owner_restore_readable = False
    elif fault == "removal_unreadable":
        strat._removed_wait_restore_readable = False
    elif fault == "identity_missing":
        strat._fanout_identity_persist = None
    elif fault == "dispatch_missing":
        strat._removed_wait_dispatch_persist = None
    elif fault == "first_rest":
        state.flip_owner_first_rest_placed = True
    strat.release_and_drop_symbol("JZ")
    with db[1]() as session:
        binds = list(session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "v2_fanout_segment_identity")))
    assert bool(binds) is (fault == "none")
    if fault == "none":
        current = strat._removed_wait_requests["JZ"]
        assert current.opportunity_id == ms(NOW) and current.token != req.token
        assert db[0].restore() == {"JZ": current}
        assert binds[0].payload["reason"] == "flip_owned_opportunity_v2_bind"
    elif fault == "legacy_request":
        assert strat._removed_wait_requests == {"JZ": req} and db[0].restore() == {"JZ": req}
    elif fault == "identity_write":
        assert state.flip_owner_phase == "unknown"
    elif fault == "owned":
        assert state.flip_owner_phase == "consumed" and not strat._removed_wait_requests


def test_unknown_owner_is_not_released_by_unbound_db_terminal_witness(db):
    req = replace(request(), requested_at_ms=ms(NOW))
    strat = strategy(req)
    state = strat.watchlist_state(req.symbol)
    state.flip_owner_opportunity_id = state.fanout_segment_id = req.opportunity_id
    state.flip_owner_phase = "unknown"
    strat.apply_removed_wait_proofs((RemovedWaitProof(req, ms(NOW), True, "unbound_symbol_terminal"),))
    assert strat._removed_wait_requests == {req.symbol: req}
    assert state.flip_owner_phase == "unknown" and state.fanout_segment_id == req.opportunity_id


@pytest.mark.parametrize("fault", ["none", "token", "accounts", "time", "extra", "verdict", "active"])
def test_already_atomic_terminal_retirement_is_exact_and_does_not_retime(db, fault):
    req = replace(request(), requested_at_ms=ms(NOW))
    payload = {**req.payload(active=False), "reason": "unbound_symbol_terminal",
        "verdict": "TERMINAL", "closed_owned_rows": [], "terminal_observed_at_ms": ms(NOW)}
    if fault == "token":
        payload["token"] = "new-request"
    elif fault == "accounts":
        payload["account_names"] = [req.account_names[0]]
    elif fault == "time":
        payload["requested_at_ms"] = str(req.requested_at_ms + 1)
    elif fault == "extra":
        payload["unexpected"] = True
    elif fault == "verdict":
        payload["verdict"] = "UNKNOWN"
    elif fault == "active":
        payload["active"] = 0
    with db[1]() as session:
        row = DashboardSnapshot(snapshot_type="v2_removed_wait", payload=payload, created_at=NOW)
        session.add(row)
        session.commit()
        row_id = row.id
    if fault == "none":
        db[0].record(req, False)
    else:
        with pytest.raises(ValueError, match="terminal witness active request changed"):
            db[0].record(req, False)
    with db[1]() as session:
        rows = list(session.scalars(select(DashboardSnapshot)))
        assert len(rows) == 1 and rows[0].id == row_id and rows[0].payload == payload
        assert rows[0].created_at.replace(tzinfo=NOW.tzinfo) == NOW
