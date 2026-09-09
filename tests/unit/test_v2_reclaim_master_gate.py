from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import Base, DashboardSnapshot
from project_mai_tai.fanout_outcome_consumer import FanoutOutcome
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.settings import Settings
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    Quote,
    SchwabV2Strategy,
)
from project_mai_tai.v2_segment_consumption_store import V2SegmentConsumptionStore


NOW = datetime(2026, 9, 9, 15, 0, tzinfo=UTC)
SEGMENT = int(NOW.timestamp() * 1000)


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[DashboardSnapshot.__table__])
    return sessionmaker(bind=engine, expire_on_commit=False)


def _strategy(*, reclaim: bool = False) -> SchwabV2Strategy:
    strategy = SchwabV2Strategy(
        Settings(
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
            strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
            strategy_schwab_1m_v2_cw_v2_reactive_entry_enabled=True,
            strategy_schwab_1m_v2_cw_v2_reclaim_enabled=reclaim,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
            strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
        )
    )
    strategy._entries_held = False
    strategy._now_ms = lambda: SEGMENT
    strategy._resting_in_window = lambda now=None: True
    strategy._resting_session_is_eh = lambda now=None: False
    strategy._liquidity_floor_ok = lambda state: True
    return strategy


def _bar(high: float = 5.0) -> OHLCVBar:
    return OHLCVBar(
        timestamp_ms=SEGMENT,
        open=high - 0.1,
        high=high,
        low=high - 0.2,
        close=high - 0.05,
        volume=25_000,
    )


def _filled(metadata: dict[str, str]) -> FanoutOutcome:
    return FanoutOutcome(
        record_id=uuid4(),
        created_at=NOW,
        symbol="FTFT",
        segment_id=int(metadata["fanout_segment_id"]),
        slot=metadata["fanout_slot"],
        slot_id=metadata["fanout_slot_id"],
        attempt_id=metadata.get("fanout_attempt_id", ""),
        outcome="filled",
        evidence_id=str(uuid4()),
    )


def test_reclaim_off_disables_both_reclaim_producers() -> None:
    strategy = _strategy()
    state = strategy.watchlist_state("FTFT")
    state.bars.append(_bar(5.0))
    state.cw_armed = True
    state.cw_bars_waited = 2
    state.cw_segment_high = 5.0
    state.cw_flip_level = 4.0

    quote = Quote("FTFT", 5.09, 5.11, 5.10, SEGMENT, 0)
    assert strategy.on_quote("FTFT", quote) is None
    strategy._resting_stop_ask_allows = lambda *_args, **_kwargs: True
    strategy._cw_v2_reclaim_resting_track(state)
    assert strategy.drain_pending_intents() == []


def test_reclaim_off_cancels_an_existing_reclaim_order_through_managed_path() -> None:
    strategy = _strategy()
    state = strategy.watchlist_state("FTFT")
    state.resting_active = True
    state.resting_slot = "reclaim"
    state.resting_level = 2.50
    state.resting_is_broker_order = True

    strategy._cw_v2_reclaim_resting_track(state)

    cancels = strategy.drain_pending_intents()
    assert len(cancels) == 1
    assert cancels[0].intent_type == "cancel"
    assert cancels[0].metadata["reason"] == "reclaim_disabled"


def test_webull_fill_consumes_the_shared_segment_without_cancelling_its_sibling() -> None:
    strategy = _strategy()
    writes: list[tuple[str, int, str, str]] = []
    strategy.configure_segment_consumption_persistence(
        lambda symbol, segment_id, slot_id, reason: writes.append(
            (symbol, segment_id, slot_id, reason)
        )
    )
    state = strategy.watchlist_state("FTFT")
    state.bars.append(_bar(2.31))
    strategy._queue_resting_place(state, 2.31, slot="first")
    primary = strategy.drain_pending_intents()
    mirror = strategy.drain_webull_direct_intents()
    assert len(primary) == len(mirror) == 1

    assert strategy.apply_fanout_outcome(_filled(mirror[0].metadata)) == "consumed"
    assert state.cw_segment_consumed is True
    assert len(writes) == 1

    strategy._cw_v2_resting_track(
        state,
        {"state": "short", "trail": 2.31, "state_age": 5, "flip": None},
    )
    assert strategy.drain_pending_intents() == []
    assert state.resting_active is True

    strategy.update_position("FTFT", 2, held_qty=2)
    assert len(writes) == 1
    strategy.update_position("FTFT", 0, held_qty=0)
    strategy._queue_resting_place(state, 2.30, slot="first")
    assert strategy.drain_pending_intents() == []


def test_position_close_does_not_reopen_a_consumed_segment() -> None:
    strategy = _strategy()
    state = strategy.watchlist_state("FTFT")
    state.bars.append(_bar(2.31))
    strategy._queue_resting_place(state, 2.31, slot="first")
    strategy.drain_pending_intents()

    strategy.update_position("FTFT", 2, held_qty=2)
    state.resting_active = False
    strategy.update_position("FTFT", 0, held_qty=0)

    assert state.cw_segment_consumed is True
    strategy._queue_resting_place(state, 2.30, slot="first")
    assert strategy.drain_pending_intents() == []


