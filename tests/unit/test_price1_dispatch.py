"""PRICE1 built entry seams; controlled clocks/mock native transport, not fill proof."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.buy_price_floor import buy_order_allowed, buy_price_allowed
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.momentum_live.admission import Refusal
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import MOMENTUM_WARNING_STRATEGIES
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.strategy_core.orb_pastlevel import select_entry
from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata
from project_mai_tai.strategy_core.orb_schwab_open import OrbSchwabOpeningOrder
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, PendingHold
from tests.unit.test_momlive1_execution import setup
from tests.unit.test_momentum_warning import START, bar
from tests.unit.test_orb_schwab_open import OPEN, _bar, _close
from tests.unit.test_schwab_1m_v2_eh_resting_entry import PRE_WIN, _arm, _strat as eh_strategy
from tests.unit.test_schwab_1m_v2_resting_entry import _strat, _tick
from tests.unit.test_schwab_1m_v2_cw_v2 import _arm_to_watch, _quote, _strat as reactive_strategy
from tests.unit.test_schwab_1m_v2_hold_confirm import _strat as hold_strategy, _warm_to_short


def request(*, strategy="schwab_1m_v2", price="0.9975", side="buy"):
    key = {"schwab_1m_v2": "entry_price", "orb_schwab": "orb_intended_break_level",
           "momentum_warn_live": "signal_price"}[strategy]
    return OrderRequest("price1-fixture", "live:orb", strategy, "OLOX", side,
                        "open" if side == "buy" else "close", D(2), "PRICE1 control",
                        {key: price, "limit_price": "1.01"}, "limit")


@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab", "momentum_warn_live"])
def test_original_subdollar_price_not_rescued_by_padded_limit(strategy):
    assert not buy_order_allowed(request(strategy=strategy))
    assert buy_order_allowed(request(strategy=strategy, price="1.20"))


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_orb_original_level_refused_even_with_tick_floor_cap_over_one(provider):
    level = D(".9975")
    assert D(build_orb_schwab_bracket_metadata(level, tick_cap=True)["limit_price"]) > 1
    with pytest.raises(ValueError, match="PRICE1-REFUSED"):
        select_entry(level, None, datetime.now(UTC), max_age_ms=2000, provider=provider, symbol="OLOX")


def test_orb_wait_retained_then_places_on_next_normal_eligible_bar():
    order = OrbSchwabOpeningOrder(OPEN, symbol="OLOX")
    for index in (0, 1):
        item = _bar(index, .9975)
        order.bars[item.timestamp] = item
    assert _close(order, 2, .9975) is None
    assert order.price1_waiting and not order.placed and not order.cancelled
    assert order.last_requested_level is None
    action = _close(order, 3, 1.20)
    assert action.kind == "place" and action.level == D("1.2")
    assert order.placed and not order.price1_waiting
    assert _close(order, 4, 1.20) is None


def test_atr_resting_original_trigger_refused_without_slot_then_resumes():
    strategy = _strat()
    state = strategy.watchlist_state("OLOX")
    assert _tick(strategy, state, trail=.9975) == []
    assert not state.resting_active and not state.cw_resting_taken and not state.cw_reclaim_taken
    assert state.flip_owner_opportunity_id == 0 and not state.fanout_webull_claimed
    drafts = _tick(strategy, state, trail=1.20)
    assert len(drafts) == 1 and drafts[0].metadata["entry_price"] == "1.2000"


def test_atr_pre_tick_floor_cannot_round_a_subdollar_signal_into_eligibility():
    strategy = _strat(strategy_schwab_1m_v2_resting_buy_round_up_enabled=True)
    state = strategy.watchlist_state("OLOX")
    strategy._queue_resting_place(state, .99999)
    assert strategy.drain_pending_intents() == []
    assert not state.resting_active and state.flip_owner_opportunity_id == 0


def test_atr_reprice_refusal_does_not_cancel_existing_wait():
    strategy = _strat()
    state = strategy.watchlist_state("OLOX")
    assert len(_tick(strategy, state, trail=1.20)) == 1
    before = (state.resting_level, state.resting_active, state.resting_trigger,
              state.resting_schwab_generation, state.flip_owner_opportunity_id)
    strategy._reprice_resting(state, .9975)
    assert strategy.drain_pending_intents() == []
    assert before == (state.resting_level, state.resting_active, state.resting_trigger,
                      state.resting_schwab_generation, state.flip_owner_opportunity_id)


def test_atr_pm_current_ask_below_one_holds_valid_trigger_then_resumes_once():
    strategy = eh_strategy()
    state = strategy.watchlist_state("GIXI")
    _arm(strategy, state, trail=1.20, now_ms=PRE_WIN + 1000)
    strategy._now_ms = lambda: PRE_WIN + 2000
    assert strategy.on_quote("GIXI", Quote("GIXI", .98, .99, 1.20, PRE_WIN + 2000, 0)) is None
    assert state.resting_active and state.resting_flip_ms == 0
    assert not state.cw_resting_taken and not state.fanout_webull_claimed
    fresh = Quote("GIXI", 1.20, 1.202, 1.201, PRE_WIN + 2000, 0)
    assert strategy.on_quote("GIXI", fresh) is not None
    assert strategy.on_quote("GIXI", fresh) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [".79", ".7597", ".8355", ".8937", ".9975"])
async def test_live_momentum_floor_precedes_any_episode_or_durable_claim(price):
    env = setup()
    with pytest.raises(Refusal, match="PRICE1-REFUSED"):
        await env.executor.admit(replace(env.signal, signal_price=D(price)))
    assert env.book.snapshot() == [] and env.sent == []
    env.ports.durable_claim.assert_not_awaited()
    env.ports.durable_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_current_ask_cannot_be_rounded_over_one_or_consume_wait():
    env = setup()
    env.quote["ask"] = ".99999"
    with pytest.raises(Refusal, match="PRICE1-REFUSED"):
        await env.executor.admit(env.signal)
    assert env.book.snapshot() == [] and env.sent == []
    env.quote["ask"] = "3.001"
    await env.executor.admit(env.signal)
    assert len(env.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("is_us", [True, False])
@pytest.mark.parametrize("utc_hour", [12, 14])
async def test_gixi_positive_entry_keeps_pm_rth_country_route_exactly_once(is_us, utc_hour):
    env = setup(is_us=is_us)
    env.now[0] = datetime(2026, 10, 14, utc_hour, tzinfo=UTC)
    env.quote.update(ask="5.25", received_at=env.now[0])
    signal = replace(env.signal, symbol="GIXI", signal_price=D("5.25"),
                     observed_ns=int(env.now[0].timestamp() * 1000000000))
    await env.executor.admit(signal)
    assert len(env.sent) == 1
    assert env.sent[0].broker_account_name == ("live:schwab_1m_v2" if is_us else "live:orb")
    await env.executor.admit(signal)
    assert len(env.sent) == 1


@pytest.mark.parametrize("ask", [.99, .99999])
def test_reactive_floor_precedes_provisional_and_segment_slot_latches(ask):
    strategy = reactive_strategy()
    state = strategy.watchlist_state("TEST")
    _arm_to_watch(strategy, state)
    quote = _quote(12.5)
    quote = replace(quote, ask_price=ask)
    before = (state.cw_v2_emit_claimed, state.cw_entries_this_flip, state.cw_resting_taken,
              state.cw_reclaim_taken, state.flip_owner_phase, state.fanout_webull_claimed)
    assert strategy._cw_v2_quote(state, quote) is None
    assert before == (state.cw_v2_emit_claimed, state.cw_entries_this_flip, state.cw_resting_taken,
                      state.cw_reclaim_taken, state.flip_owner_phase, state.fanout_webull_claimed)


def test_legacy_hold_arm_does_not_spend_under_one_segment_then_resumes():
    strategy = hold_strategy(hold_on=True, _atr_rearm_enabled=False)
    _, now = _warm_to_short(strategy)
    state = strategy.watchlist_state("TEST")
    state.atr_prev_trail = .9975
    assert strategy.on_quote("TEST", Quote("TEST", .996, .998, .9975, now, 0)) is None
    assert not state.atr_fired_in_short_seg and state.atr_hold_pending is None
    state.atr_prev_trail = 1.20
    assert strategy.on_quote("TEST", Quote("TEST", 1.199, 1.202, 1.20, now + 1000, 0)) is None
    assert state.atr_hold_pending is not None and state.atr_fired_in_short_seg


def test_pending_hold_under_one_current_operand_is_not_cleared_then_resumes():
    strategy = hold_strategy(hold_on=True, _atr_rearm_enabled=False)
    _, now = _warm_to_short(strategy)
    state = strategy.watchlist_state("TEST")
    pending = PendingHold(1.20, now, now + 20000, 1, .99, 6)
    state.atr_hold_pending = pending
    state.last_quote = Quote("TEST", .98, .99, .99, now + 20000, 0)
    assert strategy._resolve_hold(state, pending, now + 20000) is None
    assert state.atr_hold_pending is pending and state.atr_guard != "PROVISIONAL"
    pending.last_px = 1.202
    state.last_quote = Quote("TEST", 1.20, 1.202, 1.202, now + 21000, 0)
    assert strategy._resolve_hold(state, pending, now + 21000) is not None
    assert state.atr_hold_pending is None


def test_fresh_bar_floor_preserves_pending_cross_after_indicator_and_sell_observation():
    strategy = hold_strategy(hold_on=False)
    _, now = _warm_to_short(strategy)
    state = strategy.watchlist_state("TEST")
    state.pending_path_macd = state.pending_path_vwap = True
    state.pending_cross_bar_ts_ms = now - 60000
    state.bars[-1] = OHLCVBar(now, 1.0, 1.01, .98, .99, 25000)
    assert strategy._evaluate_completed_bar(state, is_new_bar=True, restored=True,
        restored_signal={"touch": False, "flip": None, "state": "short"}) is None
    assert state.pending_path_macd and state.pending_path_vwap
    assert state.pending_cross_bar_ts_ms == now - 60000


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_oms_floor_before_controls_persistence_or_rpg(strategy):
    # No OMS collaborators exist: a sub-$1 refusal must precede touching them.
    service = object.__new__(OmsRiskService)
    assert await service.process_trade_intent(SimpleNamespace(payload=request(strategy=strategy))) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_type", [SchwabBrokerAdapter, WebullBrokerAdapter])
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab", "momentum_warn_live"])
async def test_native_entry_floor_precedes_account_lookup_or_native_transport(adapter_type, strategy):
    adapter = object.__new__(adapter_type)
    reports = await adapter.submit_order(request(strategy=strategy))
    assert len(reports) == 1 and reports[0].reason == "PRICE1-REFUSED"
    assert reports[0].origin == "client" and reports[0].broker_order_id is None


@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab", "momentum_warn_live"])
def test_oloX_exit_and_buy_cancel_bypass_floor(strategy):
    assert buy_order_allowed(request(strategy=strategy, price=".97", side="sell"))
    assert buy_order_allowed(replace(request(strategy=strategy), intent_type="cancel"))


def test_l2_combo_required_current_operand_not_remembered_trigger_or_cap():
    # Shared API control for Gibbs integration; not proof his callback is wired.
    waiting = {"trigger": D("1.20"), "cap": D("1.206"), "consumed": False}
    for price in (D(".99"), D(".99999")):
        assert buy_price_allowed(waiting["trigger"], bot="schwab_1m_v2", symbol="OLOX")
        if buy_price_allowed(price, bot="schwab_1m_v2", symbol="OLOX"):
            waiting["consumed"] = True
        assert not waiting["consumed"]
    assert buy_price_allowed(D("1.20"), bot="schwab_1m_v2", symbol="OLOX")


@pytest.mark.parametrize("prior,current,ignored", [(".90", "1.20", False), ("2", ".99", True)])
def test_warning_current_close_replaces_prior_close_gate_without_crowding(prior, current, ignored):
    model = MomentumWarningConsumer(prior_closes={"TEST": D(prior)}, condition_version="controlled",
                                   coverage_started_ms=START - 60000)
    item = bar(low=".90", high=current)
    outcome, _ = model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    assert (outcome.reason == "warning_symbol_or_time_filter") is ignored
    assert model.candidates.cap == model.candidates.owner_cap == 16
    if ignored:
        assert "TEST" not in model.windows and not model.candidates._candidates


@pytest.mark.parametrize("prior,signal,admitted", [(".90", "1.20", True),
                                                 ("2", ".99", False), (None, "1.20", False)])
def test_bucket_current_signal_floor_keeps_unknown_prior_refusal(prior, signal, admitted):
    engine = MomentumPaperEngine(prior_closes={} if prior is None else {"TEST": D(prior)},
                                 condition_version="controlled", coverage_started_ms=START - 60000,
                                 strategy_windows=MOMENTUM_WARNING_STRATEGIES)
    rows = engine.detect_from_bucket(strategy_code=next(iter(MOMENTUM_WARNING_STRATEGIES)),
        symbol="TEST", reference_price=D(".70"), reference_start_ms=START - 1000,
        reference_end_ms=START, signal_price=D(signal), signal_start_ms=START,
        signal_end_ms=START + 1000, observed_decision_ns=(START + 1000) * 1000000,
        aggregate_received_utc_ns=(START + 1000) * 1000000,
        aggregate_published_utc_ns=(START + 1000) * 1000000)
    assert any(row.event_type == "DETECTED" for row in rows) is admitted
