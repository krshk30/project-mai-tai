"""Controlled isolated rows; no supplied IDs/BCPC prices represent actual production targets."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from unittest.mock import AsyncMock
from uuid import UUID

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from project_mai_tai.db.models import MomentumPaperEvent
from project_mai_tai import momentum_page_history as history
from project_mai_tai.momentum_paper.case_collision_audit import REASON, digest
from project_mai_tai.momentum_paper.case_collision_tool import (
    DAY, main, preimage, prepare_plan, rehearse_copy,
)
from project_mai_tai.momentum_paper.report import grade_strategy
from project_mai_tai.momentum_paper.store import MomentumPaperStore, session_record
from project_mai_tai.services import control_plane as cp
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_mompage1 import make_app
from tests.unit.test_momentum_paper_service import _condition_snapshot


NOW = datetime(2026, 10, 13, 18, tzinfo=UTC)


@pytest.fixture
def audit(monkeypatch):
    app, factory = make_app(monkeypatch, records=False)
    monkeypatch.setattr(cp, 'utcnow', lambda: NOW)
    store = MomentumPaperStore(factory)
    ready = session_record(session_date=DAY, observed_at=NOW - timedelta(hours=10),
        prior_close_date=DAY - timedelta(days=1), prior_closes={'TPC': '15.62', 'BCPC': '16.01', 'KEEP': '10'},
        condition_snapshot=_condition_snapshot().payload())
    store.append_many([ready])
    with factory.begin() as session:
        for index, code in enumerate(('momentum_30s', 'momentum_60s', 'momentum_30s')):
            logical = f'CONTROLLED:{code}:TPC:{index}'
            payload = dict(logical_id=logical, strategy_code=code, session_date=DAY.isoformat(), symbol='TPC',
                status='FINAL', quantity=6, fill={'price': '82.08', 'sip_ts_ms': 1791912395000 + index},
                exit={'price': '82.39', 'sip_ts_ms': 1791912995000 + index}, exit_reason='TIME', pnl='1.86',
                pnl_pct='0.377680311890837')
            for event_type in ('FILLED', 'FINAL'):
                session.add(MomentumPaperEvent(event_key=f'{logical}:{event_type}', logical_id=logical,
                    strategy_code=code, event_type=event_type, session_date=DAY, symbol='TPC',
                    observed_at=NOW - timedelta(minutes=10), payload=payload))
    reference = dict(source='/v2/aggs/grouped/locale/us/market/stocks/2026-10-12',
        date='2026-10-12', adjusted=True, acquired_at=NOW.isoformat(),
        results=[{'T': 'TpC', 'c': '15.62'}, {'T': 'TPC', 'c': '83.49'},
                 {'T': 'BCpC', 'c': '16.01'}, {'T': 'BCPC', 'c': '81.12'}])
    with factory() as session:
        filled = session.scalars(select(MomentumPaperEvent).where(MomentumPaperEvent.event_type == 'FILLED')
            .order_by(MomentumPaperEvent.logical_id)).all()
        targets = [r for r in filled if r.logical_id.endswith(':0') or r.logical_id.endswith(':1')]
        manifest = dict(reason=REASON, reviewer='claude-1 controlled review', review_id='CONTROLLED-LATEST16:0x',
            session_date=DAY.isoformat(), session_payload_sha256=digest(ready.payload),
            reference_body_sha256=digest(reference), targets=[dict(event_id=str(r.id), event_key=r.event_key,
                logical_id=r.logical_id, strategy_code=r.strategy_code, payload_sha256=digest(r.payload)) for r in targets])
    factory.configure(info={'environment': 'COPY'})
    return app, factory, store, manifest, reference


def plan_for(audit):
    _, factory, _, manifest, reference = audit
    with factory() as session:
        return prepare_plan(session, manifest, reference, observed_at=NOW)


def apply_copy(audit, plan=None):
    plan = plan or plan_for(audit)
    with audit[1].begin() as session:
        return rehearse_copy(session, plan)


def test_exact_two_append_only_idempotent_retirement_and_saved_cache_overlay(audit):
    _, factory, store, _, _ = audit
    with factory() as session:
        before = {str(row.id): preimage(row) for row in session.scalars(select(MomentumPaperEvent))}
    plan = plan_for(audit)
    assert apply_copy(audit, plan) == 3 and apply_copy(audit, plan) == 0
    with factory() as session:
        rows = session.scalars(select(MomentumPaperEvent)).all()
        assert len(rows) == len(before) + 3
        assert all(preimage(row) == before[str(row.id)] for row in rows if str(row.id) in before)
        invalid = [r for r in rows if r.event_type == 'INVALIDATED']
        assert len(invalid) == 2 and all(r.payload['reason'] == REASON and r.payload['reviewer'] for r in invalid)
        assert {r.payload['target_event_id'] for r in invalid} == {t['event_id'] for t in audit[3]['targets']}
    ready = store.latest_session_ready(store.load_session(DAY))
    assert ready.payload['prior_closes'] == {'TPC': '83.49', 'BCPC': '81.12', 'KEEP': '10'}


def test_page_and_grade_exclude_only_reviewed_ids_not_all_TPC(audit, monkeypatch):
    app, factory, store, _, _ = audit
    assert history.load_completed_today(factory, 'momentum_30s', DAY)[1] == Decimal('3.72')
    assert apply_copy(audit) == 3
    rows, pnl = history.load_completed_today(factory, 'momentum_30s', DAY)
    assert len(rows) == 1 and pnl == Decimal('1.86') and rows[0]['ticker'] == 'TPC'
    assert history.load_completed_today(factory, 'momentum_60s', DAY) == ([], Decimal('0'))
    completed = store.load_completed_since(DAY)
    assert len(completed) == 1 and grade_strategy(completed, complete_sessions=1).filled_gradable == 1
    seen = []
    render = cp._render_bot_detail_page

    def inspect(data, code, **kwargs):
        seen.append(cp._find_bot_view(data, code))
        return render(data, code, **kwargs)

    monkeypatch.setattr(cp, '_render_bot_detail_page', inspect)
    with TestClient(app) as client:
        assert client.get('/bot/momentum-30').status_code == 200
        assert len(seen[-1]['closed_today']) == 1 and seen[-1]['daily_pnl'] == 1.86
        assert client.get('/bot/momentum-60').status_code == 200
        assert seen[-1]['closed_today'] == [] and seen[-1]['daily_pnl'] == 0


@pytest.mark.asyncio
async def test_same_day_prepare_uses_corrected_saved_cache_without_API(audit, monkeypatch):
    assert apply_copy(audit) == 3
    service = MomentumPaperService(Settings(momentum_paper_enabled=True), store=audit[2],
                                   clock=lambda: NOW, redis_client=AsyncMock())

    def no_API(*_args):
        raise AssertionError('must reuse audited saved reference without new source read')

    monkeypatch.setattr(service, '_fetch_reference_inputs', no_API)
    await service._prepare_session(DAY)
    assert service._engine.prior_closes['TPC'] == Decimal('83.49')
    assert service._engine.prior_closes['BCPC'] == Decimal('81.12')
    assert len(service._completed_from_store) == 1
    assert service._completed_from_store[0]['logical_id'].endswith(':2')
    restored = audit[2].load_session(DAY)
    assert len([row for row in restored if row.event_type == 'INVALIDATED']) == 2
    assert len([row for row in restored if row.event_type == 'FINAL']) == 1


@pytest.mark.parametrize('change', ['one_target', 'duplicate', 'reviewer', 'reason', 'target_hash', 'reference_hash',
                                  'alias_only', 'missing_BCPC', 'duplicate_common', 'wrong_source_date'])
def test_missing_or_conflicting_exact_evidence_refuses_plan(audit, change):
    manifest, reference = deepcopy(audit[3]), deepcopy(audit[4])
    if change == 'one_target':
        manifest['targets'].pop()
    elif change == 'duplicate':
        manifest['targets'][1] = manifest['targets'][0]
    elif change == 'reviewer':
        manifest['reviewer'] = ''
    elif change == 'reason':
        manifest['reason'] = 'unreviewed'
    elif change == 'target_hash':
        manifest['targets'][0]['payload_sha256'] = '0' * 64
    elif change == 'reference_hash':
        manifest['reference_body_sha256'] = '0' * 64
    elif change == 'alias_only':
        reference['results'] = [r for r in reference['results'] if r['T'] != 'TPC']
    elif change == 'missing_BCPC':
        reference['results'] = [r for r in reference['results'] if r['T'] != 'BCPC']
    elif change == 'duplicate_common':
        reference['results'].append({'T': 'TPC', 'c': '15.62'})
    else:
        reference['date'] = '2026-10-11'
    if change in {'alias_only', 'missing_BCPC', 'duplicate_common', 'wrong_source_date'}:
        manifest['reference_body_sha256'] = digest(reference)
    with audit[1]() as session, pytest.raises(ValueError):
        prepare_plan(session, manifest, reference, observed_at=NOW)


@pytest.mark.parametrize('change', ['target_id', 'logical_id', 'reviewer', 'reason'])
def test_malformed_audit_cannot_hide_page_trade(audit, change):
    plan = plan_for(audit)
    row = plan['records'][0]
    if change == 'logical_id':
        row['logical_id'] = 'wrong-logical-id'
    else:
        row['payload'][{'target_id': 'target_event_id', 'reviewer': 'reviewer', 'reason': 'reason'}[change]] = ''
    values = {**row, 'session_date': DAY, 'observed_at': NOW}
    with audit[1].begin() as session:
        session.add(MomentumPaperEvent(**values))
    assert history.load_completed_today(audit[1], 'momentum_30s', DAY)[1] == Decimal('3.72')
    assert history.load_completed_today(audit[1], 'momentum_60s', DAY)[1] == Decimal('1.86')


def test_COPY_only_apply_refuses_unapproved_environment(audit):
    plan = plan_for(audit)
    with audit[1]() as session:
        session.info['environment'] = 'live'
        with pytest.raises(ValueError, match='COPY'):
            rehearse_copy(session, plan)


def test_tampered_plan_and_changed_preimages_refuse_without_audit_appends(audit):
    plan = plan_for(audit)
    tampered = deepcopy(plan)
    tampered['records'][-1]['payload']['corrections']['TPC'] = '15.62'
    with audit[1]() as session, pytest.raises(ValueError):
        rehearse_copy(session, tampered)
    with audit[1].begin() as session:
        row = session.get(MomentumPaperEvent, UUID(plan['preimages'][0]['id']))
        row.payload = {**row.payload, 'quantity': 7}
    with audit[1]() as session, pytest.raises(ValueError, match='preimage changed'):
        rehearse_copy(session, plan)
    with audit[1]() as session:
        assert session.scalar(select(MomentumPaperEvent).where(MomentumPaperEvent.event_type == 'INVALIDATED')) is None


def test_native_PG_invalidation_subquery_is_select_only_uuid_bound():
    from sqlalchemy.dialects import postgresql
    from project_mai_tai.momentum_paper.case_collision_audit import invalidated_logical_ids
    sql = str(invalidated_logical_ids().compile(dialect=postgresql.dialect()))
    assert sql.startswith('SELECT') and 'JOIN momentum_paper_events' in sql
    assert 'CAST(' in sql and 'replace(' in sql and 'target_event_id' not in sql
    assert 'DELETE' not in sql and 'UPDATE' not in sql


def test_file_cli_defaults_dryrun_and_never_changes_inputs(audit, tmp_path, monkeypatch):
    with audit[1]() as session:
        capture = [preimage(row) for row in session.scalars(select(MomentumPaperEvent))]
    inputs = {'capture': capture, 'manifest': audit[3], 'reference': audit[4]}
    paths = {}
    for name, value in inputs.items():
        paths[name] = tmp_path / (name + '.json')
        paths[name].write_text(json.dumps(value))
    original = {name: path.read_bytes() for name, path in paths.items()}
    output = tmp_path / 'plan.json'
    argv = ['tool']
    for name, path in paths.items():
        argv += ['--' + name, str(path)]
    argv += ['--output', str(output)]
    monkeypatch.setattr('sys.argv', argv)
    main()
    result = json.loads(output.read_text())
    assert result['COPY_appends'] == 0 and not result['production_execution_authorized']
    assert all(path.read_bytes() == original[name] for name, path in paths.items())
    with pytest.raises(FileExistsError):
        main()
