"""C1 16:12 real entry/publication methods; native operands are CONTROLLED."""

from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal

import pytest

from project_mai_tai.brokerdecides1_entry import (
    draft_uses_exception, entry_decision, exception_scope, prepare_send,
)
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    SchwabV2IntentEmitter, SchwabV2Strategy, SymbolState, TradeIntentDraft,
)
from tests.unit.test_brokerdecides1_entry_hooks import PrivateRedis
from tests.unit.test_brokerdecides1_reconstructed_first import cap_case
from tests.unit.test_brokerdecides1_unused_owner_sell import case as owner_case
from tests.unit.test_brokerdecides1_prewire_protocol import case as _journal_case

journal_case = _journal_case


class ForbiddenMemory:
    def assess(self, **kwargs):
        raise AssertionError("ordinary entry consulted L12")

    @property
    def snapshot(self):
        raise AssertionError("ordinary entry read L12 snapshot")

    def reserve(self, *args):
        raise AssertionError("ordinary entry reserved L12 memory")


def ordinary(*, enabled=True, memory="poison"):
    strategy = SchwabV2Strategy(Settings(_env_file=None,
        strategy_schwab_1m_v2_brokerdecides1_enabled=enabled,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=False,
        strategy_schwab_1m_v2_false_flip_enabled=False))
    state = SymbolState(symbol="WORX")
    strategy._symbol_states[state.symbol] = state
    strategy._brokerdecides1_memory = ForbiddenMemory() if memory == "poison" else None
    return strategy, state


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("memory", ["poison", "missing"])
def test_real_ordinary_first_rest_preserves_old_without_book(enabled, memory):
    strategy, state = ordinary(enabled=enabled, memory=memory)
    assert strategy._strict_first_rest_admitted(state, slot="first") is True
    assert not exception_scope(strategy, state)


@pytest.mark.parametrize("memory", ["poison", "missing"])
def test_real_ordinary_prepare_send_does_not_stamp_or_reserve(memory):
    strategy, state = ordinary(memory=memory)
    draft = TradeIntentDraft(state.symbol, "buy", "open", Decimal("1"), "ATR Flip", {})
    prepare_send(strategy, draft, "legacy-account")
    assert draft.metadata == {}
    assert entry_decision(strategy, state.symbol).allowed


@pytest.mark.parametrize("fault", ["missing", "stale", "unknown"])
def test_real_ordinary_idle_owner_ON_matches_OFF_despite_unavailable_native(fault):
    strategy, state, _ = owner_case(phase="idle")
    # Explicit CONTROLLED legacy configuration; owner evidence remains the real
    # fresh state populated by owner_case, not a native-book-derived permission.
    strategy._retry_one_enabled = False
    strategy._removed_wait_enabled = False
    if fault == "missing":
        strategy._brokerdecides1_memory = None
    else:
        memory = strategy._brokerdecides1_memory
        memory.apply(replace(memory.snapshot, generation=2,
            local_complete=fault != "unknown", started_ms=1, completed_ms=2))
    assert not exception_scope(strategy, state)
    assert strategy._strict_first_rest_admitted(state, slot="first") is True
    strategy.settings.strategy_schwab_1m_v2_brokerdecides1_enabled = False
    assert strategy._strict_first_rest_admitted(state, slot="first") is True


def test_real_service_ordinary_boundary_needs_no_journal_adapter():
    strategy, state = ordinary()
    service = SchwabV2BotService.__new__(SchwabV2BotService)
    service.strategy = strategy
    draft = TradeIntentDraft(state.symbol, "buy", "open", Decimal("1"), "ATR Flip", {})
    assert service._brokerdecides1_publication_boundary(draft, "legacy-account") is None
    service._brokerdecides1_before_entry_publish(draft, "legacy-account")
    assert draft.metadata == {}
    assert "_brokerdecides1_publication_callbacks" not in service.__dict__


@pytest.mark.asyncio
async def test_actual_emitter_service_hooks_ordinary_buy_missing_book():
    strategy, state = ordinary(memory="missing")
    service = SchwabV2BotService.__new__(SchwabV2BotService)
    service.strategy = strategy
    draft = TradeIntentDraft(state.symbol, "buy", "open", Decimal("1"), "ATR Flip", {})
    redis = PrivateRedis()
    await SchwabV2IntentEmitter(strategy.settings, redis, "legacy-account",
        before_entry_publish=service._brokerdecides1_before_entry_publish,
        publication_boundary_factory=service._brokerdecides1_publication_boundary).emit(draft)
    assert len(redis.events) == 1
    assert not any(key.startswith("brokerdecides") for key in draft.metadata)


