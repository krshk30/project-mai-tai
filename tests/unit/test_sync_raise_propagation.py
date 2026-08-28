"""§46.1 — L1's raise must NOT kill the sync loop.

⛔⭐⭐ THE HIGHEST-RISK REGRESSION OF THE L1/L2/L3 CHANGE. L1 introduced raises into a path that
previously could not raise and had **no try/except at all**. If a raise escapes the per-account loop,
positions stop syncing entirely — silently — which is strictly worse than the defect that was fixed
and is the house failure mode (a component reporting healthy while its function is dead).

The structural tests in `test_schwab_never_synthesize_flat.py` assert the SHAPE of the guard by
reading source. These assert the BEHAVIOUR by running the loop: **account A raises, account B still
syncs.** Asserted, not relied upon.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from uuid import uuid4

from project_mai_tai.broker_adapters.schwab import SchwabPositionsUnavailable
from project_mai_tai.oms import service as svc

A_ID, B_ID = uuid4(), uuid4()
A_NAME, B_NAME = "live:schwab_1m_v2", "live:orb"


class _Store:
    """Records exactly which accounts reached the zeroing/erasing DB calls."""

    def __init__(self) -> None:
        self.synced: list[tuple] = []
        self.cleared_scope: list | None = None
        self.clear_min_age_seconds: float | None = None
        self.restored_scope: list | None = None

    def list_active_broker_accounts(self, session):
        return [SimpleNamespace(id=A_ID, name=A_NAME), SimpleNamespace(id=B_ID, name=B_NAME)]

    def list_named_broker_accounts(self, session, names):
        return self.list_active_broker_accounts(session)

    def sync_account_positions(self, session, *, broker_account_id, snapshots):
        self.synced.append((broker_account_id, list(snapshots)))
        return len(snapshots)

    def clear_virtual_positions_without_account_backing(
        self,
        session,
        *,
        broker_account_ids=None,
        minimum_age_seconds=0.0,
        observed_at=None,
        deferred_out=None,
    ):
        del observed_at, deferred_out
        self.cleared_scope = list(broker_account_ids or [])
        self.clear_min_age_seconds = float(minimum_age_seconds)
        return []

    def restore_virtual_positions_from_managed(self, session, *, broker_account_ids=None):
        self.restored_scope = list(broker_account_ids or [])
        return []


class _Adapter:
    """Account A's read FAILS (the 08-12 CRWU / 08-14 VWAV shape). Account B's succeeds."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_account_positions(self, broker_account_name: str):
        self.calls.append(broker_account_name)
        if broker_account_name == A_NAME:
            raise SchwabPositionsUnavailable("read timed out")
        return [SimpleNamespace(symbol="WFF", quantity=1)]


def _svc(store: _Store, adapter: _Adapter) -> svc.OmsRiskService:
    s = object.__new__(svc.OmsRiskService)
    s.store = store
    s.broker_adapter = adapter
    s.logger = logging.getLogger("test-sync-raise")
    s.settings = SimpleNamespace()

    async def _run_db(fn, *, commit: bool = True):
        return fn(object())          # a stand-in session; the store stubs ignore it

    s._run_db = _run_db
    s._observe_settlement = lambda *a, **k: None
    return s


def _run(s):
    return asyncio.run(s.sync_broker_positions())


def test_a_raising_account_does_NOT_abort_the_sync_for_other_accounts() -> None:
    """⛔⭐⭐ THE CORE OF §46.1. Account A raises; account B must still be read AND synced.

    Before this change the call was bare, so one raising account aborted every account — reachable
    already, since the Webull adapter has raised since 2026-07-24."""
    store, adapter = _Store(), _Adapter()
    _run(_svc(store, adapter))

    assert adapter.calls == [A_NAME, B_NAME], "the loop must continue to account B after A raised"
    synced_ids = [aid for aid, _ in store.synced]
    assert B_ID in synced_ids, "account B's snapshot never reached sync_account_positions"
    assert A_ID not in synced_ids, "the FAILED account reached the zeroing sync — L2 is broken"


def test_the_sync_returns_normally_rather_than_propagating() -> None:
    """A raise escaping here would kill the caller's periodic task — positions stop syncing
    silently, which is worse than the defect being fixed."""
    store, adapter = _Store(), _Adapter()
    result = _run(_svc(store, adapter))          # must not raise
    assert result is not None


def test_the_one_way_CLEAR_is_scoped_to_the_readable_account_only() -> None:
    """⛔ The erasure must never run against an account whose read just failed — that is L2 from
    the other end, and it is the hole I introduced and had to catch by re-reading the diff."""
    store, adapter = _Store(), _Adapter()
    _run(_svc(store, adapter))
    assert store.cleared_scope == [B_ID], (
        f"the one-way clear was scoped to {store.cleared_scope}, must be [B_ID] only"
    )
    assert store.clear_min_age_seconds == 24.119


def test_the_L3_RESTORE_is_scoped_the_same_way() -> None:
    store, adapter = _Store(), _Adapter()
    _run(_svc(store, adapter))
    assert store.restored_scope == [B_ID]


def test_ALL_accounts_failing_still_returns_and_zeroes_NOTHING() -> None:
    """The degenerate case: a total outage must be a no-op, never a fleet-wide flatten."""
    class _AllFail(_Adapter):
        async def list_account_positions(self, broker_account_name: str):
            self.calls.append(broker_account_name)
            raise SchwabPositionsUnavailable("total outage")

    store, adapter = _Store(), _AllFail()
    _run(_svc(store, adapter))
    assert adapter.calls == [A_NAME, B_NAME], "every account is still attempted"
    assert store.synced == [], "nothing may be synced when every read failed"
    assert store.cleared_scope == [], "nothing may be cleared when every read failed"


def test_a_GENUINELY_FLAT_account_still_syncs_its_empty_snapshot() -> None:
    """⛔⭐ THE REGRESSION DIRECTION. An empty list from a SUCCESSFUL read is a real flat account
    and must still zero — otherwise re-entry breaks."""
    class _Flat(_Adapter):
        async def list_account_positions(self, broker_account_name: str):
            self.calls.append(broker_account_name)
            return []

    store, adapter = _Store(), _Flat()
    _run(_svc(store, adapter))
    synced = {aid: snaps for aid, snaps in store.synced}
    assert synced.get(A_ID) == [] and synced.get(B_ID) == [], (
        "a genuinely flat account must still reach sync_account_positions with an empty list"
    )
    assert sorted(store.cleared_scope) == sorted([A_ID, B_ID])
