"""Offline decision replay; raw inputs are the bounded October 6 receipts.

Run with PYTHONPATH=src:. against main c21d8274. Quote, liquidity, persistence,
window and provisional owner are controlled inputs, not a live-cache replay.
No broker wire calls are made and no trading implementation is changed.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from project_mai_tai.v2_flip_entry_ownership import FlipConfirmationClose, FlipPositionClose
from tests.unit.test_v2_retry_one import _book, _strategy


def ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def raw(name):
    return json.loads((Path(__file__).parent / 'zero-budget-receipts' / name).read_text())


def candle(symbol, time):
    bars = raw(f'codex-retryoff1-{symbol.lower()}-bars-20261006.json')
    row = next(row for row in bars if ms(row['bar_time']) == ms(time))
    return OHLCVBar(ms(time), float(row['open_price']), float(row['high_price']),
                   float(row['low_price']), float(row['close_price']), int(row['volume']))


def closed_case(symbol, max_retries=0):
    if symbol == 'OLOX':
        row = raw('codex-retryoff1-olox-rows-20261006.json')[0]
        opportunity, segment = 1791302942230, 1791302700000
        close_at, draft_at = '2026-10-06T16:20:14.431Z', '2026-10-06T16:21:02.657Z'
        bar_time, trail = '2026-10-06T16:20:00Z', 1.543078
    else:
        row = next(row for row in raw('codex-retryoff1-ipdn-rows-20261006.txt')
                   if row['id'] == 'a53adba2-3417-48c5-98c7-15307dab7b1c')
        opportunity, segment = 1791301442514, 1791301200000
        close_at, draft_at = '2026-10-06T16:16:07.952Z', '2026-10-06T16:17:02.924Z'
        bar_time, trail = '2026-10-06T16:16:00Z', 4.5179
    strategy, clock, writes = _strategy(enabled=True, max_retries=max_retries, dual=True)
    clock[0] = ms(close_at)
    state = strategy.watchlist_state(symbol)
    state.flip_owner_phase = 'provisional'
    state.flip_owner_opportunity_id = opportunity
    state.fanout_segment_id = opportunity
    state.flip_owner_first_rest_placed = True
    account = row['broker_account_name']
    state.flip_owner_fill_accounts = {account}
    state.flip_owner_position_ids = {account: row['id']}
    state.flip_owner_position_entry_ms = {account: ms(row['entry_time'])}
    state.flip_owner_provisional_started_ms = ms(row['entry_time'])
    state.flip_owner_flip_bar_ts = 0
    state.retry_one_segment_id = segment
    state.atr_short_flip_bar_ts = segment
    state.retry_one_watch_start_ms = segment - 60_000
    state.flip_owner_retry_segment_id = segment
    state.flip_owner_retry_closes_at_place = 0
    state.retry_one_closes_in_segment = 0
    state.retry_one_budget_readable = True
    state.fanout_webull_resting_consumed = True
    state.cw_resting_taken = True
    state.resting_active = False
    slot = fanout_slot_id(strategy_code='schwab_1m_v2', symbol=symbol,
                         segment_id=opportunity, slot='resting')
    _book(strategy, clock, symbol,
          closes=(FlipPositionClose(account, row['id'], 'CONFIRMATION_EXIT'),),
          confirmation_closes=(FlipConfirmationClose(account, row['id'], slot),))
    phase_after_close = state.flip_owner_phase
    clock[0] = ms(draft_at)
    state.bars.append(candle(symbol, bar_time))
    _book(strategy, clock, symbol)
    strategy._queue_resting_place(state, trail, slot='first')
    return {
        'symbol': symbol, 'enabled': True, 'max_retries': max_retries,
        'row_id': row['id'], 'segment_id': segment,
        'owner_after_close': phase_after_close,
        'closes_in_segment': state.retry_one_closes_in_segment,
        'primary_drafts': len(strategy.drain_pending_intents()),
        'mirror_drafts': len(strategy.drain_webull_direct_intents()),
        'budget_writes': writes,
    }


@pytest.mark.parametrize('symbol', ['OLOX', 'IPDN'])
def test_recorded_close_zero_retry_blocks_both_second_drafts(symbol):
    result = closed_case(symbol, int(os.environ.get('REPLAY_MAX_RETRIES', '0')))
    assert result['owner_after_close'] == 'consumed'
    assert result['closes_in_segment'] == 1
    assert result['primary_drafts'] == result['mirror_drafts'] == 0


@pytest.mark.parametrize('symbol', ['OLOX', 'IPDN'])
def test_recorded_close_budget_one_positive_control_emits_both(symbol):
    result = closed_case(symbol, 1)
    assert result['owner_after_close'] == 'idle'
    assert result['primary_drafts'] == result['mirror_drafts'] == 1


def fresh_sell_case():
    strategy, clock, writes = _strategy(enabled=True, max_retries=0, dual=True)
    clock[0] = ms('2026-10-06T15:41:02.394Z')
    state = strategy.watchlist_state('IPDN')
    # Consumed prior cycle: recorded 10:23 SELL and 11:01 closed primary row.
    prior = raw('codex-retryoff1-ipdn-rows-20261006.txt')[0]
    state.flip_owner_phase = 'consumed'
    state.flip_owner_opportunity_id = 1791296823073
    state.fanout_segment_id = 1791296823073
    state.flip_owner_first_rest_placed = True
    state.flip_owner_position_ids = {prior['broker_account_name']: prior['id']}
    state.flip_owner_fill_accounts = {prior['broker_account_name']}
    state.retry_one_segment_id = 1791296580000
    state.retry_one_watch_start_ms = 1791296580000 - 60_000
    state.retry_one_closes_in_segment = 1
    state.flip_owner_retry_segment_id = 1791296580000
    state.fanout_webull_resting_consumed = True
    state.cw_resting_taken = True
    state.bars.append(candle('IPDN', '2026-10-06T15:40:00Z'))
    state.atr_short_flip_bar_ts = 1791301200000
    _book(strategy, clock, 'IPDN')
    strategy._cw_v2_track(state, {'flip': 'SELL'})
    assert state.flip_owner_phase == 'idle'
    assert state.retry_one_segment_id == 1791301200000
    assert state.retry_one_closes_in_segment == 0
    assert writes[-1] == ('IPDN', 1791301200000, 0)
    clock[0] = ms('2026-10-06T15:44:02.526Z')
    state.bars.append(candle('IPDN', '2026-10-06T15:43:00Z'))
    _book(strategy, clock, 'IPDN')
    strategy._queue_resting_place(state, 4.6361, slot='first')
    return strategy, state, clock


def test_recorded_ipdn_fresh_sell_resets_consumed_budget_and_first_drafts_both():
    strategy, state, _clock = fresh_sell_case()
    assert state.retry_one_closes_in_segment == 0
    assert len(strategy.drain_pending_intents()) == 1
    assert len(strategy.drain_webull_direct_intents()) == 1


def test_recorded_ipdn_unfilled_wait_is_not_a_closed_trade():
    strategy, state, clock = fresh_sell_case()
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    clock[0] = ms('2026-10-06T15:56:04Z')
    _book(strategy, clock, 'IPDN')
    assert state.retry_one_closes_in_segment == 0
    assert state.flip_owner_phase == 'resting'
    assert state.resting_active


if __name__ == '__main__':
    print(json.dumps({'application': 'c21d8274fcd1d3129d61207a33dd7b002a7c9e8c',
                      'cases': [closed_case(s, n) for s in ('OLOX', 'IPDN') for n in (0, 1)],
                      'boundary': 'Offline decision replay; reconstructed owner and successful gates.'},
                     indent=2))