@pytest.mark.asyncio
async def test_exception_appearing_during_L11_await_refuses_before_wire():
    strategy, state = ordinary()
    service = SchwabV2BotService.__new__(SchwabV2BotService)
    service.strategy = strategy
    draft = TradeIntentDraft(state.symbol, "buy", "open", Decimal("1"), "ATR Flip", {})
    observed = []

    @asynccontextmanager
    async def guard(value):
        observed.append(value)
        strategy._flip_owned_first_entry_enabled = True
        state.flip_owner_phase = "unknown"
        state.flip_owner_opportunity_id = state.fanout_segment_id = 100
        yield lambda: True

    redis = PrivateRedis()
    emitter = SchwabV2IntentEmitter(strategy.settings, redis, "legacy-account",
        publication_guard=guard,
        before_entry_publish=service._brokerdecides1_before_entry_publish,
        publication_boundary_factory=service._brokerdecides1_publication_boundary)
    with pytest.raises(ValueError, match="exception_fresh_journal_UNKNOWN"):
        await emitter.emit(draft)
    assert observed == [draft] and not redis.events
    assert draft.metadata == {}


def test_actual_cap_only_fresh_admits_without_erasing_old_notes():
    strategy, state = cap_case()
    assert SchwabV2Strategy._strict_first_rest_admitted(strategy, state, slot="first")
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert state.cw_seed_cap_watch_start_ms == 950
    assert not hasattr(state, "_brokerdecides1_exception_segment")


def test_actual_cap_clear_in_same_segment_returns_to_old_admission_without_book():
    strategy, state = cap_case()
    segment = state.atr_short_flip_bar_ts
    assert SchwabV2Strategy._strict_first_rest_admitted(strategy, state, slot="first")
    SchwabV2Strategy._clear_cw_slot_claims(state)
    strategy._brokerdecides1_memory = ForbiddenMemory()
    assert state.atr_short_flip_bar_ts == segment and not exception_scope(strategy, state)
    assert SchwabV2Strategy._strict_first_rest_admitted(strategy, state, slot="first")
    draft = TradeIntentDraft(state.symbol, "buy", "open", Decimal("1"), "ATR Flip", {})
    prepare_send(strategy, draft, "legacy-account")
    assert draft.metadata == {}


@pytest.mark.parametrize("fault", ["missing", "stale", "unknown", "working", "position", "inflight", "fill"])
def test_actual_exception_still_refuses_unknown_or_economic_claim(fault):
    strategy, state = cap_case()
    memory = strategy._brokerdecides1_memory
    snapshot = memory.snapshot
    if fault == "missing":
        strategy._brokerdecides1_memory = None
    elif fault == "stale":
        strategy._now_ms = lambda: 21001
    else:
        change = {"generation": 2}
        if fault == "unknown":
            change["local_complete"] = False
        elif fault in {"working", "position"}:
            field = "working" if fault == "working" else "positions"
            change["books"] = (replace(snapshot.books[0], **{field: frozenset({state.symbol})}), snapshot.books[1])
        elif fault == "inflight":
            change["inflight"] = frozenset({state.symbol})
        else:
            change["filled_segments"] = frozenset({(state.symbol, state.atr_short_flip_bar_ts)})
        memory.apply(replace(snapshot, **change))
    assert not SchwabV2Strategy._strict_first_rest_admitted(strategy, state, slot="first")


def test_real_stale_owner_retirement_restores_ordinary_same_segment_OLD_parity():
    strategy, state, writes = owner_case(phase="unknown")
    segment = state.atr_short_flip_bar_ts
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "idle"
    assert len(writes) == 2 and all(not active for _, _, active, _ in writes)
    assert state.atr_short_flip_bar_ts == segment
    assert not exception_scope(strategy, state)
    strategy._retry_one_enabled = False  # Explicit CONTROLLED legacy config.
    strategy._removed_wait_enabled = False
    strategy._brokerdecides1_memory = ForbiddenMemory()
    assert strategy._strict_first_rest_admitted(state, slot="first") is True
    draft = TradeIntentDraft(state.symbol, "buy", "open", Decimal("1"), "ATR Flip",
        {"cw_entry_slot": "reclaim"})
    prepare_send(strategy, draft, "legacy-account")
    assert draft.metadata == {"cw_entry_slot": "reclaim"}
    strategy.settings.strategy_schwab_1m_v2_brokerdecides1_enabled = False
    assert strategy._strict_first_rest_admitted(state, slot="first") is True


def test_real_stale_owner_missing_book_retains_old_identity():
    strategy, state, writes = owner_case(phase="unknown")
    state.flip_owner_evidence_at_ms = 0  # OLD itself must hold this stale owner.
    opportunity = state.flip_owner_opportunity_id
    strategy._brokerdecides1_memory = None
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_opportunity_id == opportunity
    assert not any(not active for _, _, active, _ in writes)


