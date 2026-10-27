"""Controls exercise actual strategy/emitter hooks with private injected evidence."""
import asyncio
from dataclasses import replace
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from project_mai_tai.brokerdecides1 import AdmissionMemory, NativeAccountBook, NativeBookPublisher
from project_mai_tai.brokerdecides1_entry import entry_decision, line_segment, prepare_send
from project_mai_tai.atr_country_route import AtrCountrySelection
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    SchwabV2IntentEmitter, SchwabV2Strategy, TradeIntentDraft,
)

ACCOUNTS = ("live:schwab", "live:webull")


def fixture(account=ACCOUNTS[0]):
    settings = SimpleNamespace(strategy_schwab_1m_v2_brokerdecides1_enabled=True,
        strategy_schwab_1m_v2_brokerdecides1_max_age_ms=500,
        strategy_schwab_1m_v2_account_name=ACCOUNTS[0],
        strategy_schwab_1m_v2_webull_account_name=ACCOUNTS[1],
        strategy_schwab_1m_v2_country_route_enabled=True,
        redis_stream_prefix="private_fixture", redis_strategy_intent_stream_maxlen=20)
    state = SimpleNamespace(symbol="WORX", slotclear_fresh_buy_bar_ms=0, bars=[],
        slotclear_fresh_buy_segment_id=0, atr_state="short", atr_short_flip_bar_ts=900,
        flip_owner_phase="unknown", fanout_segment_id=100, flip_owner_opportunity_id=100)
    selection = AtrCountrySelection("WORX", 100,
        "schwab" if account == ACCOUNTS[0] else "webull", account,
        "sec_us" if account == ACCOUNTS[0] else "sec_non_us", "READY",
        "1970-01-01T00:00:01+00:00", "1970-01-01T00:00:01.100000+00:00")
    state.atr_country_route = selection.metadata()
    strategy = SimpleNamespace(settings=settings, _symbol_states={"WORX": state},
        _flip_owned_first_entry_enabled=True,
        _now_ms=lambda: 1300, _removed_wait_dispatch_persist=lambda data: None,
        _brokerdecides1_memory=AdmissionMemory())
    strategy._country_single_entry = lambda s: SchwabV2Strategy._country_single_entry(strategy, s)
    strategy._country_existing_selection = lambda s: SchwabV2Strategy._country_existing_selection(strategy, s)
    publisher = NativeBookPublisher(epoch="fixture", accounts=ACCOUNTS)
    generation = publisher.begin(1000)
    value = publisher.finish(generation, completed_ms=1200, books=(
        NativeAccountBook(ACCOUNTS[0], "schwab", 1000, 1100, frozenset(), frozenset(), True, "fixture"),
        NativeAccountBook(ACCOUNTS[1], "webull", 1000, 1100, frozenset(), frozenset(), True, "fixture")),
        inflight=(), filled_segments=(), local_complete=True, reason="fixture")
    strategy._brokerdecides1_memory.apply(value)
    draft = TradeIntentDraft("WORX", "buy", "open", Decimal("1"), "ATR Flip", {
        "clearwait_dispatch_token": "persisted-attempt", "fanout_segment_id": "100", **selection.metadata()})
    return strategy, state, draft


class PrivateRedis:
    def __init__(self, *, fail=False):
        self.events = []
        self.fail = fail

    async def xadd(self, stream, data, **kwargs):
        self.events.append(json.loads(data["data"]))
        if self.fail:
            raise ConnectionError("unknown acknowledgement")
        return "1-0"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
async def test_actual_emitter_reserves_before_xadd_and_stamps_real_segment(account):
    strategy, state, draft = fixture(account)
    redis = PrivateRedis()
    emitter = SchwabV2IntentEmitter(strategy.settings, redis, account,
        before_entry_publish=lambda d, a: prepare_send(strategy, d, a))
    await emitter.emit(draft)
    md = redis.events[0]["payload"]["metadata"]
    assert md["brokerdecides_line_segment_ms"] == "900"
    assert md["fanout_segment_id"] == "100"
    assert not entry_decision(strategy, "WORX").allowed
    with pytest.raises(ValueError, match="local_send_reserved"):
        await emitter.emit(draft)
    assert len(redis.events) == 1


@pytest.mark.asyncio
async def test_unknown_xadd_ack_keeps_exact_reservation_no_retry():
    strategy, state, draft = fixture()
    redis = PrivateRedis(fail=True)
    emitter = SchwabV2IntentEmitter(strategy.settings, redis, ACCOUNTS[0],
        before_entry_publish=lambda d, a: prepare_send(strategy, d, a))
    with pytest.raises(ConnectionError):
        await emitter.emit(draft)
    assert not entry_decision(strategy, "WORX").allowed
    with pytest.raises(ValueError):
        await emitter.emit(draft)
    assert len(redis.events) == 1


@pytest.mark.asyncio
async def test_emit_cancellation_does_not_clear_reservation():
    strategy, state, draft = fixture()

    class CancelRedis:
        async def xadd(self, *args, **kwargs):
            raise asyncio.CancelledError()

    emitter = SchwabV2IntentEmitter(strategy.settings, CancelRedis(), ACCOUNTS[0],
        before_entry_publish=lambda d, a: prepare_send(strategy, d, a))
    with pytest.raises(asyncio.CancelledError):
        await emitter.emit(draft)
    assert not entry_decision(strategy, "WORX").allowed


