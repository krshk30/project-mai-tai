"""Controlled ratio/identity/COPY cases, never production correction prices or target IDs."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import MomentumPaperEvent
from project_mai_tai import momentum_page_history as history
from project_mai_tai.momentum_paper.case_collision_audit import (
    COLLISION_DAY, COLLISION_REASONS, RATIO_RULE, digest,
)
from project_mai_tai.momentum_paper.case_collision_tool import preimage
from project_mai_tai.momentum_paper.case_collision_widen_tool import (
    assess_rows, load_capture, main, prepare_widened_plan, rehearse_widened_copy,
)
from project_mai_tai.momentum_paper.store import MomentumPaperStore, session_record
from tests.unit.test_mompage1 import make_app
from tests.unit.test_momentum_paper_service import _condition_snapshot


NOW = datetime(2026, 10, 13, 22, tzinfo=UTC)


@pytest.fixture
def widened(monkeypatch):
    _, factory = make_app(monkeypatch, records=False)
    factory.configure(info={'environment': 'COPY'})
    store = MomentumPaperStore(factory)
    store.append_many([session_record(session_date=COLLISION_DAY, observed_at=NOW - timedelta(hours=10),
        prior_close_date=COLLISION_DAY - timedelta(days=1),
        prior_closes={'TPC': '15.62', 'BCPC': '22.29', 'KEEP': '10'},
        condition_snapshot=_condition_snapshot().payload())])
    with factory.begin() as session:
        # Three TPC collision pairs, BCPC60 and an unfilled detection. Distinct
        # legitimate, unknown and warning rows are deliberately retained controls.
        groups = [('TPC', code, i, '15.62', '15.68', '82.08')
                  for i in range(3) for code in ('momentum_30s', 'momentum_60s')]
        groups += [('BCPC', 'momentum_60s', 0, '22.29', '22.46', '169.72'),
                   ('TPC', 'momentum_30s', 8, '15.62', '15.68', '82.08'),
                   ('TPC', 'momentum_30s', 9, '83.49', '82', '82.08'),
                   ('BCPC', 'momentum_60s', 9, '168.19', '169', '169.72'),
                   ('TPC', 'momentum_30s_warn', 0, '15.62', '15.68', '82.08')]
        for symbol, code, index, prior, ref, fill in groups:
            logical = f'CONTROLLED:{code}:{symbol}:{index}'
            summary = dict(logical_id=logical, strategy_code=code, session_date=COLLISION_DAY.isoformat(),
                symbol=symbol, prior_close=prior, reference={'symbol': symbol, 'price': ref},
                detect={'symbol': symbol, 'price': fill}, quantity=6, status='FINAL',
                fill={'symbol': symbol, 'price': fill, 'sip_ts_ms': 1791912395000},
                exit={'symbol': symbol, 'price': str(Decimal(fill) + 1), 'sip_ts_ms': 1791912995000},
                exit_reason='TIME', pnl='6', pnl_pct='1')
            types = ('DETECTED', 'NO_FILL') if index == 8 else ('DETECTED', 'FILLED', 'EXITED', 'FINAL')
            if index == 8:
                summary.update(fill=None, exit=None, quantity=None, status='NO_FILL', pnl=None, pnl_pct=None)
            for event_type in types:
                payload = summary if event_type != 'FILLED' else {'fill': summary['fill'], 'quantity': 6}
                session.add(MomentumPaperEvent(event_key=f'{logical}:{event_type}', logical_id=logical,
                    strategy_code=code, event_type=event_type, session_date=COLLISION_DAY, symbol=symbol,
                    observed_at=NOW - timedelta(minutes=1), payload=deepcopy(payload)))
        session.add(MomentumPaperEvent(event_key='UNKNOWN:FINAL', logical_id='UNKNOWN',
            strategy_code='momentum_60s', event_type='FINAL', session_date=COLLISION_DAY,
            symbol='TPC', observed_at=NOW, payload={}))
    return factory, store


def inputs(widened):
    with widened[0]() as session:
        rows = session.scalars(select(MomentumPaperEvent).order_by(MomentumPaperEvent.event_key)).all()
        assessments = assess_rows(rows)
        ready = next(r for r in rows if r.event_type == 'SESSION_READY')
        manifest = dict(session_date=COLLISION_DAY.isoformat(), scope_rule=RATIO_RULE, reviewer='claude-1',
            population_complete=True,
            review_id='CONTROLLED-LATEST18:4x', capture_preimages_sha256=digest([preimage(r) for r in rows]),
            session_payload_sha256=digest(ready.payload), targets=[
                {k: v for k, v in item.items() if k not in {'state', 'scope_rule', 'ratio_proof', 'native_SIP_cause'}}
                | {'reason': COLLISION_REASONS[item['symbol']]}
                for item in assessments if item['state'] == 'PROVEN_RATIO_COLLISION'])
        reference = dict(source='momentum_paper_events:prior_close', date='2026-10-12', prices={})
        for symbol in ('TPC', 'BCPC'):
            good = next(r for r in rows if r.symbol == symbol and r.logical_id.endswith(':9')
                        and r.event_type == 'FINAL')
            reference['prices'][symbol] = dict(event_id=str(good.id), event_key=good.event_key,
                payload_sha256=digest(good.payload), field='prior_close', value=good.payload['prior_close'])
        manifest['reference_body_sha256'] = digest(reference)
    return manifest, reference


def plan_for(widened, *, cache=True):
    manifest, reference = inputs(widened)
    with widened[0]() as session:
        return prepare_widened_plan(session, manifest, reference if cache else None, observed_at=NOW)


def test_widen_ratio_only_exact_event_ids_and_native_cache_overlay_COPY(widened):
    plan = plan_for(widened)
    assert plan['target_count'] == 30  # 3 pairs*4 + BCPC*4 + DETECTED/NO_FILL*2.
    assert len(plan['records']) == 31 and plan['reference_state'] == 'PREPARED_FROM_NATIVE_PRIOR_CLOSE'
    assert {r['payload']['reason'] for r in plan['records'][:-1]} == set(COLLISION_REASONS.values())
    assert all(r['payload']['reviewer'] == 'claude-1' for r in plan['records'])
    assert {'DETECTED', 'FILLED', 'EXITED', 'FINAL', 'NO_FILL'} == {
        r['payload']['target_event_type'] for r in plan['records'][:-1]}
    with widened[0].begin() as session:
        assert rehearse_widened_copy(session, plan) == 31
        assert rehearse_widened_copy(session, plan) == 0
    with widened[0]() as session:
        assert all(preimage(session.get(MomentumPaperEvent, UUID(p['id']))) == p for p in plan['preimages'])
    for code in ('momentum_30s', 'momentum_60s'):
        page, pnl = history.load_completed_today(widened[0], code, COLLISION_DAY)
        assert len(page) == 1 and pnl == 6  # The real common trade stays visible.
    assert len(history.load_warning_completed_today(widened[0], 'momentum_30s_warn', COLLISION_DAY)[0]) == 1
    restored = widened[1].load_session(COLLISION_DAY)
    assert len([r for r in restored if r.event_type == 'INVALIDATED']) == 30
    saved = widened[1].latest_session_ready(restored)
    assert saved.payload['prior_closes'] == {'TPC': '83.49', 'BCPC': '168.19', 'KEEP': '10'}
    assert widened[1].incomplete_logical_ids(restored) == set()
    assert len(widened[1].load_completed_since(COLLISION_DAY)) == 3  # Two good + unknown terminal.


def test_audit_only_plan_cannot_claim_missing_prior_close_rebuilt(widened):
    plan = plan_for(widened, cache=False)
    assert plan['reference_state'].startswith('BLOCKED_MISSING') and len(plan['records']) == 30
    assert not plan['production_execution_authorized']
    with widened[0].begin() as session:
        assert rehearse_widened_copy(session, plan) == 30
    ready = widened[1].latest_session_ready(widened[1].load_session(COLLISION_DAY))
    assert ready.payload['prior_closes']['TPC'] == '15.62'  # Explicitly still not repaired.


@pytest.mark.parametrize('prior,ref,fill,state', [
    ('41.04', '41.04', '82.08', 'NOT_PROVEN'),
    ('41.039', '82', '82.08', 'PROVEN_RATIO_COLLISION'),
    ('83.49', '41.039', '82.08', 'PROVEN_RATIO_COLLISION'),
    ('83.49', '82', '82.08', 'NOT_PROVEN'),
    (None, None, '82.08', 'UNKNOWN'),
    ('NaN', '-1', '82.08', 'UNKNOWN'),
    ('15.62', '15.68', 'NaN', 'UNKNOWN'),
])
def test_strict_half_ratio_and_unknown_not_blanket_symbol_invalidation(prior, ref, fill, state):
    row = MomentumPaperEvent(id=UUID(int=1), event_key='CONTROLLED', logical_id='CONTROLLED',
        strategy_code='momentum_60s', event_type='FINAL', session_date=COLLISION_DAY, symbol='TPC',
        observed_at=NOW, payload={'prior_close': prior, 'reference': {'price': ref}, 'fill': {'price': fill}})
    assert assess_rows([row])[0]['state'] == state


@pytest.mark.parametrize('change', ['empty', 'duplicate', 'reviewer', 'rule', 'day', 'target_hash',
                                  'target_symbol', 'target_type', 'reason', 'capture_hash', 'good_target', 'unknown'])
def test_unreviewed_unproven_or_misbound_target_refuses(widened, change):
    manifest, _ = inputs(widened)
    if change == 'empty':
        manifest['targets'] = []
    elif change == 'duplicate':
        manifest['targets'].append(manifest['targets'][0])
    elif change in {'reviewer', 'rule', 'day', 'capture_hash'}:
        manifest[{'reviewer': 'reviewer', 'rule': 'scope_rule', 'day': 'session_date',
                  'capture_hash': 'capture_preimages_sha256'}[change]] = 'wrong'
    elif change in {'good_target', 'unknown'}:
        with widened[0]() as session:
            assessment = next(a for a in assess_rows(session.scalars(select(MomentumPaperEvent)).all())
                              if a['state'] == ('NOT_PROVEN' if change == 'good_target' else 'UNKNOWN'))
        manifest['targets'][0]['event_id'] = assessment['event_id']
    else:
        manifest['targets'][0][{'target_hash': 'payload_sha256', 'target_symbol': 'symbol',
                               'target_type': 'event_type', 'reason': 'reason'}[change]] = 'wrong'
    with widened[0]() as session, pytest.raises(ValueError):
        prepare_widened_plan(session, manifest, observed_at=NOW)


@pytest.mark.parametrize('change', ['fill', 'reference', 'value', 'hash', 'missing_BCPC', 'date', 'source', 'corrupt'])
def test_missing_exact_daily_prior_close_cannot_be_invented_from_fill(widened, change):
    manifest, reference = inputs(widened)
    binding = reference['prices']['TPC']
    if change in {'fill', 'reference'}:
        binding['field'] = change
    elif change == 'value':
        binding['value'] = '82.08'
    elif change == 'hash':
        binding['payload_sha256'] = '0' * 64
    elif change == 'missing_BCPC':
        del reference['prices']['BCPC']
    elif change in {'date', 'source'}:
        reference[change] = 'wrong'
    else:
        with widened[0]() as session:
            bad = session.scalar(select(MomentumPaperEvent).where(MomentumPaperEvent.symbol == 'TPC',
                MomentumPaperEvent.event_type == 'FINAL', MomentumPaperEvent.logical_id.like('%:0')))
            binding.update(event_id=str(bad.id), event_key=bad.event_key,
                payload_sha256=digest(bad.payload), value=bad.payload['prior_close'])
    manifest['reference_body_sha256'] = digest(reference)
    with widened[0]() as session, pytest.raises(ValueError):
        prepare_widened_plan(session, manifest, reference, observed_at=NOW)


@pytest.mark.parametrize('change', ['reason', 'target_type', 'rule', 'reviewer', 'day', 'proof'])
def test_malformed_widened_audit_cannot_hide_good_common_page(widened, change):
    plan = plan_for(widened)
    record = deepcopy(plan['records'][0])
    if change == 'day':
        record['session_date'] = COLLISION_DAY - timedelta(days=1)
    elif change == 'proof':
        record['session_date'] = COLLISION_DAY
        record['payload']['ratio_proof'] = {}
    else:
        record['session_date'] = COLLISION_DAY
        record['payload'][{'reason': 'reason', 'target_type': 'target_event_type',
                           'rule': 'scope_rule', 'reviewer': 'reviewer'}[change]] = 'wrong'
    record['observed_at'] = NOW
    with widened[0].begin() as session:
        session.add(MomentumPaperEvent(**record))
    assert sum(history.load_completed_today(widened[0], code, COLLISION_DAY)[1]
               for code in ('momentum_30s', 'momentum_60s')) == 54  # 7 bad + 2 good trades.


@pytest.mark.parametrize('change', ['field', 'value', 'missing_key', 'reviewer', 'source'])
def test_malformed_native_reference_audit_does_not_overlay_cache(widened, change):
    record = deepcopy(plan_for(widened)['records'][-1])
    if change in {'field', 'value'}:
        record['payload']['native_price_bindings']['TPC'][change] = 'fill' if change == 'field' else '82.08'
    elif change == 'missing_key':
        del record['payload']['native_price_bindings']['BCPC']
    else:
        record['payload'][change] = 'wrong'
    record.update(session_date=COLLISION_DAY, observed_at=NOW)
    with widened[0].begin() as session:
        session.add(MomentumPaperEvent(**record))
    ready = widened[1].latest_session_ready(widened[1].load_session(COLLISION_DAY))
    assert ready.payload['prior_closes'] == {'TPC': '15.62', 'BCPC': '22.29', 'KEEP': '10'}


def test_widened_scope_subquery_PG_compile_only_no_mutation():
    from sqlalchemy.dialects import postgresql
    from project_mai_tai.momentum_paper.case_collision_audit import invalidated_event_scopes
    statement = invalidated_event_scopes()
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert len(statement.selected_columns) == 4 and sql.startswith('SELECT')
    assert 'JOIN momentum_paper_events' in sql and ' OR ' in sql
    assert 'UPDATE' not in sql and 'DELETE' not in sql and 'INSERT' not in sql


def test_COPY_preimages_hashes_environment_and_count_bounds(widened):
    plan = plan_for(widened)
    tampered = deepcopy(plan)
    tampered['records'][0]['payload']['reason'] = 'wrong'
    with widened[0]() as session, pytest.raises(ValueError):
        rehearse_widened_copy(session, tampered)
    with widened[0]() as session:
        session.info['environment'] = 'LIVE'
        with pytest.raises(ValueError, match='COPY'):
            rehearse_widened_copy(session, plan)
    with widened[0].begin() as session:
        target = session.get(MomentumPaperEvent, UUID(plan['preimages'][0]['id']))
        target.payload = {'changed': True}
    with widened[0]() as session, pytest.raises(ValueError, match='preimage changed'):
        rehearse_widened_copy(session, plan)
    for capture in ({'events': [], 'row_count': 0, 'truncated': False},
                    {'events': [{}], 'row_count': 2, 'truncated': False},
                    {'events': [{}], 'row_count': 1, 'truncated': True},
                    {'events': [{}, {}], 'row_count': 2, 'truncated': False}):
        with pytest.raises(ValueError):
            load_capture(capture)


@pytest.mark.parametrize('change', ['missing_target', 'partial_population'])
def test_incomplete_reviewed_population_refuses_blanket_or_partial_repair(widened, change):
    manifest, _ = inputs(widened)
    if change == 'missing_target':
        manifest['targets'].pop()
    else:
        manifest['population_complete'] = False
    with widened[0]() as session, pytest.raises(ValueError):
        prepare_widened_plan(session, manifest, observed_at=NOW)


def test_exclusion_tuple_preserves_other_strategy_symbol_and_date_with_same_logical_id(widened):
    plan = plan_for(widened)
    target = next(r for r in plan['records'] if r['symbol'] == 'TPC'
                  and r['payload'].get('target_event_type') == 'FINAL')
    with widened[0].begin() as session:
        original = session.get(MomentumPaperEvent, UUID(target['payload']['target_event_id']))
        summary = session.scalar(select(MomentumPaperEvent).where(
            MomentumPaperEvent.logical_id == original.logical_id, MomentumPaperEvent.event_type == 'FINAL'))
        for suffix, code, symbol, day in (
            ('strategy', 'momentum_60s' if original.strategy_code == 'momentum_30s' else 'momentum_30s',
             original.symbol, COLLISION_DAY),
            ('symbol', original.strategy_code, 'KEEP', COLLISION_DAY),
            ('day', original.strategy_code, original.symbol, COLLISION_DAY - timedelta(days=1)),
        ):
            payload = deepcopy(summary.payload)
            payload['prior_close'] = '83.49'
            payload['reference'] = {'symbol': symbol, 'price': '82'}
            payload['fill']['symbol'] = symbol
            session.add(MomentumPaperEvent(event_key='CONTROLLED_REUSED:' + suffix,
                logical_id=original.logical_id, strategy_code=code, event_type='FINAL', symbol=symbol,
                session_date=day, observed_at=NOW, payload=payload))
    # Added rows alter the full preimage guard; they are preserved, not silently
    # hidden by an already-reviewed target with the same textual logical ID.
    manifest, reference = inputs(widened)
    with widened[0]() as session:
        revised = prepare_widened_plan(session, manifest, reference, observed_at=NOW)
    with widened[0].begin() as session:
        rehearse_widened_copy(session, revised)
    completed = widened[1].load_completed_since(COLLISION_DAY - timedelta(days=1))
    assert len(completed) == 6


def test_widened_file_CLI_dryrun_and_COPY_only_with_immutable_inputs(widened, tmp_path, monkeypatch):
    manifest, reference = inputs(widened)
    with widened[0]() as session:
        events = [preimage(row) for row in session.scalars(select(MomentumPaperEvent))]
    values = {'capture': {'events': events, 'row_count': len(events), 'truncated': False},
              'manifest': manifest, 'reference': reference}
    argv = ['tool']
    original = {}
    for name, value in values.items():
        path = tmp_path / (name + '.json')
        path.write_text(json.dumps(value))
        original[path] = path.read_bytes()
        argv += ['--' + name, str(path)]
    for copy in (False, True):
        output = tmp_path / ('copy.json' if copy else 'dryrun.json')
        monkeypatch.setattr('sys.argv', argv + ['--output', str(output)] + (['--rehearse-copy'] if copy else []))
        main()
        result = json.loads(output.read_text())
        assert result['COPY_appends'] == (31 if copy else 0) and not result['production_execution_authorized']
        assert all(path.read_bytes() == before for path, before in original.items())
        with pytest.raises(FileExistsError):
            main()