def test_retirement_leaves_neither_same_nor_next_segment_scope():
    strategy, state, _ = owner_case(phase="unknown")
    strategy._end_flip_owner_on_sell(state)
    assert not exception_scope(strategy, state)
    state.atr_short_flip_bar_ts += 60000
    strategy._brokerdecides1_memory = ForbiddenMemory()
    assert not exception_scope(strategy, state)
    assert entry_decision(strategy, state.symbol).allowed


@pytest.mark.parametrize("fault", ["missing", "stale", "working"])
def test_already_journaled_draft_keeps_final_recheck_after_current_blocker_clears(journal_case, fault):
    from tests.unit.test_brokerdecides1_publication_service_callbacks import service_case

    service, draft = service_case(journal_case)
    boundary = service._brokerdecides1_publication_boundary(draft, "live:schwab")
    assert boundary.phase == "BEFORE_XADD"
    # CONTROLLED async-state settlement: no new source/historical claim.
    service.strategy._symbol_states["WORX"].flip_owner_phase = "idle"
    assert not exception_scope(service.strategy, service.strategy._symbol_states["WORX"])
    assert draft_uses_exception(service.strategy, draft)
    if fault == "missing":
        service.strategy._brokerdecides1_memory = None
    elif fault == "stale":
        service.strategy._now_ms = lambda: 21001
    else:
        memory = service.strategy._brokerdecides1_memory
        memory.apply(replace(memory.snapshot, generation=2,
            books=(replace(memory.snapshot.books[0], working=frozenset({"WORX"})), memory.snapshot.books[1])))
    with pytest.raises(ValueError, match="brokerdecides1_hold"):
        service._brokerdecides1_before_entry_publish(draft, "live:schwab")
    assert boundary.phase == "BEFORE_XADD"
    unrelated = TradeIntentDraft("WORX", "buy", "open", Decimal("1"), "ATR Flip", {})
    assert not draft_uses_exception(service.strategy, unrelated)
    service._brokerdecides1_before_entry_publish(unrelated, "live:schwab")
    assert unrelated.metadata == {}


def test_ordinary_dispatch_keeps_legacy_payload_without_fresh_authority():
    strategy, state = ordinary()
    state.fanout_segment_id = state.flip_owner_opportunity_id = 100
    records = []
    strategy.configure_removed_wait(None, restored={}, readable=True,
        dispatch_persist=lambda value: records.append(value))
    account = strategy._removed_wait_accounts()[0]
    draft = TradeIntentDraft(state.symbol, "buy", "open", Decimal("1"), "ATR Flip",
        {"fanout_segment_id": "100"})
    assert strategy.track_removed_wait_dispatch(draft, account)
    assert len(records) == 1 and "brokerdecides_exception" not in records[0]
    assert records[0]["kind"] == "attempt"
    assert draft.metadata["clearwait_dispatch_token"] == records[0]["attempt_token"]


def test_actual_enabled_store_ordinary_attempt_is_legacy_not_capability(journal_case):
    from sqlalchemy import select
    from project_mai_tai.db.models import DashboardSnapshot
    from project_mai_tai.v2_removed_wait import RemovedWaitStore

    payload = {"schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": "WORX",
        "opportunity_id": "100", "account_names": ["live:schwab", "live:webull"],
        "account_name": "live:schwab", "kind": "attempt", "attempt_token": "ordinary-no-exception"}
    assert RemovedWaitStore(journal_case.factory, prewire_authority=True).record_dispatch(payload) is None
    with journal_case.factory() as session:
        row = session.scalar(select(DashboardSnapshot).where(
            DashboardSnapshot.payload["attempt_token"].as_string() == "ordinary-no-exception"))
        assert row.payload == payload


def test_actual_exception_marker_is_not_persisted_as_new_authority_proof(journal_case):
    from project_mai_tai.db.models import DashboardSnapshot

    capability = journal_case.boundary._capability
    with journal_case.factory() as session:
        row = session.get(DashboardSnapshot, capability.attempt_id)
        assert row.payload["attempt_token"] == "persisted-attempt"
        assert row.payload["prewire_authority_nonce"] == capability.nonce
        assert "brokerdecides_exception" not in row.payload


def test_exception_dispatch_still_requires_fresh_authority_not_legacy_ack():
    strategy, state = ordinary()
    strategy._flip_owned_first_entry_enabled = True
    state.flip_owner_phase = "unknown"
    state.fanout_segment_id = state.flip_owner_opportunity_id = 100
    records = []
    strategy.configure_removed_wait(None, restored={}, readable=True,
        dispatch_persist=lambda value: records.append(value))
    draft = TradeIntentDraft(state.symbol, "buy", "open", Decimal("1"), "ATR Flip",
        {"fanout_segment_id": "100"})
    assert not strategy.track_removed_wait_dispatch(draft, strategy._removed_wait_accounts()[0])
    assert len(records) == 1 and records[0]["brokerdecides_exception"] is True
