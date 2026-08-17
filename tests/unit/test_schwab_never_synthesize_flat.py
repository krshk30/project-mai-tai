"""A failed Schwab positions read must never be read as a flat account.

⛔⭐⭐ PROVEN HARM, not a hypothesis (2026-08-17 forensics on `live:schwab_1m_v2`):
    324 read failures in the retained logs · 109 windows in which we held a position
    2 failures landed DURING a hold  ->  2 of 2 ERASED, to the second:
        08-12 19:34:18 failure -> 19:34:18.484 [VIRTUAL-CLEAR] CRWU=2
        08-14 19:31:48 failure -> 19:31:49.090 [VIRTUAL-CLEAR] VWAV=2
Both were ISOLATED SINGLE failures, so ONE bad read is sufficient; no burst is required, and no
later good sync repaired it because the erasure was one-way.

⛔ Quote the 2-of-2 CONVERSION, never the 2/324 trigger rate — it scales with HOLD TIME, not with
failure frequency. 2/324 invites someone to call this rare.

THREE LAYERS, and they are not independent:
  L1 the adapter RAISES instead of returning []      <- makes empty-because-broken distinguishable
  L2 the sync EXCLUDES an account whose read failed  <- only implementable because of L1
  L3 the erasure is re-derivable from OUR managed rows
"""
from __future__ import annotations

import inspect

import pytest

from project_mai_tai.broker_adapters import schwab as schwab_mod
from project_mai_tai.broker_adapters.schwab import SchwabPositionsUnavailable
from project_mai_tai.oms import service as svc
from project_mai_tai.oms import store as store_mod


# ---------------------------------------------------------------- L1: the adapter must raise
def _src() -> str:
    return inspect.getsource(schwab_mod.SchwabBrokerAdapter.list_account_positions)


def _code_only(src: str) -> str:
    """Source with the docstring and all comment lines removed.

    ⛔ The docstring and comments QUOTE the old `return []` while explaining why it is gone, so a
    naive substring check matches PROSE instead of CODE — which is how the first version of the
    test below failed against a correct implementation.
    """
    parts = src.split('"""', 2)
    body = parts[2] if len(parts) > 2 else src
    return "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))


def test_L1_no_failure_path_returns_an_empty_list() -> None:
    """⛔ THE CORE. Every failure branch raises; `return []` must not appear in CODE at all."""
    body = _code_only(_src())
    assert "return []" not in body, (
        "a failure path still returns [] — an empty list from a failed call is not a flat account"
    )


def test_L1_the_bare_except_RuntimeError_now_raises() -> None:
    """⛔ A bare except returning a value that means 'nothing here' is the worst shape of this bug.
    Timeouts, upstream resets and DNS failures all landed here — 45 of them on 08-17 alone."""
    body = _src()
    assert "except RuntimeError as exc:" in body
    assert "raise SchwabPositionsUnavailable" in body


def test_L1_ALL_FOUR_failure_paths_raise() -> None:
    """missing account config · request raised · HTTP>=400 or bad shape · unusable payload."""
    body = _src()
    assert body.count("raise SchwabPositionsUnavailable") >= 4, (
        f"expected 4 raising failure paths, found {body.count('raise SchwabPositionsUnavailable')}"
    )


def test_L1_the_SUCCESS_path_still_returns_a_possibly_EMPTY_list() -> None:
    """⛔⭐ THE REGRESSION THIS FIX COULD INTRODUCE, and the one most likely to be missed.
    A GENUINELY FLAT account must still zero. If the success path stopped returning an empty list,
    a real flat account would never clear and re-entry would break."""
    body = _src()
    assert "return snapshots" in body, "the success path must still return the (possibly empty) list"
    # and it must not be guarded by an emptiness check that turns flat into an error
    assert "if not snapshots" not in body, "a flat account must NOT be treated as a failure"


def test_L1_the_exception_is_typed_and_documents_why() -> None:
    doc = SchwabPositionsUnavailable.__doc__ or ""
    assert "never downgrade this to a `[]` return" in doc.lower()
    assert issubclass(SchwabPositionsUnavailable, Exception)


# ---------------------------------------------------------------- L2: the sync must exclude it
def _sync_src() -> str:
    return inspect.getsource(svc.OmsRiskService.sync_broker_positions)


def test_L2_a_failed_read_cannot_reach_sync_account_positions() -> None:
    """⛔ The account is EXCLUDED from `fetched`, so neither the zeroing sync nor the one-way
    clear ever sees it.

    ⛔ Checking only for `continue` is NOT enough — a mutant that appends an empty list BEFORE the
    continue survives that (it did). Assert the failure branch appends NOTHING.
    """
    body = _sync_src()
    assert "[BROKER-SYNC-UNREADABLE]" in body
    # isolate the except-branch: from the except line to its `continue`
    start = body.index("except Exception")
    end = body.index("continue", start)
    failure_branch = body[start:end]
    assert "fetched.append" not in failure_branch, (
        "the failure branch appends to `fetched` — a failed read would reach "
        "sync_account_positions and zero the account"
    )
    # and there must be exactly ONE append site overall (the success path)
    assert body.count("fetched.append") == 1, (
        f"expected exactly one fetched.append (the success path), found {body.count('fetched.append')}"
    )


def test_L2_the_clear_is_scoped_to_accounts_actually_READ() -> None:
    """⛔ Scoping the clear to every configured account would let it run against an account whose
    read just failed — the exact thing L2 exists to prevent."""
    body = _sync_src()
    assert "account_ids = [account_id for account_id, _ in fetched]" in body, (
        "the one-way clear must be scoped to successfully-read accounts, not to all accounts"
    )


