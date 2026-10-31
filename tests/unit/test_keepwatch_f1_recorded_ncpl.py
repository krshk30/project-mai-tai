"""Retained NCPL chronology with explicitly CONTROLLED missing race operands.

The log proves the two cancel/UNKNOWN/stale-rebuild/recovery sequences. It does
not contain provider OHLCV, native rest generations, source identity or the
precise callback ordering. The integration control supplies those operands and
causes a real revision CAS refusal; it is not a native or economic replay.
"""
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import re

import pytest

from tests.unit import test_keepwatch1_quiet_gap_resume as gap


PACKET = Path('/Users/velkris/.codex/task-queues/analysis/OCT29-C6-ORIGINAL-CASE-RECEIPTS/f1_ncpl_blip_cancels_1238_1245.log')
PACKET_SHA = '27fcc58080a061854b0a472b443bc2420d9e0fa4a11e1a9a4f83e480b75cb93c'


def records():
    raw = PACKET.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PACKET_SHA
    result = []
    for line in raw.decode().splitlines():
        stamp = datetime.strptime(line[:23], '%Y-%m-%d %H:%M:%S,%f').replace(tzinfo=UTC)
        result.append((int(stamp.timestamp() * 1000), line))
    assert len(result) == 58
    return result


def episodes():
    result, current = [], None
    for stamp, line in records():
        if '[V2-KEEPWATCH1]' in line and 'outcome=UNKNOWN' in line:
            assert current is None
            current = dict(onset=stamp, transitions=[], cancels=[])
        if current is not None:
            reason = re.search(r'outcome=UNKNOWN reason=(\S+)', line)
            if reason:
                current['transitions'].append((stamp, reason[1]))
            if '[V2-RESTING-CANCEL]' in line:
                current['cancels'].append(stamp)
            if '[V2-KEEPWATCH1]' in line and 'outcome=PROVEN' in line:
                current['proven'] = stamp
                result.append(current)
                current = None
    assert current is None
    return result


def test_retained_packet_onsets_and_reported_reason_intervals():
    rows = episodes()
    assert [x['proven'] - x['onset'] for x in rows] == [1915, 7135, 991]
    assert [len(x['cancels']) for x in rows] == [1, 1, 0]
    assert [x['proven'] - x['cancels'][0] for x in rows[:2]] == [673, 5536]
    assert [next(t - x['onset'] for t, reason in x['transitions']
                 if reason == 'prefix_publication_unproven') for x in rows[:2]] == [1243, 1600]
    # Log residence intervals, not inferred durations of underlying conditions
    # or a census of all UNKNOWN episodes outside the two supplied windows.
    for row in rows:
        changes = row['transitions'] + [(row['proven'], 'PROVEN')]
        residence = [(reason, right - left) for (left, reason), (right, _) in zip(changes, changes[1:])]
        assert sum(duration for _, duration in residence) == row['proven'] - row['onset']
        assert all(duration >= 0 for _, duration in residence)


@pytest.mark.asyncio
@pytest.mark.parametrize('episode_index', [0, 1], ids=['recorded-1238', 'recorded-1245'])
@pytest.mark.parametrize('enabled', [False, True], ids=['baseline-OFF', 'fixed-ON'])
async def test_recorded_prefix_refusal_real_owned_history_and_CAS(monkeypatch, episode_index, enabled):
    episode = episodes()[episode_index]
    refusal = next(t for t, reason in episode['transitions'] if reason == 'prefix_publication_unproven')
    current = refusal // 60000 * 60000 - 60000
    previous = current - 60000
    bot, clock, sources = await gap.seeded(monkeypatch, symbols=('NCPL',), last=previous, sized_policy=True)
    bot.settings = bot.settings.model_copy(update={
        'strategy_schwab_1m_v2_keepwatch_transient_rest_hold_enabled': enabled})
    bot.strategy.settings = bot.settings
    state = bot.strategy.watchlist_state('NCPL')
    # The packet omits native identities. These are CONTROLLED inputs only.
    state.resting_active = state.resting_is_broker_order = True
    state.resting_schwab_generation = 'CONTROLLED-unrecorded-rest-generation'
    state.resting_schwab_quantity = 328 if episode_index == 0 else 330
    state.resting_level = state.resting_trigger = 1.82 if episode_index == 0 else 1.81
    state.fanout_segment_id = 1793291104243  # Retained re-place opportunity ID.
    sources['NCPL'].append(gap.bar('NCPL', current))  # CONTROLLED OHLCV.
    ledger = bot._line_sessions['NCPL']
    ledger.observe_closed_live(sources['NCPL'][-3])
    original = ledger.rebuild
    attempts = []

    async def competing_provenance(builder, **kwargs):
        result = await original(builder, **kwargs)
        assert result is not None
        if not attempts:
            # Real immutable result becomes stale after a same-price closed
            # callback adds provenance. No guard/result is replaced by False.
            ledger.observe_closed_live(sources['NCPL'][-2])
            assert result.request.revision != ledger.revision
        attempts.append(result.request)
        return result

    monkeypatch.setattr(ledger, 'rebuild', competing_provenance)

    def provider(_rest, _massive, symbol, anchor, through):
        assert symbol == 'NCPL' and anchor == gap.ANCHOR and through == current
        return gap.history(symbol, sources[symbol], through, clock[0])

    monkeypatch.setattr('project_mai_tai.market_data.keepwatch_history.read_keepwatch_history', provider)
    try:
        clock[0] = refusal
        await bot._keepwatch_history_pass()
        assert ledger.incomplete_reason == 'stale_rebuild'
        assert 'NCPL' in bot._keepwatch_unknown
        assert not bot._keepwatch_entry_allowed('NCPL')
        cancels = [x for x in bot.strategy.drain_pending_intents() if x.intent_type == 'cancel']
        assert len(cancels) == int(not enabled)
        assert state.resting_active is enabled
        assert not bot._keepwatch_history_scopes and not bot._keepwatch_native_reads
        # The next DISTINCT real history operation succeeds; real PROVEN
        # publication retires the dedicated pending policy, without a timer.
        clock[0] = episode['proven']
        await bot._keepwatch_history_pass()
        assert 'NCPL' not in bot._keepwatch_unknown
        assert 'NCPL' not in bot._keepwatch_rest_hold._pending
        assert bot._keepwatch_proven['NCPL'].through_ms == current
        assert bot.strategy._entries_held  # Native/fleet admission is unproved.
        assert not bot._keepwatch_history_scopes and not bot._keepwatch_native_reads
    finally:
        await bot._stop_keepwatch_bar_owners()