def test_fresh_segment_reset_reopens_only_the_first_resting_path() -> None:
    strategy = _strategy()
    state = strategy.watchlist_state("FTFT")
    state.bars.append(_bar(2.31))
    state.cw_armed = True
    strategy._queue_resting_place(state, 2.31, slot="first")
    strategy.drain_pending_intents()
    strategy.update_position("FTFT", 2, held_qty=2)
    assert state.cw_segment_consumed is True

    assert strategy._release_arm(state, "sell_flip") is True
    assert state.cw_segment_consumed is False
    state.bars[-1] = _bar(2.20)
    strategy._queue_resting_place(state, 2.20, slot="first")
    drafts = strategy.drain_pending_intents()
    assert len(drafts) == 1 and drafts[0].metadata["cw_entry_slot"] == "first"


def test_consumed_segment_survives_restart_and_blocks_a_second_first_order() -> None:
    factory = _factory()
    identity_store = FanoutSegmentIdentityStore(factory)
    consumed_store = V2SegmentConsumptionStore(factory)

    original = _strategy()
    original.configure_fanout_identity_persistence(
        lambda symbol, segment_id, active, reason: identity_store.record(
            symbol, segment_id, active, reason, now=NOW
        ),
        {},
    )
    original.configure_segment_consumption_persistence(
        lambda symbol, segment_id, slot_id, reason: consumed_store.record_consumed(
            symbol,
            segment_id,
            slot_id,
            reason,
            now=NOW + timedelta(seconds=1),
        )
    )
    original_state = original.watchlist_state("FTFT")
    original_state.bars.append(_bar(2.31))
    original._queue_resting_place(original_state, 2.31, slot="first")
    original.drain_pending_intents()
    mirror = original.drain_webull_direct_intents()
    assert len(mirror) == 1
    assert original.apply_fanout_outcome(_filled(mirror[0].metadata)) == "consumed"

    strategy = _strategy()
    active = dict(identity_store.restore_active(now=NOW + timedelta(minutes=1)))
    consumed = dict(consumed_store.restore_consumed(active, now=NOW + timedelta(minutes=1)))
    strategy.configure_fanout_identity_persistence(None, active)
    strategy.configure_segment_consumption_persistence(
        None,
        active_segments=active,
        consumed_segments=consumed,
    )
    state = strategy.watchlist_state("FTFT")
    state.bars.append(_bar(2.31))

    strategy._queue_resting_place(state, 2.31, slot="first")

    assert state.cw_segment_consumed is True
    assert state.cw_segment_consumption_known is True
    assert strategy.drain_pending_intents() == []


def test_active_segment_without_consumption_record_fails_closed_after_restart() -> None:
    strategy = _strategy()
    strategy.configure_fanout_identity_persistence(None, {"FTFT": SEGMENT})
    strategy.configure_segment_consumption_persistence(
        None,
        active_segments={"FTFT": SEGMENT},
        consumed_segments={},
    )
    state = strategy.watchlist_state("FTFT")
    state.bars.append(_bar(2.31))

    strategy._queue_resting_place(state, 2.31, slot="first")

    assert state.cw_segment_consumed is True
    assert state.cw_segment_consumption_known is False
    assert strategy.drain_pending_intents() == []


def test_unreadable_restart_state_blocks_even_a_newly_reconstructed_segment() -> None:
    strategy = _strategy()
    strategy.configure_segment_consumption_persistence(None, restore_readable=False)
    state = strategy.watchlist_state("FTFT")
    state.bars.append(_bar(2.31))

    strategy._queue_resting_place(state, 2.31, slot="first")

    assert state.cw_segment_consumption_known is False
    assert strategy.drain_pending_intents() == []


def test_failed_identity_bind_blocks_the_first_resting_order() -> None:
    strategy = _strategy()

    def fail_identity(*_args) -> None:
        raise RuntimeError("forced identity-store failure")

    strategy.configure_fanout_identity_persistence(fail_identity)
    state = strategy.watchlist_state("FTFT")
    state.bars.append(_bar(2.31))

    strategy._queue_resting_place(state, 2.31, slot="first")

    assert state.cw_segment_consumption_known is False
    assert strategy.drain_pending_intents() == []


def test_bot_startup_restores_consumption_before_any_emitter_or_market_data() -> None:
    source = inspect.getsource(SchwabV2BotService.run)

    identity = source.index("self._configure_fanout_identity_store()")
    consumption = source.index("self._configure_segment_consumption_store(active_segments)")
    outcome = source.index("self._configure_fanout_outcome_journal(active_segments)")
    emitter = source.index("self.intent_emitter = SchwabV2IntentEmitter(")

    assert identity < consumption < outcome < emitter
