"""UNRUN source controls for Main after the external proposal is integrated.

CONTROLLED state and clocks; real strategy guards, sizing and stream parser.
No native/provider/database requests and no permission callback replacements.
These controls do not attest the installed ALLON profile or historical NCPL cause.
"""

from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
import json

import pytest

from project_mai_tai import pm_softrest_quote_log as diagnostic
from project_mai_tai.market_data.schwab_v2_streamer import SchwabV2Streamer
from project_mai_tai.services import schwab_1m_v2_bot as bot_module
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import schwab_1m_v2 as strategy_module
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy


AT = datetime(2026, 10, 29, 11, 40, 20, 257223, tzinfo=UTC)
NOW = int(AT.timestamp() * 1000)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return AT.astimezone(tz) if tz is not None else AT.replace(tzinfo=None)


class Logger:
    def __init__(self, broken=False):
        self.rows = []
        self.broken = broken

    def info(self, template, *args):
        if self.broken:
            raise RuntimeError('diagnostic logger failed')
        self.rows.append(json.loads((template % args).split(' ', 1)[1]))


@pytest.fixture
def clock(monkeypatch):
    # Control only time. The actual EH/session/entry/line/size methods run.
    monkeypatch.setattr(strategy_module, 'datetime', FixedDatetime)
    monkeypatch.setattr(bot_module, 'datetime', FixedDatetime)


def armed():
    settings = Settings(_env_file=None,
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=True,
        strategy_schwab_1m_v2_eh_resting_stream_cross_enabled=True,
        strategy_schwab_1m_v2_pm_print_ask_confirm_enabled=True)
    restored = settings.strategy_schwab_1m_v2_line_chart_restoration_enabled
    if restored:
        from tests.unit.pm_softrest_real_prelude import prepare_real_line_country
        strategy, state = prepare_real_line_country(settings, symbol='NCPL', at=AT)
    else:
        strategy = SchwabV2Strategy(settings)
        state = strategy.watchlist_state('NCPL')
    state.resting_level = 1.536583
    if not restored:
        state.atr_trail = state.resting_level
    state.resting_trigger = state.resting_schwab_wire_stop = 1.55
    state.resting_wire_cap = state.resting_schwab_wire_limit = 1.56
    state.resting_active = True
    state.resting_is_broker_order = False
    if not restored:
        state.bars.append(OHLCVBar(NOW // 60000 * 60000 - 60000,
                                  1.50, 1.54, 1.49, 1.53, 12000))
    else:
        assert strategy.line_buy_ready('NCPL')
        assert strategy._line_version_reader('NCPL')
    return strategy, state


def observation(logger):
    book = diagnostic.PmSoftRestQuoteLog(logger, monotonic_ns=lambda: 0)
    return book.begin('NCPL', received_at=AT, kind='trade', service='LEVELONE_EQUITIES',
        bid=1.54, ask=1.55, last=1.55, message_ts_ms=NOW,
        trade_ts_ms=NOW - 100, placed={})


def run_strategy(*, enabled, case, bad_observer=False, bad_logger=False):
    strategy, state = armed()
    price, ask, event, age = 1.55, 1.55, NOW - 100, 100
    if case == 'missing_ask':
        ask = None
    elif case == 'past_cap':
        ask = 1.57
    elif case == 'old_print':
        event = NOW - 100000
    elif case == 'below_trigger':
        price = 1.54
    elif case == 'boot_hold':
        strategy._entries_held = True
    elif case == 'position':
        state.position_qty = 1
    elif case == 'latch':
        state.resting_flip_ms = NOW - 1
    elif case == 'stale_bar':
        state.bars.clear()
    calls = []
    original = strategy._now_ms
    def counted_clock():
        calls.append('clock')
        return original()
    strategy._now_ms = counted_clock
    logger = Logger(broken=bad_logger)
    observed = observation(logger) if enabled else None
    if bad_observer:
        def broken(*args, **kwargs):
            raise RuntimeError('observer method failed')
        observed.note = observed.values = broken
    options = {} if observed is None else {'_pm_observation': observed}
    draft = strategy.on_stream_trade('NCPL', price, event, ask_price=ask,
                                    bid_price=1.54, print_size=100, ask_age_ms=age, **options)
    diagnostic.observe(observed, 'finish')
    return (asdict(draft) if draft is not None else None,
            deepcopy(state.__dict__), deepcopy(strategy._pending_webull_fanout_intents),
            calls, logger.rows)


@pytest.mark.parametrize('case,reason', [
    ('missing_ask', 'no_fresh_ask'), ('past_cap', 'ask_past_band'),
    ('old_print', 'print_not_fresh'), ('below_trigger', 'trigger_price_or_latch'),
    ('boot_hold', 'boot_hold'), ('position', 'position_or_latch'),
    ('latch', 'trigger_price_or_latch'), ('stale_bar', 'bar_missing_or_stale'),
])
def test_actual_refusal_and_state_clock_queue_equality(clock, case, reason):
    off = run_strategy(enabled=False, case=case)
    on = run_strategy(enabled=True, case=case)
    assert off[:4] == on[:4]
    assert off[0] is None and off[4] == []
    assert on[4][-1]['decision'] == 'REFUSED' and on[4][-1]['reason'] == reason


