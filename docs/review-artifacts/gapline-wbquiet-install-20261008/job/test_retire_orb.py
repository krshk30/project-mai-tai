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


def test_orb_already_empty_unrelated_older_cursor_is_not_our_application():
    value = receipt()
    value['before']['owners']['orb'] = '[]'
    value['request_id'] = '3-0'
    with pytest.raises(RuntimeError, match='application cursor unproven'):
        retire.validate_receipt(value)
    value['after']['owners']['_last_applied_id'] = '3-0'
    retire.validate_receipt(value)
    value['after']['owners']['_last_applied_id'] = '4-0'
    retire.validate_receipt(value)


@pytest.mark.parametrize('value', ['malformed', '1', '-1-0', '1--1', '1-2-3', '18446744073709551616-0', None, 3])
def test_malformed_cursor_or_request_refuses(value):
    with pytest.raises(RuntimeError, match='malformed Redis'):
        retire.redis_id(value)


@pytest.mark.asyncio
async def test_empty_orb_waits_for_our_cursor_without_duplicate_publication(monkeypatch):
    value = receipt()
    value['before']['owners']['orb'] = '[]'
    snapshots = [dict(owners={**value['after']['owners'], '_last_applied_id': '2-0'}, state=value['after']['state']),
                 dict(owners={**value['after']['owners'], '_last_applied_id': '3-0'}, state=value['after']['state'])]
    calls = []
    class Redis:
        async def xadd(self, *args, **kwargs):
            calls.append(args)
            return '3-0'
    async def capture(*args):
        return snapshots.pop(0)
    async def sleep(*args):
        return None
    monkeypatch.setattr(retire, 'capture', capture)
    monkeypatch.setattr(retire.asyncio, 'sleep', sleep)
    monkeypatch.setattr(retire, 'identity', lambda: value['after']['state'])
    from project_mai_tai.settings import Settings
    result = await retire.publish(Redis(), Settings(_env_file=None, market_data_subscription_startup_enabled=True),
        value['before'], now=datetime(2026, 10, 8, 21, tzinfo=timezone.utc))
    assert len(calls) == 1 and not snapshots
    assert result['after']['owners']['_last_applied_id'] == result['request_id'] == '3-0'


def test_first_write_authorization_not_new_clock_abort_after_midnight():
    assert retire.started_in_window(dict(at_utc='2026-10-08T23:00:00+00:00', approved_sha='a' * 40, attempt='exclusive'))
    assert not retire.started_in_window(dict(at_utc='2026-10-08T19:59:00+00:00', approved_sha='a' * 40, attempt='exclusive'))


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
