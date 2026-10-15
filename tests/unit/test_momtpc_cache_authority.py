"""Controlled provider-prevDay provenance/exclusion controls, no API or native repair."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import MomentumPaperEvent
from project_mai_tai.momentum_paper.case_collision_audit import COLLISION_DAY, apply_reference_audit
from project_mai_tai.momentum_paper.case_collision_widen_tool import (
    prepare_widened_plan, rehearse_widened_copy, retained_snapshot_reference,
)
from tests.unit.test_momtpc_audit_widen import NOW, inputs, widened as widened


def snapshot():
    return dict(scope='CONTROLLED normalized snapshot', redis_id='1791916358294-0', event=dict(
        event_id='00000000-0000-0000-0000-000000000001', source_service='market-data-gateway',
        produced_at='2026-10-13T18:32:38.145784Z', payload=dict(snapshots=[
            {'symbol': 'TpC', 'previous_close': '15.62'}, {'symbol': 'TPC', 'previous_close': '83.49'},
            {'symbol': 'BCpC', 'previous_close': '22.29'}, {'symbol': 'BCPC', 'previous_close': '168.19'},
        ])))


def reference_for(tmp_path, manifest, capture=None):
    path = tmp_path / 'CONTROLLED-snapshot.json'
    path.write_text(json.dumps(capture or snapshot()))
    reference = retained_snapshot_reference(path, '2026-10-12')
    manifest['reference_body_sha256'] = reference['snapshot_sha256']
    return reference


def build(widened, tmp_path, capture=None):
    manifest, _ = inputs(widened)
    reference = reference_for(tmp_path, manifest, capture)
    with widened[0]() as session:
        return prepare_widened_plan(session, manifest, reference, observed_at=NOW)


def test_exact_COMMON_prevDay_source_overlay_and_originals_immutable(widened, tmp_path):
    plan = build(widened, tmp_path)
    assert plan['reference_state'] == 'PREPARED_FROM_RETAINED_PREVDAY'
    audit = plan['records'][-1]['payload']
    assert audit['version'] == 3 and audit['corrections'] == {'TPC': '83.49', 'BCPC': '168.19'}
    assert audit['excluded_symbols'] == [] and audit['source_date'] == '2026-10-12'
    assert audit['source'] == 'massive_snapshot:prevDay.close' and len(audit['source_body_sha256']) == 64
    assert all(b['symbol'] == s and b['provider_field'] == 'prevDay.close'
               and b['retention'] == 'NORMALIZED_PROVIDER_PREVDAY_NOT_RAW_FRAME'
               for s, b in audit['provider_price_bindings'].items())
    with widened[0].begin() as session:
        assert rehearse_widened_copy(session, plan) == 31 and rehearse_widened_copy(session, plan) == 0
    ready = widened[1].latest_session_ready(widened[1].load_session(COLLISION_DAY))
    assert ready.payload['prior_closes'] == {'TPC': '83.49', 'BCPC': '168.19', 'KEEP': '10'}
    with widened[0]() as session:
        original = session.scalar(select(MomentumPaperEvent).where(MomentumPaperEvent.event_type == 'SESSION_READY'))
        assert original.payload['prior_closes'] == {'TPC': '15.62', 'BCPC': '22.29', 'KEEP': '10'}


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'alias', 'NaN', 'fill_field', 'corrupt', 'both_missing'])
def test_unsourced_value_excludes_symbol_TODAY_not_guess_or_reuse_bad_cache(widened, tmp_path, change):
    capture = snapshot()
    records = capture['event']['payload']['snapshots']
    common = next(row for row in records if row['symbol'] == 'TPC')
    if change in {'missing', 'both_missing'}:
        records.remove(common)
        if change == 'both_missing':
            records[:] = [row for row in records if row['symbol'] != 'BCPC']
    elif change == 'duplicate':
        records.append(deepcopy(common))
    elif change == 'alias':
        common['symbol'] = 'TpC'
    elif change == 'fill_field':
        del common['previous_close']
        common['last_trade_price'] = '83.49'
    else:
        common['previous_close'] = 'NaN' if change == 'NaN' else '15.62'
    plan = build(widened, tmp_path, capture)
    assert plan['reference_state'] == 'PREPARED_FROM_RETAINED_PREVDAY_WITH_EXCLUSION'
    with widened[0].begin() as session:
        assert rehearse_widened_copy(session, plan) == 31
    rows = widened[1].load_session(COLLISION_DAY)
    ready = widened[1].latest_session_ready(rows)
    assert 'TPC' not in ready.payload['prior_closes'] and ready.payload['prior_closes']['KEEP'] == '10'
    assert set(ready.payload['case_collision_excluded_symbols']) == (
        {'TPC', 'BCPC'} if change == 'both_missing' else {'TPC'})
    if change != 'both_missing':
        assert ready.payload['prior_closes']['BCPC'] == '168.19'
    tomorrow = replace(ready, session_date=COLLISION_DAY + timedelta(days=1),
                       payload={'prior_closes': {'TPC': '90', 'BCPC': '180'}, 'prior_close_date': '2026-10-13'})
    assert apply_reference_audit(tomorrow, rows) is tomorrow  # No future-session exclusion.


@pytest.mark.parametrize('change', ['hash', 'source_service', 'date', 'naive_clock', 'missing_hash', 'body_tamper'])
def test_unbound_or_wrong_session_snapshot_authority_refuses(widened, tmp_path, change):
    manifest, _ = inputs(widened)
    capture = snapshot()
    if change == 'source_service':
        capture['event']['source_service'] = 'unreviewed'
    elif change in {'date', 'naive_clock'}:
        capture['event']['produced_at'] = ('2026-10-14T18:32:38Z' if change == 'date'
                                         else '2026-10-13T18:32:38')
    reference = reference_for(tmp_path, manifest, capture)
    if change == 'hash':
        manifest['reference_body_sha256'] = '0' * 64
    elif change == 'missing_hash':
        reference['snapshot_sha256'] = None
        manifest['reference_body_sha256'] = None
    elif change == 'body_tamper':
        reference['snapshot_body'] = reference['snapshot_body'].replace('83.49', '82.08')
    with widened[0]() as session, pytest.raises(ValueError):
        prepare_widened_plan(session, manifest, reference, observed_at=NOW)


@pytest.mark.parametrize('change', ['field', 'value', 'rawclaim', 'other_symbol', 'overlap', 'future', 'scope'])
def test_malformed_provider_audit_cannot_overlay_or_exclude(widened, tmp_path, change):
    record = deepcopy(build(widened, tmp_path)['records'][-1])
    payload = record['payload']
    if change in {'field', 'value', 'rawclaim'}:
        payload['provider_price_bindings']['TPC'][{'field': 'provider_field', 'value': 'value',
                                                 'rawclaim': 'retention'}[change]] = 'wrong'
    elif change == 'other_symbol':
        payload['excluded_symbols'] = ['KEEP']
    elif change == 'overlap':
        payload['excluded_symbols'] = ['TPC']
    elif change == 'scope':
        payload['exclusion_scope'] = 'FOREVER'
    record.update(session_date=COLLISION_DAY + timedelta(days=int(change == 'future')), observed_at=NOW)
    with widened[0].begin() as session:
        session.add(MomentumPaperEvent(**record))
    ready = widened[1].latest_session_ready(widened[1].load_session(COLLISION_DAY))
    assert ready.payload['prior_closes'] == {'TPC': '15.62', 'BCPC': '22.29', 'KEEP': '10'}
