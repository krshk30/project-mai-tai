"""Card-only boot/restart/source integration; private SQLite/native responses.

Uses actual publisher, packet codec, consumer and durable local census. Not
native broker coverage, PostgreSQL cost or historical capture completeness.
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import logging
from types import SimpleNamespace

import pytest

from project_mai_tai.brokerdecides1 import NativeAccountBook, decode, encode
from project_mai_tai.brokerdecides1_entry import entry_decision, initialize, reconstructed_first_admitted
from project_mai_tai.brokerdecides1_sync import begin_pass, finish_pass, refresh_consumer
from project_mai_tai.db.models import Fill
from project_mai_tai.fanout_segment_store import current_session_anchor
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from tests.line_restore_acceptance_factory import make_line_restore_case
from tests.unit.test_brokerdecides1_card_only import card_case
from tests.unit.test_brokerdecides1_local_census import ACCOUNTS, attempt, census, order


class PrivatePacketTransport:
    def __init__(self):
        self.raw = None
        self.writes = []

    async def set(self, key, raw, **kwargs):
        self.raw = raw
        self.writes.append(decode(raw))

    async def get(self, key):
        return self.raw


def controlled_service(monkeypatch, census, *, failure=None):
    from project_mai_tai import brokerdecides1_sync as module

    session, _, _ = census
    session.commit()
    observed = int(datetime.now(UTC).timestamp() * 1000)
    short = observed // 60_000 * 60_000
    strategy, state = card_case()
    strategy._now_ms = lambda: observed
    state.atr_short_flip_bar_ts = short
    state.cw_seed_cap_watch_start_ms = observed
    state.bars = [SimpleNamespace(timestamp_ms=short)]
    initialize(strategy)
    monkeypatch.setattr(module, "now_ms", lambda: observed)
    monkeypatch.setattr(module, "current_session_anchor", lambda:
        current_session_anchor(datetime.fromtimestamp(observed / 1000, UTC)))
    stages = []

    async def native(account):
        stages.append(("native", account))
        if failure == "unreachable" and account == ACCOUNTS[1]:
            raise ConnectionError("CONTROLLED account unavailable")
        return NativeAccountBook(account, "schwab" if account == ACCOUNTS[0] else "webull",
            observed, observed, frozenset(),
            frozenset({state.symbol}) if failure == "manual_position" and account == ACCOUNTS[1] else frozenset(),
            not (failure == "partial" and account == ACCOUNTS[1]), "CONTROLLED_NATIVE_RESPONSE")

    async def run_db(function, *, commit):
        stages.append(("db", commit))
        result = function(session)
        if commit:
            session.commit()
        else:
            session.rollback()
        return result

    producer = SimpleNamespace(settings=strategy.settings, _run_db=run_db,
        broker_adapter=SimpleNamespace(read_native_entry_book=native),
        redis=PrivatePacketTransport(), logger=logging.getLogger(__name__))
    consumer = SimpleNamespace(settings=strategy.settings, strategy=strategy, redis=producer.redis)
    return producer, consumer, state, observed, stages


async def publish_complete(producer, consumer):
    generation = await begin_pass(producer)
    await finish_pass(producer, generation, sync_succeeded=True)
    await refresh_consumer(consumer)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, False])
async def test_actual_boot_initialization_precedes_identity_stores_and_emitters(monkeypatch, flag):
    from project_mai_tai.services import schwab_1m_v2_bot as module

    now = int(datetime.now(UTC).timestamp() * 1000)
    overrides = {
        "strategy_schwab_1m_v2_enabled": True,
        "strategy_schwab_1m_v2_brokerdecides1_enabled": flag,
    }
    if flag:
        # Actual paired97ffc965 constructor context; not an ownership-ON boot claim.
        overrides.update(strategy_schwab_1m_v2_flip_owned_first_entry_enabled=False,
            strategy_schwab_1m_v2_retry_one_enabled=False)
    case = make_line_restore_case(symbol="WORX", now_ms=now, settings_overrides=overrides)
    case.bot.session_factory = object()
    transport = PrivatePacketTransport()
    monkeypatch.setattr(module.Redis, "from_url", lambda *args, **kwargs: transport)
    monkeypatch.setattr(case.bot, "_rpg_retire_disabled_at_boot", lambda: None)
    markers = []

    class StopAtStore(Exception):
        pass

    def identity_store():
        memory = getattr(case.strategy, "_brokerdecides1_memory", None)
        assert (memory is not None) is flag
        if flag:
            assert memory.snapshot is None and not memory.reservation_tokens()
            # Literal OLD9c paired97ffc965 returned TRUE on this real legacy path.
            assert case.strategy._strict_first_rest_admitted(
                case.strategy._symbol_states["WORX"], slot="first") is True
            decision = entry_decision(case.strategy, "WORX")
            assert decision.allowed and decision.reason == "outside_L12_exception"
        markers.append("initialized_before_identity_store")
        raise StopAtStore()

    monkeypatch.setattr(case.bot, "_configure_fanout_identity_store", identity_store)
    with pytest.raises(StopAtStore):
        await SchwabV2BotService.run(case.bot)
    assert markers == ["initialized_before_identity_store"]
    assert not case.schwab.intents and not case.webull.intents


@pytest.mark.asyncio
async def test_cold_boot_unknown_then_actual_complete_publisher_consumer_card_admits(monkeypatch, census):
    producer, consumer, state, observed, stages = controlled_service(monkeypatch, census)
    assert not entry_decision(consumer.strategy, state.symbol).allowed
    await publish_complete(producer, consumer)
    assert [value for kind, value in stages if kind == "native"] == list(ACCOUNTS)
    assert entry_decision(consumer.strategy, state.symbol).allowed
    assert reconstructed_first_admitted(consumer.strategy, state)
    assert state.retry_one_segment_id == state.retry_one_closes_in_segment == 0
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert len(producer.redis.writes) == 2 and not producer.redis.writes[0].local_complete


@pytest.mark.asyncio
async def test_restart_recovers_durable_unknown_send_without_relying_on_lost_ram_reservation(monkeypatch, census):
    session, _, _ = census
    old = attempt(session)
    session.commit()
    producer, consumer, state, observed, stages = controlled_service(monkeypatch, census)
    assert not consumer.strategy._brokerdecides1_memory.reservation_tokens()
    await publish_complete(producer, consumer)
    snapshot = consumer.strategy._brokerdecides1_memory.snapshot
    assert snapshot.local_complete and state.symbol in snapshot.inflight
    assert not entry_decision(consumer.strategy, state.symbol).allowed
    assert old.payload["attempt_token"] == "persisted-attempt"
    assert old.payload["kind"] == "attempt"


@pytest.mark.asyncio
async def test_new_in_progress_epoch_revokes_old_flat_without_clearing_unknown_ack(monkeypatch, census):
    producer, consumer, state, observed, stages = controlled_service(monkeypatch, census)
    await publish_complete(producer, consumer)
    memory = consumer.strategy._brokerdecides1_memory
    memory.reserve(state.symbol, "unknown-ACK")
    producer._brokerdecides1_publisher = None
    await begin_pass(producer)
    await refresh_consumer(consumer)
    assert memory.snapshot is not None and not memory.snapshot.local_complete
    assert memory.reservation_tokens() == frozenset({"unknown-ACK"})
    assert not entry_decision(consumer.strategy, state.symbol).allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unreachable", "partial", "manual_position"])
async def test_one_account_unknown_or_manual_position_cannot_restart_flat(monkeypatch, census, failure):
    producer, consumer, state, observed, stages = controlled_service(monkeypatch, census, failure=failure)
    await publish_complete(producer, consumer)
    assert not entry_decision(consumer.strategy, state.symbol).allowed
    assert state.retry_one_segment_id == 0 and state.cw_resting_taken


@pytest.mark.asyncio
async def test_corrupt_transport_revokes_flat_but_preserves_send_overlay(monkeypatch, census):
    producer, consumer, state, observed, stages = controlled_service(monkeypatch, census)
    await publish_complete(producer, consumer)
    memory = consumer.strategy._brokerdecides1_memory
    memory.reserve(state.symbol, "persisted-unknown")
    producer.redis.raw = '{"schema":3,"schema":3}'
    await refresh_consumer(consumer)
    assert memory.snapshot is None
    assert memory.reservation_tokens() == frozenset({"persisted-unknown"})
    assert not entry_decision(consumer.strategy, state.symbol).allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["lower_generation", "changed_generation", "old_epoch"])
async def test_stale_or_changed_restart_packet_never_reopens_entry(monkeypatch, census, change):
    producer, consumer, state, observed, stages = controlled_service(monkeypatch, census)
    await publish_complete(producer, consumer)
    await publish_complete(producer, consumer)
    snapshot = consumer.strategy._brokerdecides1_memory.snapshot
    changes = {"generation": snapshot.generation - 1} if change == "lower_generation" else (
        {"reason": "changed_complete_generation"} if change == "changed_generation" else
        {"epoch": "older-process", "started_ms": observed - 1000})
    producer.redis.raw = encode(replace(snapshot, **changes))
    await refresh_consumer(consumer)
    assert consumer.strategy._brokerdecides1_memory.snapshot is None
    assert not entry_decision(consumer.strategy, state.symbol).allowed


@pytest.mark.asyncio
async def test_next_real_durable_fill_census_blocks_same_segment_after_restart(monkeypatch, census):
    session, accounts, strategy_row = census
    producer, consumer, state, observed, stages = controlled_service(monkeypatch, census)
    await publish_complete(producer, consumer)
    assert entry_decision(consumer.strategy, state.symbol).allowed
    child = order(session, accounts, strategy_row, line=str(state.atr_short_flip_bar_ts))
    session.add(Fill(order_id=child.id, strategy_id=strategy_row.id, broker_account_id=accounts[0].id,
        symbol=state.symbol, side="buy", quantity=Decimal("1"), price=Decimal("3"),
        filled_at=datetime.fromtimestamp(observed / 1000, UTC)))
    session.commit()
    initialize(consumer.strategy)
    assert not entry_decision(consumer.strategy, state.symbol).allowed
    await publish_complete(producer, consumer)
    snapshot = consumer.strategy._brokerdecides1_memory.snapshot
    assert (state.symbol, state.atr_short_flip_bar_ts) in snapshot.filled_segments
    assert not entry_decision(consumer.strategy, state.symbol).allowed


@pytest.mark.asyncio
async def test_repeated_hot_admission_reads_add_no_db_or_native_operations(monkeypatch, census):
    producer, consumer, state, observed, stages = controlled_service(monkeypatch, census)
    await publish_complete(producer, consumer)
    before = tuple(stages)
    for _ in range(3):
        assert entry_decision(consumer.strategy, state.symbol).allowed
    assert tuple(stages) == before  # Structural call-boundary assertion, NOT a rate test.


@pytest.mark.asyncio
async def test_stale_after_complete_boot_still_holds_without_new_io(monkeypatch, census):
    producer, consumer, state, observed, stages = controlled_service(monkeypatch, census)
    await publish_complete(producer, consumer)
    before = tuple(stages)
    consumer.strategy._now_ms = lambda: observed + 25_000
    assert not entry_decision(consumer.strategy, state.symbol).allowed
    assert not reconstructed_first_admitted(consumer.strategy, state)
    assert tuple(stages) == before and state.retry_one_segment_id == 0
