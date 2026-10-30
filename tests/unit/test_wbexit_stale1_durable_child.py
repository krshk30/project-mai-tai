"""Original row-copy characterization; not live/native or full boot evidence."""
import os
import json
from decimal import Decimal
from pathlib import Path

import pytest

from project_mai_tai.oms import wbexit2
from tests.unit.wbexit_stale1_episode_port import (
    WB2_ID, WB3_ID, seed_episode, snapshot, hydrate,
)


@pytest.fixture
def retained_episode(tmp_path):
    if not os.environ.get('WBSTALE1_EPISODE_SOURCE'):
        pytest.skip('requires the explicitly reviewed private C1 episode export')
    case = seed_episode(tmp_path)
    try:
        yield case
    finally:
        case.engine.dispose()


@pytest.mark.asyncio
async def test_original24_keeps_wb2_hold_despite_real_durable_child_copy(retained_episode, monkeypatch, record_property):
    case = retained_episode
    original = json.loads((Path(__file__).parents[1] / 'fixtures/wbexit_stale1_original24_finish.json').read_text())
    assert original['head'] == '24d80deb1301cd3bf4c68f4efda98cd368a6716c'
    namespace = dict(wbexit2.__dict__)
    namespace['_now'] = lambda: case.now
    exec(compile(original['finish_call'], '<pinned-original24-finish_call>', 'exec'), namespace)
    monkeypatch.setattr(wbexit2, 'finish_call', namespace['finish_call'])
    monkeypatch.setattr(wbexit2, '_now', lambda: case.now)
    before = snapshot(case.factory)
    assert before['system_incidents'][str(WB2_ID)]['payload']['phase'] == 'HOLD'
    assert before['system_incidents'][str(WB3_ID)]['payload']['phase'] == 'RESOLVED'
    assert before['system_incidents'][str(WB3_ID)]['status'] == 'closed'
    assert len(before['fills']) == 2 and len(before['broker_orders']) == 2
    child = next(row for row in before['fills'].values() if row['side'] == 'sell')
    assert child['quantity'] == Decimal(69) and child['price'] == Decimal('4.54')
    await hydrate(case)
    assert not case.service._wbexit3_pending
    await wbexit2.refresh_pending(case.service)
    after = snapshot(case.factory)
    assert after == before
    state = after['system_incidents'][str(WB2_ID)]['payload']
    assert state['phase'] == 'HOLD' and Decimal(state['remaining']) == 69
    assert case.service._wbexit2_pending[('live:orb', 'TNMG')]['owner_id'] == str(WB2_ID)
    assert case.attempts == []
    record_property('OLD24_original_copy', 'HOLD_remaining69_WB3_alreadyResolved_all_rows_unchanged_providerRedis0')
