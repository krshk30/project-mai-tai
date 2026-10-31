"""Author UNRUN, pure scalar/logging controls. No service, DB or provider."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from project_mai_tai import pm_softrest_quote_log as diagnostic


AT = datetime(2026, 10, 29, 11, 40, 20, 257223, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.at = 0

    def __call__(self):
        return self.at


class Logger:
    def __init__(self):
        self.lines = []

    def info(self, template, *args):
        self.lines.append(template % args)

    def rows(self):
        return [json.loads(line.split(' ', 1)[1]) for line in self.lines]


def setup():
    logger, clock = Logger(), Clock()
    return diagnostic.PmSoftRestQuoteLog(logger, monotonic_ns=clock), logger, clock


def begin(book, *, symbol='NCPL', kind='trade', received=AT, message=20000, native_trade=19643,
          service='LEVELONE_EQUITIES', bid=1.54, ask=None, last=1.55):
    return book.begin(symbol, received_at=received, kind=kind, service=service,
        bid=bid, ask=ask, last=last, message_ts_ms=message, trade_ts_ms=native_trade,
        placed=dict(placed_wire_trigger=1.55, placed_trigger=1.55, placed_line=1.536583,
                    placed_wire_cap=1.56, placed_legacy_cap=1.56))


def refuse(observation, reason='trigger_price_or_latch'):
    diagnostic.observe(observation, 'note', 'REFUSED', reason)
    diagnostic.observe(observation, 'finish')


def test_default_off_allocates_no_helper_or_clock(monkeypatch):
    monkeypatch.setattr(diagnostic, 'PmSoftRestQuoteLog', lambda *a, **k: pytest.fail('OFF helper allocation'))
    assert diagnostic.make_log(SimpleNamespace(), Logger()) is None
    assert diagnostic.make_log(SimpleNamespace(**{diagnostic.FLAG: False}), Logger()) is None
    assert diagnostic.make_log(SimpleNamespace(**{diagnostic.FLAG: 'true'}), Logger()) is None


def test_one_periodic_per_second_for_same_decision_despite_price_clock_changes():
    book, logger, clock = setup()
    for index in range(200):
        clock.at = index * 5_000_000
        refuse(begin(book, last=1.54 + index / 10000, message=index + 1))
    assert len(logger.lines) == 1
    clock.at = 1_000_000_000
    refuse(begin(book))
    assert len(logger.lines) == 2


def test_every_actual_reason_change_is_attempted_inside_the_period():
    book, logger, clock = setup()
    for reason in ('print_not_fresh', 'no_fresh_ask', 'ask_past_band', 'print_not_fresh'):
        refuse(begin(book), reason)
        clock.at += 1
    assert [row['reason'] for row in logger.rows()] == [
        'print_not_fresh', 'no_fresh_ask', 'ask_past_band', 'print_not_fresh']


def test_quote_only_does_not_overwrite_decision_key_or_double_log_native_pair():
    book, logger, clock = setup()
    refuse(begin(book))
    diagnostic.observe(begin(book, kind='quote'), 'finish')
    clock.at = 1_000_000_000
    diagnostic.observe(begin(book, kind='quote'), 'finish')
    clock.at += 1
    refuse(begin(book))
    assert len(logger.lines) == 2
    assert logger.rows()[1]['decision'] == 'NOT_EVALUATED'
    assert book._records['NCPL'].decision == ('REFUSED', 'trigger_price_or_latch')


def test_first_actual_decision_after_quote_heartbeat_is_a_change():
    book, logger, _ = setup()
    diagnostic.observe(begin(book, kind='quote'), 'finish')
    refuse(begin(book))
    assert len(logger.lines) == 2


def test_symbols_are_independent_and_forget_disconnect_and_day_reset_provenance():
    book, logger, clock = setup()
    quote = begin(book, kind='quote', message=12345)
    diagnostic.observe(quote, 'ask_updated', 20000)
    refuse(quote)
    refuse(begin(book, symbol='PCLA'))
    assert len(logger.lines) == 2
    diagnostic.observe(book, 'forget', 'NCPL')
    refuse(begin(book))
    assert logger.rows()[-1]['quote_ts_ms'] == 'UNKNOWN'
    quote = begin(book, kind='quote', message=23456)
    diagnostic.observe(quote, 'ask_updated', 30000)
    clock.at += 1
    refuse(begin(book, received=AT + timedelta(days=1)))
    assert logger.rows()[-1]['quote_ts_ms'] == 'UNKNOWN'
    diagnostic.observe(book, 'clear')
    assert book._records == {}


def test_prior_ask_delta_message_clock_is_not_current_trade_clock():
    book, logger, clock = setup()
    quote = begin(book, kind='quote', message=17000, ask=1.55, native_trade=None)
    diagnostic.observe(quote, 'ask_updated', 18131)
    diagnostic.observe(quote, 'finish')
    clock.at = 10
    trade = begin(book, message=20257, native_trade=19643, ask=None, bid=None)
    diagnostic.observe(trade, 'values', effective_ask=1.55, ask_age_ms=2126, decision_now_ms=20257)
    refuse(trade, 'ask_past_band')
    row = logger.rows()[-1]
    assert row['schwab_message_ts_ms'] == 20257 and row['trade_ts_ms'] == 19643
    assert row['quote_ts_ms'] == 17000 and row['quote_clock'] == 'schwab_message'
    assert row['quote_event_ts_ms'] == 'UNKNOWN'
    assert row['ask'] == row['bid'] == 'UNKNOWN' and row['effective_ask'] == '1.55'
    assert row['ask_source'] == 'stream_ask_cache'
    assert row['receive_ts_utc'] == AT.isoformat()


def test_missing_provenance_and_invalidated_ask_stay_unknown():
    book, logger, clock = setup()
    refuse(begin(book, message=None, native_trade=None))
    assert logger.rows()[0]['quote_ts_ms'] == 'UNKNOWN'
    quote = begin(book, kind='quote', message=12345)
    diagnostic.observe(quote, 'ask_updated', 20000)
    diagnostic.observe(quote, 'ask_invalidated')
    clock.at = 1_000_000_000
    refuse(begin(book))
    assert logger.rows()[-1]['quote_ts_ms'] == 'UNKNOWN'


def test_later_ask_update_cannot_relabel_a_delayed_observation():
    book, logger, _ = setup()
    first = begin(book, kind='quote', message=10000)
    diagnostic.observe(first, 'ask_updated', 11000)
    trade = begin(book, message=12000)
    later = begin(book, kind='quote', message=13000)
    diagnostic.observe(later, 'ask_updated', 14000)
    refuse(trade)
    assert logger.rows()[-1]['quote_ts_ms'] == 10000
    assert logger.rows()[-1]['ask_cache_clock_ms'] == 11000


@pytest.mark.parametrize('hour,minute', [(6, 59), (9, 30), (12, 0), (16, 1)])
def test_non_premarket_has_no_record_or_clock_call(hour, minute):
    book, logger, _ = setup()
    book._clock = lambda: pytest.fail('out-of-scope clock')
    local = AT.astimezone(diagnostic.ET).replace(hour=hour, minute=minute)
    assert begin(book, received=local) is None
    assert not book._records and not logger.lines


@pytest.mark.parametrize('service', ['TIMESALE_EQUITY', 'massive', 'OTHER'])
def test_other_feeds_never_become_schwab_levelone(service):
    book, logger, _ = setup()
    assert begin(book, service=service) is None
    assert not book._records and not logger.lines


def test_logger_failure_is_bounded_and_does_not_escape():
    book, logger, clock = setup()
    def broken(*args):
        raise RuntimeError('SECRET https://private.example/token/account')
    logger.info = broken
    for _ in range(50):
        refuse(begin(book))
    assert book.failures == 1
    clock.at = 1_000_000_000
    refuse(begin(book))
    assert book.failures == 2


@pytest.mark.parametrize('operation,args,kwargs', [
    ('note', ('REFUSED', 'gap_hold'), {}), ('values', (), {'cap': 1.56}),
    ('ask_updated', (12345,), {}), ('ask_invalidated', (), {}),
    ('finish', (), {}), ('none_if_unset', (), {}), ('forget', ('NCPL',), {}), ('clear', (), {}),
])
def test_failing_observer_method_is_contained(operation, args, kwargs):
    def broken(*a, **k):
        raise RuntimeError('observer failure')
    diagnostic.observe(SimpleNamespace(**{operation: broken}), operation, *args, **kwargs)


def test_invalid_values_and_unknown_reason_never_leak_raw_sdk_text():
    book, logger, _ = setup()
    secret = 'https://private.example/token/account'
    observation = begin(book, bid=secret, ask=float('nan'), last=float('inf'))
    diagnostic.observe(observation, 'values', cap=secret, quote_event_ts_ms=123)
    diagnostic.observe(observation, 'note', 'REFUSED', secret)
    diagnostic.observe(observation, 'finish')
    assert secret not in logger.lines[0]
    assert logger.rows()[0]['bid'] == logger.rows()[0]['ask'] == logger.rows()[0]['last'] == 'UNKNOWN'
    assert logger.rows()[0]['reason'] == 'cross_not_called'
    assert logger.rows()[0]['quote_event_ts_ms'] == 'UNKNOWN'


def test_rate_refusal_never_formats_or_logs(monkeypatch):
    book, _, clock = setup()
    refuse(begin(book))
    clock.at = 1
    monkeypatch.setattr(diagnostic.json, 'dumps', lambda *a, **k: pytest.fail('suppressed formatting'))
    refuse(begin(book))


def test_observation_internal_corruption_and_repeat_finish_cannot_escape():
    book, logger, _ = setup()
    observation = begin(book)
    observation._fields = None
    diagnostic.observe(observation, 'values', trigger=1.55)
    diagnostic.observe(observation, 'finish')
    diagnostic.observe(observation, 'finish')
    assert not logger.lines and book.failures == 1


def test_wrong_thread_ownership_refuses_only_diagnostics_without_workers():
    book, logger, _ = setup()
    book._owner = -1
    assert begin(book) is None
    diagnostic.observe(book, 'clear')
    assert not logger.lines
