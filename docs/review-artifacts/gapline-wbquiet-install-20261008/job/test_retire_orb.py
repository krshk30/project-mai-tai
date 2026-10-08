"""Private operating-system/Redis mechanics, no live subscription writes."""
import copy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

import retire_orb as retire


def receipt():
    owners = {name: json.dumps(['ABC']) for name in retire.OWNERS}
    owners.update(_migration_complete='1', _last_applied_id='1-0')
    after = {**owners, 'orb': '[]', '_last_applied_id': '2-0'}
    return dict(verdict='RETIRED', normal_replace_count=1, request_id='2-0',
                before=dict(owners=owners), after=dict(owners=after, state=dict(
                    MainPID='0', ActiveState='inactive', UnitFileState='disabled')))


def test_only_orb_empty_tombstone_and_cursor_may_change():
    retire.validate_receipt(receipt())


@pytest.mark.parametrize('defect', ['other_owner', 'marker', 'live_orb', 'disabled', 'pid', 'cursor', 'duplicate', 'missing'])
def test_retirement_proof_each_deciding_field(defect):
    value = receipt()
    if defect == 'other_owner':
        value['after']['owners']['orb-schwab'] = '[]'
    elif defect == 'marker':
        value['after']['owners']['_migration_complete'] = '0'
    elif defect == 'live_orb':
        value['after']['owners']['orb'] = '["ABC"]'
    elif defect == 'disabled':
        value['after']['state']['UnitFileState'] = 'enabled'
    elif defect == 'pid':
        value['after']['state']['MainPID'] = '123'
    elif defect == 'cursor':
        value['after']['owners']['_last_applied_id'] = '1-0'
    elif defect == 'duplicate':
        value['normal_replace_count'] = 2
    else:
        del value['after']['owners']['orb-schwab']
    with pytest.raises(RuntimeError):
        retire.validate_receipt(value)


@pytest.mark.asyncio
async def test_approved_service_publishes_one_empty_replace_without_deletion_api(monkeypatch):
    value = receipt()
    calls = []
    class Redis:
        async def xadd(self, key, fields, **kwargs):
            calls.append((key, json.loads(fields['data']), kwargs))
            return '2-0'
    async def capture(*args):
        return copy.deepcopy(value['after'])
    monkeypatch.setattr(retire, 'capture', capture)
    monkeypatch.setattr(retire, 'identity', lambda: value['after']['state'])
    from project_mai_tai.settings import Settings
    settings = Settings(_env_file=None, market_data_subscription_startup_enabled=True)
    result = await retire.publish(Redis(), settings, value['before'],
                                 now=datetime(2026, 10, 8, 21, tzinfo=timezone.utc))
    assert result['normal_replace_count'] == len(calls) == 1
    assert calls[0][1]['payload'] == dict(consumer_name='orb', mode='replace', symbols=[])
    assert not hasattr(retire.OneReplace(SimpleNamespace()), 'hdel')


@pytest.mark.asyncio
async def test_retirement_clock_and_stopped_unit_before_any_publish(monkeypatch):
    class Redis:
        async def xadd(self, *args, **kwargs):
            raise AssertionError('must not write')
    settings = SimpleNamespace(market_data_subscription_startup_enabled=True)
    with pytest.raises(RuntimeError, match='after-close'):
        await retire.publish(Redis(), settings, receipt()['before'], now=datetime(2026, 10, 8, 19, tzinfo=timezone.utc))
    monkeypatch.setattr(retire, 'identity', lambda: dict(MainPID='123', ActiveState='active', UnitFileState='enabled'))
    with pytest.raises(RuntimeError, match='stopped'):
        await retire.publish(Redis(), settings, receipt()['before'], now=datetime(2026, 10, 8, 21, tzinfo=timezone.utc))
