"""COUNTRYROUTE-ATR controlled local contracts; no provider/broker connections."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from project_mai_tai.atr_country_route import (
    AtrCountryRouteBook, AtrCountryRouteStore, CountryRouteRefusal, selection_from_metadata,
)
from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.fanout_identity import carry_fanout_identity
from project_mai_tai import fanout_segment_store as fanout_store_module
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.momentum_live_sec import CountryRecord, CountryRouter, CountryStore
from project_mai_tai.services import schwab_1m_v2_bot as bot_module
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import schwab_1m_v2 as strategy_module
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar, PendingHold, SchwabV2IntentEmitter, SchwabV2Strategy,
)
from project_mai_tai.v2_restart_rest_store import checkpoint_state
from tests.unit.test_control_plane import build_test_session_factory

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)


def options(**extra):
    values = {
        "strategy_schwab_1m_v2_account_name": "live:schwab_1m_v2",
        "strategy_schwab_1m_v2_broker_provider": "schwab",
        "strategy_schwab_1m_v2_webull_account_name": "live:orb",
        "strategy_schwab_1m_v2_country_route_enabled": True,
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled": True,
        "strategy_schwab_1m_v2_dual_broker_fanout_enabled": True,
        "strategy_schwab_1m_v2_webull_resting_mirror_enabled": True,
    }
    values.update(extra)
    return Settings(_env_file=None, **values)


def country_router(is_us, *, status="READY", as_of=NOW - timedelta(days=1)):
    # Real CountryRouter on a controlled immutable in-memory SEC-table snapshot.
    router = CountryRouter(SimpleNamespace())
    if is_us is not None or status != "READY":
        router._records = {"TEST": CountryRecord(
            "TEST", "0000000001", "CA" if is_us else "L3", "DE" if is_us else "L3",
            is_us if status == "READY" else None, as_of, status, NOW.date())}
    return router


@pytest.fixture(autouse=True)
def owned_clock(monkeypatch):
    clock = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz is not None else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(strategy_module, "datetime", Clock)
    monkeypatch.setattr(bot_module, "datetime", Clock)
    monkeypatch.setattr(fanout_store_module, "datetime", Clock)
    return clock


def native_strategy(is_us, now=NOW, **extra):
    settings = options(**extra)
    strategy = SchwabV2Strategy(settings)
    clock = [int(now.timestamp() * 1000)]
    strategy._now_ms = lambda: clock[0]
    strategy._entries_held = False
    strategy.configure_country_routing(AtrCountryRouteBook(settings, country_router(is_us).route_for))
    state = strategy.watchlist_state("TEST")
    state.atr_state, state.atr_prev_state = "short", "short"
    state.atr_state_age, state.atr_trail = 6, 2.0
    state.bars.append(OHLCVBar(clock[0] - 60_000, 1.99, 2, 1.98, 1.99, 50000))
    state.last_quote = Quote("TEST", 1.99, 2.0, 2.0, clock[0])
    return strategy, state, clock


@pytest.mark.parametrize("is_us,broker,amount", [(True, "schwab", "600"), (False, "webull", "300"), (None, "webull", "300")])
@pytest.mark.parametrize("hour", [11, 14])
def test_actual_router_selects_one_venue_in_pm_and_rth(is_us, broker, amount, hour):
    now = NOW.replace(hour=hour, minute=30)
    book = AtrCountryRouteBook(options(), country_router(is_us).route_for)
    selected = book.select("TEST", 100, now)
    assert selected.broker == broker
    assert selected.metadata()["atr_country_notional_usd"] == amount
    assert selected.account == ("live:schwab_1m_v2" if is_us else "live:orb")


@pytest.mark.parametrize("is_us", [True, False, None])
@pytest.mark.parametrize("at", ["2026-10-15T10:59:59+00:00", "2026-10-15T19:45:00+00:00", "2026-10-15T20:00:00+00:00"])
def test_country_router_cannot_widen_atr_0700_1545(is_us, at):
    book = AtrCountryRouteBook(options(), country_router(is_us).route_for)
    with pytest.raises(CountryRouteRefusal, match="entry_window_closed"):
        book.select("TEST", 100, datetime.fromisoformat(at))
    assert book.routes == {}


def test_reload_does_not_switch_a_sealed_opportunity():
    router = country_router(True)
    book = AtrCountryRouteBook(options(), router.route_for)
    first = book.select("TEST", 100, NOW)
    router._records.clear()
    assert book.select("TEST", 100, NOW + timedelta(seconds=1)) is first
    assert book.select("TEST", 101, NOW + timedelta(seconds=1)).broker == "webull"


@pytest.mark.parametrize("status,as_of", [("INCOMPLETE", NOW - timedelta(seconds=1)), ("READY", NOW + timedelta(seconds=1))])
def test_incomplete_or_future_country_remains_unknown_webull(status, as_of):
    router = country_router(True, status=status, as_of=as_of)
    selected = AtrCountryRouteBook(options(), router.route_for).select("TEST", 100, NOW)
    assert selected.broker == "webull" and selected.reason == "sec_unknown"


@pytest.mark.parametrize("is_us,expected", [(True, 300), (False, 150), (None, 150)])
def test_selected_notional_sized_before_primary_claim(is_us, expected):
    strategy, state, _ = native_strategy(is_us)
    quantity, metadata = strategy._sized_open("TEST", leg="schwab", price=2, basis="trigger_quote_ask")
    assert quantity == expected
    assert metadata["atr_country_broker"] == ("schwab" if is_us else "webull")
    assert not state.cw_v2_emit_claimed
    assert strategy.drain_webull_fanout_intents() == []


@pytest.mark.parametrize("is_us", [True, False, None])
def test_new_rth_rest_queues_only_chosen_venue_and_only_its_cancel(is_us):
    strategy, state, _ = native_strategy(is_us)
    strategy._queue_resting_place(state, 2.0)
    primary, webull = strategy.drain_pending_intents(), strategy.drain_webull_direct_intents()
    chosen = primary if is_us else webull
    assert len(chosen) == 1
    assert (webull if is_us else primary) == []
    assert chosen[0].metadata["order_type"] == "STOP_LIMIT"
    assert chosen[0].metadata["atr_country_broker"] == ("schwab" if is_us else "webull")
    if not is_us:
        assert not any(key.startswith("bracket") for key in chosen[0].metadata)
    strategy._queue_resting_cancel(state, reason="window_closed")
    primary, webull = strategy.drain_pending_intents(), strategy.drain_webull_direct_intents()
    assert len(primary if is_us else webull) == 1
    assert (webull if is_us else primary) == []


def test_unsealed_active_segment_retains_both_legacy_rest_legs():
    strategy, state, _ = native_strategy(True)
    state.fanout_segment_id = 100
    strategy._country_route_book.legacy.add(("TEST", 100))
    strategy._queue_resting_place(state, 2.0)
    assert len(strategy.drain_pending_intents()) == 1
    assert len(strategy.drain_webull_direct_intents()) == 1
    strategy._queue_resting_cancel(state, reason="window_closed")
    assert len(strategy.drain_pending_intents()) == 1
    assert len(strategy.drain_webull_direct_intents()) == 1


@pytest.mark.parametrize("is_us", [True, False])
def test_hold_entry_keeps_route_and_does_not_queue_copy(is_us):
    strategy, state, clock = native_strategy(is_us)
    ph = PendingHold(2, clock[0] - 20000, clock[0], 2, 2.01, 5)
    draft = strategy._build_hold_draft(state, ph, "confirm", 50)
    assert draft is not None
    assert draft.quantity == (300 if is_us else 150)
    assert draft.metadata["atr_country_broker"] == ("schwab" if is_us else "webull")
    assert strategy.drain_webull_fanout_intents() == []


@pytest.mark.parametrize("is_us", [True, False])
def test_fill_and_rth_quote_never_mirror_a_selected_entry(is_us):
    strategy, state, clock = native_strategy(is_us)
    strategy._queue_resting_place(state, 2.0)
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    strategy.update_position("TEST", 10, held_qty=10)
    strategy._fanout_rth_resting_cross(state, Quote("TEST", 2, 2.01, 2.01, clock[0]))
    assert strategy.drain_webull_fanout_intents() == []


def test_restore_preserves_selected_route_and_legacy_distinction():
    factory, settings = build_test_session_factory(), options()
    store = AtrCountryRouteStore(factory, settings)
    selected = AtrCountryRouteBook(settings, country_router(True).route_for).select("TEST", 100, NOW)
    store.record(selected)
    store.record(selected)
    restored = store.restore()
    book = AtrCountryRouteBook(settings, country_router(None).route_for,
                               restored=restored, legacy={("TEST", 100), ("TEST", 99)})
    assert book.select("TEST", 100, NOW).broker == "schwab"
    assert book.select("TEST", 99, NOW) is None
    assert len(restored) == 1


def test_conflicting_durable_route_is_never_overwritten():
    factory, settings = build_test_session_factory(), options()
    store = AtrCountryRouteStore(factory, settings)
    selected = AtrCountryRouteBook(settings, country_router(True).route_for).select("TEST", 100, NOW)
    store.record(selected)
    changed = replace(selected, broker="webull", account="live:orb", reason="sec_unknown", status="UNKNOWN", as_of="")
    with pytest.raises(CountryRouteRefusal, match="journal_conflict"):
        store.record(changed)
    assert store.restore()[("TEST", 100)] == selected


def test_corrupt_route_restore_is_not_an_empty_legacy_book():
    factory, settings = build_test_session_factory(), options()
    store = AtrCountryRouteStore(factory, settings)
    selected = AtrCountryRouteBook(settings, country_router(True).route_for).select("TEST", 100, NOW)
    store.record(selected)
    with factory() as session:
        row = session.get(DashboardSnapshot, store.identity(selected))
        row.payload = {**row.payload, "atr_country_account": "different"}
        session.commit()
    with pytest.raises(CountryRouteRefusal):
        store.restore()


def test_sealed_metadata_survives_partial_report_without_a_route_lookup():
    selected = AtrCountryRouteBook(options(), country_router(False).route_for).select("TEST", 100, NOW)
    carried = carry_fanout_identity({"filled_quantity": "1"}, selected.metadata())
    assert selection_from_metadata(carried, options()) == selected


@pytest.mark.parametrize("is_us", [True, False, None])
def test_reactive_public_quote_returns_one_selected_draft(is_us):
    strategy, state, clock = native_strategy(is_us,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=False)
    state.cw_armed, state.cw_bars_waited = True, 2
    state.cw_segment_high, state.cw_flip_level = 2.0, 1.95
    draft = strategy.on_quote("TEST", Quote("TEST", 2, 2.02, 2.01, clock[0]))
    assert draft is not None
    assert draft.metadata["atr_country_broker"] == ("schwab" if is_us else "webull")
    assert strategy.drain_webull_fanout_intents() == []
    assert strategy.on_quote("TEST", Quote("TEST", 2, 2.02, 2.01, clock[0])) is None


@pytest.mark.parametrize("is_us", [True, False, None])
def test_pm_software_cross_selects_one_marketably_limited_entry(is_us):
    from tests.unit.test_pmprint1_pmflip1 import armed, cross

    strategy, state, clock = armed(
        ("TEST", "2026-10-15T11:30:00Z", 2.0, 2.0, 2.0),
        strategy_schwab_1m_v2_country_route_enabled=True)
    strategy.configure_country_routing(AtrCountryRouteBook(strategy.settings, country_router(is_us).route_for))
    draft = cross(strategy, state, clock, 2.0, 2.0, stream=False)
    assert draft is not None
    assert draft.metadata["order_type"] == "limit"
    assert draft.quantity == (300 if is_us else 150)
    assert draft.metadata["atr_country_broker"] == ("schwab" if is_us else "webull")
    assert strategy.drain_webull_fanout_intents() == []
    assert cross(strategy, state, clock, 2.0, 2.0, stream=False) is None


class RecordedRedis:
    def __init__(self, *, fail=False):
        self.events, self.fail = [], fail

    async def xadd(self, stream, fields, **kwargs):
        self.events.append(json.loads(fields["data"]))
        if self.fail:
            raise ConnectionError("CONTROLLED_UNKNOWN_REDIS_ACK")
        return "1-0"


def wired_bot(is_us, *, redis=None):
    strategy, state, clock = native_strategy(is_us)
    factory = build_test_session_factory()
    bot = SchwabV2BotService(strategy.settings, session_factory=factory)
    bot.strategy = strategy
    bot._country_route_store = AtrCountryRouteStore(factory, strategy.settings)
    bot.redis = redis or RecordedRedis()
    bot.intent_emitter = SchwabV2IntentEmitter(strategy.settings, bot.redis, "live:schwab_1m_v2")
    bot.webull_intent_emitter = SchwabV2IntentEmitter(strategy.settings, bot.redis, "live:orb")
    bot._last_quote_by_symbol = {"TEST": state.last_quote}
    bot._watchlist = {"TEST"}
    return bot, state, clock


@pytest.mark.asyncio
@pytest.mark.parametrize("is_us,account", [(True, "live:schwab_1m_v2"), (False, "live:orb"), (None, "live:orb")])
async def test_actual_emitter_journals_before_only_selected_account_event(is_us, account):
    bot, state, clock = wired_bot(is_us)
    draft = bot.strategy._build_hold_draft(state, PendingHold(2, clock[0], clock[0], 2, 2), "confirm", 0)
    assert await bot._maybe_emit(draft) == "queued"
    assert len(bot.redis.events) == 1
    event = bot.redis.events[0]
    assert event["payload"]["broker_account_name"] == account
    assert len(bot._country_route_store.restore()) == 1
    assert event["payload"]["metadata"]["atr_country_schema"] == "1"


@pytest.mark.asyncio
async def test_unknown_redis_ack_never_falls_back_to_other_broker():
    bot, state, clock = wired_bot(True, redis=RecordedRedis(fail=True))
    draft = bot.strategy._build_hold_draft(state, PendingHold(2, clock[0], clock[0], 2, 2), "confirm", 0)
    assert await bot._maybe_emit(draft) == "could_not_tell"
    assert len(bot.redis.events) == 1
    assert bot.redis.events[0]["payload"]["broker_account_name"] == "live:schwab_1m_v2"
    assert bot.strategy.drain_webull_fanout_intents() == []
    assert len(bot._country_route_store.restore()) == 1


@pytest.mark.asyncio
async def test_conflicting_cached_metadata_cannot_switch_an_already_journaled_key():
    bot, state, clock = wired_bot(True)
    draft = bot.strategy._build_hold_draft(state, PendingHold(2, clock[0], clock[0], 2, 2), "confirm", 0)
    assert await bot._maybe_emit(draft) == "queued"
    draft.metadata.update(atr_country_broker="webull", atr_country_account="live:orb",
                          atr_country_notional_usd="300", atr_country_reason="sec_unknown",
                          atr_country_status="UNKNOWN", atr_country_as_of="", fanout_leg="webull")
    assert not await bot._country_before_publish(bot.webull_intent_emitter, draft)
    assert len(bot.redis.events) == 1


@pytest.mark.asyncio
async def test_cancel_does_not_require_country_or_route_store_after_close(owned_clock):
    from project_mai_tai.strategy_core.schwab_1m_v2 import TradeIntentDraft

    bot, _, _ = wired_bot(True)
    owned_clock[0] = NOW.replace(hour=22)
    bot._country_route_store = None
    draft = TradeIntentDraft(symbol="TEST", side="buy", intent_type="cancel", quantity=1,
                             reason="CONTROLLED_EXISTING_GENERATION", metadata={})
    assert await bot._emit_removal_tracked(bot.intent_emitter, draft)
    assert bot.redis.events[0]["payload"]["intent_type"] == "cancel"


@pytest.mark.parametrize("is_us", [True, False])
def test_checkpoint_preserves_single_rest_and_exact_broker_cancellation_without_country_db(is_us):
    strategy, state, _ = native_strategy(is_us)
    strategy._queue_resting_place(state, 2.0)
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    saved = checkpoint_state(state)
    assert saved["country_route"]["atr_country_broker"] == ("schwab" if is_us else "webull")
    restored, target, _ = native_strategy(not is_us)
    target.fanout_segment_id = saved["opportunity_id"]
    legs = {"live:schwab_1m_v2": "owned" if is_us else "absent",
            "live:orb": "absent" if is_us else "owned"}
    assert restored.restore_restart_rest(target, saved, legs)
    restored.configure_country_routing(None)
    restored._queue_resting_cancel(target, reason="window_closed")
    primary, webull = restored.drain_pending_intents(), restored.drain_webull_direct_intents()
    assert len(primary if is_us else webull) == 1
    assert (webull if is_us else primary) == []


@pytest.mark.parametrize("is_us", [True, False])
def test_memory_only_eh_checkpoint_seals_country_before_any_publication(is_us):
    now = NOW.replace(hour=11, minute=30)
    strategy, state, _ = native_strategy(is_us, now)
    strategy._resting_session_is_eh = lambda now=None: True
    strategy._queue_resting_place(state, 2)
    assert not state.resting_is_broker_order
    assert strategy.drain_pending_intents() == []
    assert strategy.drain_webull_direct_intents() == []
    saved = checkpoint_state(state)
    selected = selection_from_metadata(saved["country_route"], strategy.settings)
    book = AtrCountryRouteBook(strategy.settings, country_router(not is_us).route_for,
                               restored={(state.symbol, selected.segment_id): selected})
    assert book.select(state.symbol, selected.segment_id, now).broker == ("schwab" if is_us else "webull")


@pytest.mark.asyncio
async def test_actual_boot_restores_legacy_and_checkpoint_seal_without_provider_worker():
    settings, factory = options(), build_test_session_factory()
    bot = SchwabV2BotService(settings, session_factory=factory)
    bot.fanout_identity_store = FanoutSegmentIdentityStore(factory)
    bot.fanout_identity_store.record("TEST", 100, True, "CONTROLLED_OLD", now=NOW)
    selected = AtrCountryRouteBook(settings, country_router(True).route_for).select("TEST", 101, NOW)
    bot._restart_checkpoint = {"symbols": {"TEST": {"state": {
        "opportunity_id": 101, "country_route": selected.metadata()}}}}
    await bot._configure_country_router({"TEST": 100})
    book = bot.strategy._country_route_book
    assert book.select("TEST", 100, NOW) is None
    assert book.select("TEST", 101, NOW) == selected
    assert bot._country_router.client is None and bot._country_router._worker is None
    assert bot._country_route_store.restore()[("TEST", 101)] == selected
    await bot._country_router.close()


def test_fanout_restore_keeps_same_session_but_not_previous_session_at_04_et(owned_clock):
    store = FanoutSegmentIdentityStore(build_test_session_factory())
    store.record("TEST", 100, True, "CONTROLLED_OLD", now=NOW)
    owned_clock[0] = datetime(2026, 10, 16, 7, 59, 59, tzinfo=UTC)
    assert store.restore_active() == {"TEST": 100}
    owned_clock[0] += timedelta(seconds=1)
    assert store.restore_active() == {}
    store.record("TEST", 102, True, "CONTROLLED_NEW", now=owned_clock[0])
    assert store.restore_active() == {"TEST": 102}


@pytest.mark.asyncio
async def test_boot_control_exposes_unbound_defining_store_clock(monkeypatch):
    class NextSession(datetime):
        @classmethod
        def now(cls, tz=None):
            value = NOW + timedelta(days=1)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    # Deliberately mutate ONLY the defining store clock: the old fixture lost100.
    monkeypatch.setattr(fanout_store_module, "datetime", NextSession)
    settings, factory = options(), build_test_session_factory()
    bot = SchwabV2BotService(settings, session_factory=factory)
    bot.fanout_identity_store = FanoutSegmentIdentityStore(factory)
    bot.fanout_identity_store.record("TEST", 100, True, "CONTROLLED_OLD", now=NOW)
    selected = AtrCountryRouteBook(settings, country_router(True).route_for).select("TEST", 101, NOW)
    bot._restart_checkpoint = {"symbols": {"TEST": {"state": {
        "opportunity_id": 101, "country_route": selected.metadata()}}}}
    await bot._configure_country_router({"TEST": 100})
    try:
        book = bot.strategy._country_route_book
        assert book.select("TEST", 100, NOW) is not None
        assert book.select("TEST", 101, NOW) == selected
        assert bot._country_router.client is None and bot._country_router._worker is None
    finally:
        await bot._country_router.close()


@pytest.mark.asyncio
async def test_0355_reload_uses_actual_country_store_and_does_not_rehome_working_route(monkeypatch):
    settings, factory = options(), build_test_session_factory()
    table = CountryStore(factory)
    table.put(country_router(True)._records["TEST"])
    bot = SchwabV2BotService(settings, session_factory=factory)
    bot.fanout_identity_store = FanoutSegmentIdentityStore(factory)
    await bot._configure_country_router({})
    book = bot.strategy._country_route_book
    original = book.select("TEST", 100, NOW)
    table.put(country_router(False, as_of=NOW - timedelta(seconds=1))._records["TEST"])
    bot._country_reload_day = NOW.date() - timedelta(days=1)

    async def one_iteration(stop, seconds):
        stop.set()

    monkeypatch.setattr(bot_module, "sleep_or_stop", one_iteration)
    await bot._country_reload_loop()
    assert book.select("TEST", 100, NOW) is original
    assert book.select("TEST", 101, NOW).broker == "webull"
    assert bot._country_reload_day == NOW.date()
    await bot._country_router.close()


@pytest.mark.asyncio
async def test_default_off_empty_boot_never_opens_sec_dependency(monkeypatch):
    settings = options(strategy_schwab_1m_v2_country_route_enabled=False)
    bot = SchwabV2BotService(settings, session_factory=build_test_session_factory())
    bot.fanout_identity_store = FanoutSegmentIdentityStore(bot.session_factory)

    def forbidden(*args, **kwargs):
        pytest.fail("default-OFF empty route book opened a SEC dependency")

    monkeypatch.setattr("project_mai_tai.momentum_live_sec.CountryStore", forbidden)
    await bot._configure_country_router({})
    assert bot._country_router is None


@pytest.mark.asyncio
async def test_country_boot_read_failure_refuses_new_entry_without_disabling_cancel(monkeypatch):
    bot, state, _ = wired_bot(True)
    bot.fanout_identity_store = FanoutSegmentIdentityStore(bot.session_factory)

    def unreadable(self):
        raise RuntimeError("CONTROLLED_COUNTRY_READ_FAILURE")

    monkeypatch.setattr(CountryStore, "load", unreadable)
    await bot._configure_country_router({})
    assert bot.strategy._sized_open("TEST", leg="schwab", price=2, basis="controlled") is None
    assert bot.strategy._country_restore_unknown
    state.resting_is_broker_order = True
    state.resting_active = True
    bot.strategy._queue_resting_cancel(state, reason="window_closed")
    assert bot.strategy.drain_pending_intents()