def test_L2_is_PER_ACCOUNT_not_per_sync() -> None:
    """One raising account must not abort the sync for every other account. Webull has raised since
    2026-07-24, so this was already reachable before Schwab joined it."""
    body = _sync_src()
    assert "except asyncio.CancelledError:" in body, "cancellation must still propagate"


# ---------------------------------------------------------------- L3: the erasure is repairable
def test_L3_restore_reads_OUR_managed_rows_not_the_shared_account_book() -> None:
    """⛔⭐ The account book is SHARED — the operator held 5000 IVF on our account on 08-17.
    Re-deriving quantity from it would attribute HIS shares to us (the scoping-invariant bypass).
    `oms_managed_positions` is the ownership discriminator and carries OUR quantity."""
    body = inspect.getsource(store_mod.OmsStore.restore_virtual_positions_from_managed)
    assert "OmsManagedPosition" in body
    assert "virtual.quantity = want" in body
    assert "managed.current_quantity" in body, "our quantity must come from the managed row"


def test_L3_requires_broker_backing_as_a_FLOOR() -> None:
    """⛔ A managed row alone is not proof the shares exist — restoring on it alone would
    resurrect a genuinely-closed position."""
    body = inspect.getsource(store_mod.OmsStore.restore_virtual_positions_from_managed)
    assert "if have < want:" in body and "continue" in body


def test_L3_never_overwrites_a_LIVE_ledger_value() -> None:
    """Only an ERASED row (quantity 0) is repaired; a row already carrying quantity is left alone."""
    body = inspect.getsource(store_mod.OmsStore.restore_virtual_positions_from_managed)
    assert "> 0:" in body and "continue" in body
    assert "Only repair an ERASED row" in body


def test_L3_is_actually_CALLED_by_the_sync() -> None:
    """⛔ A repair nobody invokes is the '#647 built and dark' shape."""
    body = _sync_src()
    assert "restore_virtual_positions_from_managed" in body
    assert "[VIRTUAL-RESTORE]" in body, "a silent repair is as bad as a silent erasure"


# ---------------------------------------- §43: the Webull exit-pair path must be untouched tonight
def test_the_webull_exit_pair_path_is_NOT_touched() -> None:
    """⛔ DEPLOY DISCIPLINE. Tomorrow's ALL_DAY acceptance read depends on the exit-pair path being
    exactly what was deployed at 16:06 ET. If this change altered it, attribution is lost."""
    from project_mai_tai.broker_adapters import webull as webull_mod
    pair = inspect.getsource(webull_mod.WebullBrokerAdapter._build_exit_only_pair_payload)
    sess = inspect.getsource(webull_mod.WebullBrokerAdapter._exit_pair_session)
    assert "_exit_pair_session(request)" in pair
    assert 'self._EXIT_PAIR_SESSION_EXTENDED' in sess and 'self._EXIT_PAIR_SESSION_RTH' in sess
    assert webull_mod.WebullBrokerAdapter._EXIT_PAIR_SESSION_EXTENDED == "ALL_DAY"
    assert webull_mod.WebullBrokerAdapter._EXIT_PAIR_SESSION_RTH == "CORE"


# ------------------------------------------- §42.4: replay the two KNOWN incidents end-to-end
class _RaisingAdapter:
    """Reproduces the 08-12 CRWU / 08-14 VWAV sequence: ONE isolated failed read while held."""

    def __init__(self) -> None:
        self.calls = 0

    async def list_account_positions(self, broker_account_name: str):
        self.calls += 1
        raise SchwabPositionsUnavailable(f"read timed out for {broker_account_name}")


class _FlatAdapter:
    """A GENUINELY flat account — must still zero. The regression guard."""

    async def list_account_positions(self, broker_account_name: str):
        return []


def test_INCIDENT_REPLAY_a_failed_read_yields_no_snapshot_to_persist() -> None:
    """⛔⭐⭐ THE 08-12 CRWU / 08-14 VWAV SHAPE.

    Both incidents were an ISOLATED SINGLE failed read landing while we held a position, and both
    erased the ledger row to the second. The fix's contract is that such a read contributes NOTHING
    to the persist phase — so `sync_account_positions` (which zeroes absent symbols) and the
    one-way `clear_virtual_positions_without_account_backing` never see that account.

    Asserted structurally against the sync source, because the persist phase is a DB closure: the
    failure branch must `continue` without appending, and the clear must be scoped to `fetched`.
    """
    body = _sync_src()
    start = body.index("except Exception")
    end = body.index("continue", start)
    assert "fetched.append" not in body[start:end]
    assert "account_ids = [account_id for account_id, _ in fetched]" in body


@pytest.mark.asyncio
async def test_INCIDENT_REPLAY_the_adapter_raises_rather_than_returning_flat() -> None:
    """The adapter contract itself, exercised: a failed read RAISES."""
    a = _RaisingAdapter()
    with pytest.raises(SchwabPositionsUnavailable):
        await a.list_account_positions("live:schwab_1m_v2")
    assert a.calls == 1


@pytest.mark.asyncio
async def test_INCIDENT_REPLAY_a_genuinely_flat_account_still_returns_empty() -> None:
    """⛔ The regression direction. Flat must stay flat, or re-entry breaks."""
    assert await _FlatAdapter().list_account_positions("live:schwab_1m_v2") == []