@pytest.mark.parametrize('failure', ['none', 'observer', 'logger'])
def test_actual_positive_draft_and_latch_unchanged_even_if_diagnostics_fail(clock, failure):
    off = run_strategy(enabled=False, case='positive')
    on = run_strategy(enabled=True, case='positive',
                      bad_observer=failure == 'observer', bad_logger=failure == 'logger')
    assert off[0] is not None, 'CONTROLLED prelude must earn a real draft'
    assert off[:4] == on[:4]
    assert on[1]['resting_flip_ms'] == NOW
    if failure == 'none':
        assert on[4][-1]['decision'] == 'DRAFT_CREATED'


class Writer:
    def __init__(self):
        self.ticks = []

    async def on_tick(self, tick):
        self.ticks.append(tick)


def bot_shell(enabled):
    # No service initialize()/REST client/emitter construction. Methods and
    # strategy are real; the existing writer boundary is a controlled sink.
    bot = SchwabV2BotService.__new__(SchwabV2BotService)
    bot.strategy, state = armed()
    bot.settings = bot.strategy.settings
    bot._eh_stream_cross_enabled = True
    bot._gap_hold_enabled = False
    bot._line_restoration_enabled = False
    bot._eh_stream_ask_by_symbol = {}
    bot._eh_stream_ask_max_age_ms = 10000
    bot._eh_stream_emit_tasks = set()
    bot.tick_writer = Writer()
    logger = Logger()
    bot._pm_softrest_quote_log = diagnostic.PmSoftRestQuoteLog(logger, monotonic_ns=lambda: 0) if enabled else None
    return bot, state, logger


@pytest.mark.asyncio
@pytest.mark.parametrize('native_trade,expected', [
    (None, 'native_trade_time_missing_or_mismatch'), (NOW - 100, 'ask_past_band'),
])
async def test_real_parser_callback_native_delta_and_writer_equality(clock, native_trade, expected):
    results = []
    for enabled in (False, True):
        bot, state, logger = bot_shell(enabled)
        quote = {'0': 'NCPL', '2': 1.57}
        for tick in SchwabV2Streamer._extract_level_one_ticks(quote, NOW - 2000):
            await bot._handle_stream_tick(tick)
        trade = {'0': 'NCPL', '3': 1.55}
        if native_trade is not None:
            trade['35'] = native_trade
        for tick in SchwabV2Streamer._extract_level_one_ticks(trade, NOW):
            await bot._handle_stream_tick(tick)
        assert not bot._eh_stream_emit_tasks and state.resting_flip_ms == 0
        results.append((deepcopy(state.__dict__), bot._eh_stream_ask_by_symbol,
                        [asdict(tick) for tick in bot.tick_writer.ticks], logger.rows))
    assert results[0][:3] == results[1][:3]
    row = results[1][3][-1]
    assert row['reason'] == expected and row['quote_event_ts_ms'] == 'UNKNOWN'
    assert row['quote_ts_ms'] == NOW - 2000
    assert row['schwab_message_ts_ms'] == NOW
    assert row['ask'] == 'UNKNOWN'
    assert row['trade_ts_ms'] == (native_trade if native_trade is not None else 'UNKNOWN')


@pytest.mark.asyncio
async def test_callback_off_preserves_original_offer_kwargs_and_disconnect(clock):
    bot, _, logger = bot_shell(False)
    # Fail if OFF adds a new kwarg or constructs a diagnostic observation.
    calls = []
    def offer(tick):
        calls.append(tick)
    bot._offer_stream_trade_to_eh_rest = offer
    bot._pm_softrest_quote_observation = lambda tick: pytest.fail('OFF observation')
    ticks = SchwabV2Streamer._extract_level_one_ticks({'0': 'NCPL', '3': 1.55, '35': NOW - 1}, NOW)
    await bot._handle_stream_tick(ticks[0])
    assert calls == ticks and bot.tick_writer.ticks == ticks and not logger.rows
    await bot._clear_eh_stream_asks_on_disconnect()
    assert bot._eh_stream_ask_by_symbol == {}