@pytest.mark.parametrize("change", ["missing_memory", "unreviewed_age", "missing_segment", "stale"])
def test_any_admission_unknown_refuses_before_reservation(change):
    strategy, state, draft = fixture()
    if change == "missing_memory":
        strategy._brokerdecides1_memory = None
    elif change == "unreviewed_age":
        strategy.settings.strategy_schwab_1m_v2_brokerdecides1_max_age_ms = 0
    elif change == "missing_segment":
        state.atr_short_flip_bar_ts = 0
    else:
        strategy._now_ms = lambda: 1501
    with pytest.raises(ValueError, match="brokerdecides1_hold"):
        prepare_send(strategy, draft, ACCOUNTS[0])
    assert "brokerdecides_line_segment_ms" not in draft.metadata


def test_missing_committed_attempt_proof_refuses_send():
    strategy, state, draft = fixture()
    draft.metadata.pop("clearwait_dispatch_token")
    with pytest.raises(ValueError, match="dispatch_journal_UNKNOWN"):
        prepare_send(strategy, draft, ACCOUNTS[0])


def test_slotclear_uses_original_short_boundary_not_buy_bar_or_opportunity():
    strategy, state, draft = fixture()
    state.slotclear_fresh_buy_bar_ms = 1200
    state.bars = [SimpleNamespace(timestamp_ms=1200)]
    state.atr_state = "long"
    state.slotclear_fresh_buy_segment_id = 800
    assert line_segment(state) == 800


@pytest.mark.parametrize("kind", ["falseflip", "owner_unknown"])
def test_actual_strict_admission_retains_original_gates(kind):
    strategy, state, draft = fixture()
    strategy._falseflip_entry_blocked = lambda s: kind == "falseflip"
    strategy._flip_owned_first_entry_enabled = True
    strategy._flip_owner_counts = {"admission_evaluated": 0, "admission_refused_unknown": 0}
    strategy._removed_wait_gate_closed = lambda symbol: False
    strategy._flip_owner_restore_readable = True
    strategy._retry_one_enabled = False
    strategy._flip_owner_evidence_fresh = lambda s: True
    assert entry_decision(strategy, "WORX").allowed
    assert not SchwabV2Strategy._strict_first_rest_admitted(strategy, state, slot="first")
    assert state.flip_owner_phase == "unknown"  # No note retirement or gate bypass.


@pytest.mark.asyncio
@pytest.mark.parametrize("intent,side", [("close", "sell"), ("cancel", "buy")])
async def test_cancel_and_exit_never_require_entry_book(intent, side):
    strategy, state, draft = fixture()
    strategy._brokerdecides1_memory = None
    draft.intent_type, draft.side = intent, side
    redis = PrivateRedis()
    emitter = SchwabV2IntentEmitter(strategy.settings, redis, ACCOUNTS[0])
    await emitter.emit(draft)
    assert len(redis.events) == 1


@pytest.mark.asyncio
async def test_flag_off_emitter_keeps_legacy_behavior_without_guard():
    strategy, state, draft = fixture()
    strategy.settings.strategy_schwab_1m_v2_brokerdecides1_enabled = False
    redis = PrivateRedis()
    await SchwabV2IntentEmitter(strategy.settings, redis, ACCOUNTS[0]).emit(draft)
    assert len(redis.events) == 1
    assert "brokerdecides_line_segment_ms" not in draft.metadata


def test_only_exact_positive_closure_releases_reservation():
    strategy, state, draft = fixture()
    memory = strategy._brokerdecides1_memory
    prepare_send(strategy, draft, ACCOUNTS[0])
    value = replace(memory.snapshot, generation=2,
        terminal_reservations=frozenset({("WORX", "foreign")}))
    memory.apply(value)
    assert not entry_decision(strategy, "WORX").allowed
    memory.apply(replace(value, generation=3,
        terminal_reservations=frozenset({("WORX", "persisted-attempt")})))
    assert entry_decision(strategy, "WORX").allowed


@pytest.mark.parametrize("account", ACCOUNTS)
def test_actual_country_contract_does_not_build_parallel_webull_leg(account):
    strategy, state, draft = fixture(account)
    assert SchwabV2Strategy._build_webull_fanout_draft(strategy, state, entry_px=3.0,
        session_is_eh=True, source="resting", entry_n=1) is None


def test_foreign_account_same_opportunity_has_no_reservation_exception():
    strategy, state, draft = fixture()
    with pytest.raises(ValueError, match="selected_account_UNKNOWN"):
        prepare_send(strategy, draft, ACCOUNTS[1])
    assert entry_decision(strategy, "WORX").allowed


def test_legacy_fanout_is_refused_before_first_leg_not_silently_half_published():
    strategy, state, draft = fixture()
    state.atr_country_route = {}
    strategy.settings.strategy_schwab_1m_v2_country_route_enabled = False
    with pytest.raises(ValueError, match="legacy_fanout_UNREVIEWED"):
        prepare_send(strategy, draft, ACCOUNTS[0])
    assert entry_decision(strategy, "WORX").allowed


@pytest.mark.parametrize("index", [0, 1])
def test_same_opportunity_never_waives_any_native_working_order(index):
    strategy, state, draft = fixture()
    memory = strategy._brokerdecides1_memory
    books = list(memory.snapshot.books)
    books[index] = replace(books[index], working=frozenset({"WORX"}))
    memory.apply(replace(memory.snapshot, generation=2, books=tuple(books)))
    with pytest.raises(ValueError, match="native_working_order"):
        prepare_send(strategy, draft, ACCOUNTS[0])