@pytest.mark.parametrize('schedule_fails', [False, True])
@pytest.mark.asyncio
async def test_real_positive_cross_preserves_schedule_boundary_and_old_fanout_suffix(clock, monkeypatch, schedule_fails):
    results = []
    for enabled in (False, True):
        bot, state, logger = bot_shell(enabled)
        older = object()
        bot.strategy._pending_webull_fanout_intents.append(older)
        scheduled = []
        class Task:
            def add_done_callback(self, callback):
                self.callback = callback
        task = Task()
        def schedule(coroutine):
            # Sink the existing emission coroutine at its scheduling boundary;
            # do not run its network/DB-facing body or grant any entry guard.
            coroutine.close()
            scheduled.append(True)
            if schedule_fails:
                raise RuntimeError('controlled scheduling failure')
            return task
        monkeypatch.setattr(bot_module.asyncio, 'create_task', schedule)
        for tick in SchwabV2Streamer._extract_level_one_ticks(
                {'0': 'NCPL', '1': 1.54, '2': 1.55, '3': 1.55, '35': NOW - 100, '9': 100}, NOW):
            await bot._handle_stream_tick(tick)
        assert bot.strategy._pending_webull_fanout_intents == [older]
        assert state.resting_flip_ms == NOW and scheduled == [True]
        assert len(bot._eh_stream_emit_tasks) == (0 if schedule_fails else 1)
        results.append((deepcopy(state.__dict__), len(bot._eh_stream_emit_tasks), logger.rows))
    assert results[0][:2] == results[1][:2]
    assert results[1][2][0]['reason'] == ('schedule_error' if schedule_fails else 'draft_scheduled')


@pytest.mark.parametrize('hour', [14, 21])
def test_rth_and_afterhours_do_not_construct_diagnostic_state(clock, monkeypatch, hour):
    class OtherDatetime(FixedDatetime):
        @classmethod
        def now(cls, tz=None):
            when = AT.replace(hour=hour)
            return when.astimezone(tz) if tz is not None else when.replace(tzinfo=None)
    monkeypatch.setattr(bot_module, 'datetime', OtherDatetime)
    bot, _, logger = bot_shell(True)
    tick = SchwabV2Streamer._extract_level_one_ticks({'0': 'NCPL', '2': 1.55}, NOW)[0]
    assert bot._pm_softrest_quote_observation(tick) is None
    assert not bot._pm_softrest_quote_log._records and not logger.rows


@pytest.mark.parametrize('change', ['unarmed', 'broker_rest', 'missing_state'])
def test_service_does_not_create_observation_outside_armed_softrest(clock, change):
    bot, state, logger = bot_shell(True)
    if change == 'unarmed':
        state.resting_active = False
    elif change == 'broker_rest':
        state.resting_is_broker_order = True
    else:
        bot.strategy._symbol_states.clear()
    tick = SchwabV2Streamer._extract_level_one_ticks({'0': 'NCPL', '2': 1.55}, NOW)[0]
    assert bot._pm_softrest_quote_observation(tick) is None
    assert not bot._pm_softrest_quote_log._records and not logger.rows


def test_message_metadata_does_not_change_native_event_clock_raw_or_hash():
    payload = {'0': 'NCPL', '1': 1.54, '2': 1.55, '3': 1.55, '35': NOW - 100}
    ticks = SchwabV2Streamer._extract_level_one_ticks(payload, NOW)
    assert [tick.kind for tick in ticks] == ['trade', 'quote']
    assert [tick.event_ts_ms for tick in ticks] == [NOW - 100, NOW]
    assert all(tick.message_ts_ms == NOW and tick.raw is payload for tick in ticks)
    assert ticks[0].raw_hash == ticks[1].raw_hash
    timesale = SchwabV2Streamer._extract_timesale_tick({'0': 'NCPL', '1': NOW - 1, '2': 1.55, '3': 100}, NOW)
    assert timesale.message_ts_ms is None and timesale.event_ts_ms == NOW - 1


@pytest.mark.asyncio
@pytest.mark.parametrize('pm_enabled,flip_enabled', [(False, False), (True, False),
                                                  (False, True), (True, True)])
async def test_composed_diagnostics_preserve_real_callback_and_refusal(
        clock, monkeypatch, pm_enabled, flip_enabled):
    # CONTROLLED independent diagnostic switches, not an eligibility override.
    results = []
    for pm, flip in ((False, False), (pm_enabled, flip_enabled)):
        bot, state, logger = bot_shell(pm)
        bot.strategy._flip_print_enabled = flip
        reads = []
        monkeypatch.setattr(bot_module.time, 'time_ns',
                            lambda: reads.append(True) or NOW * 1000000)
        for tick in SchwabV2Streamer._extract_level_one_ticks(
                {'0': 'NCPL', '1': 1.54, '2': 1.57, '3': 1.55,
                 '35': NOW - 100, '9': 100}, NOW):
            await bot._handle_stream_tick(tick)
        assert not bot._eh_stream_emit_tasks and state.resting_flip_ms == 0
        assert reads == ([True] if flip else [])
        if flip:
            assert len(bot.strategy._flip_print_observer._trades['NCPL']) == 1
        else:
            assert not hasattr(bot.strategy, '_flip_print_observer')
        if pm:
            assert logger.rows[-1]['reason'] == 'ask_past_band'
        else:
            assert logger.rows == []
        results.append((deepcopy(state.__dict__), dict(bot._eh_stream_ask_by_symbol),
                        [asdict(tick) for tick in bot.tick_writer.ticks]))
    assert results[0] == results[1]
