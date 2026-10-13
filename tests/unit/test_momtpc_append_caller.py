"""Atomic cold caller controls in private SQLite; no production approval/execution."""

from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import json

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import MomentumPaperEvent
from project_mai_tai.momentum_paper.case_collision_append import (
    CollisionAppendApproval, append_reviewed_collision_plan,
)
from project_mai_tai.momentum_paper.case_collision_audit import COLLISION_REASONS, RATIO_RULE, digest
from project_mai_tai.momentum_paper.case_collision_tool import preimage
from project_mai_tai.momentum_paper.case_collision_widen_tool import assess_rows, prepare_widened_plan
from project_mai_tai.momentum_paper.store import MomentumPaperStore, session_record
from tests.unit.test_mompage1 import make_app
from tests.unit.test_momentum_paper_service import _condition_snapshot
from tests.unit.test_momtpc_cache_authority import reference_for
from tests.unit.test_momtpc_audit_widen import COLLISION_DAY, NOW


HEAD = '4' * 40


@pytest.fixture
def caller(monkeypatch, tmp_path):
    _, factory = make_app(monkeypatch, records=False)
    factory.configure(info={'environment': 'COPY'})
    store = MomentumPaperStore(factory)
    store.append_many([session_record(session_date=COLLISION_DAY, observed_at=NOW,
        prior_close_date=COLLISION_DAY - timedelta(days=1), prior_closes={'TPC': '15.62', 'BCPC': '22.29'},
        condition_snapshot=_condition_snapshot().payload())])
    with factory.begin() as session:
        for symbol, code in [('TPC', 'momentum_30s'), ('TPC', 'momentum_60s'), ('BCPC', 'momentum_60s')]:
            logical = 'CONTROLLED:' + code + ':' + symbol
            for kind in ('FILLED', 'FINAL'):
                session.add(MomentumPaperEvent(event_key=logical + ':' + kind, logical_id=logical,
                    strategy_code=code, event_type=kind, session_date=COLLISION_DAY, symbol=symbol,
                    observed_at=NOW, payload={'prior_close': '15.62' if symbol == 'TPC' else '22.29',
                        'fill': {'symbol': symbol, 'price': '82.08' if symbol == 'TPC' else '169.72'}}))
    with factory() as session:
        rows = session.scalars(select(MomentumPaperEvent).order_by(MomentumPaperEvent.event_key)).all()
        ready = next(r for r in rows if r.event_type == 'SESSION_READY')
        manifest = dict(session_date=COLLISION_DAY.isoformat(), scope_rule=RATIO_RULE, population_complete=True,
            reviewer='claude-1', review_id='CONTROLLED-review', session_payload_sha256=digest(ready.payload),
            capture_preimages_sha256=digest([preimage(r) for r in rows]), targets=[
                {k: v for k, v in a.items() if k not in {'state', 'scope_rule', 'ratio_proof', 'native_SIP_cause'}}
                | {'reason': COLLISION_REASONS[a['symbol']]} for a in assess_rows(rows)])
        reference = reference_for(tmp_path, manifest)
        plan = prepare_widened_plan(session, manifest, reference, observed_at=NOW)
    body = json.dumps(plan, indent=2).encode()
    source = reference['snapshot_body'].encode()
    approval = CollisionAppendApproval(sha256(body).hexdigest(), plan['plan_id'], HEAD,
        sha256(source).hexdigest(), manifest['review_id'], writer_quiesced=True)
    return store, body, source, approval


def invoke(caller, **kwargs):
    store, body, source, approval = caller
    return append_reviewed_collision_plan(store, body, approval, price_source_bytes=source,
        current_source_head=kwargs.get('head', HEAD), now=kwargs.get('now', NOW), apply=kwargs.get('apply', True))


def count_audits(store):
    with store.session_factory() as session:
        return len(session.scalars(select(MomentumPaperEvent).where(
            MomentumPaperEvent.event_type.in_(('INVALIDATED', 'SESSION_REFERENCE_CORRECTED')))).all())


def test_cold_transaction_normal_append_idempotent_and_UTC_driver_format_equivalent(caller):
    first = invoke(caller)
    assert first['appended'] == 7 and first['original_rows'] == 7 and first['originals_unchanged']
    assert first['environment'] == 'PRIVATE_COPY' and not first['production_execution']
    second = invoke(caller)
    assert second['appended'] == 0 and second['existing'] == 7 and set(second['audit_ids']) == set(first['audit_ids'])
    store = caller[0]
    ready = store.latest_session_ready(store.load_session(COLLISION_DAY))
    assert ready.payload['prior_closes'] == {'TPC': '83.49', 'BCPC': '168.19'}


def test_caller_default_is_fresh_validation_DRYRUN_without_store_append(caller, monkeypatch):
    store, body, source, approval = caller

    def no_append(*args, **kwargs):
        raise AssertionError('default must not call the normal append API')

    monkeypatch.setattr(store, 'append_many', no_append)
    receipt = append_reviewed_collision_plan(store, body, approval, price_source_bytes=source,
        current_source_head=HEAD, now=NOW)
    assert receipt['mode'] == 'DRY_RUN_FRESH_VALIDATION' and receipt['appended'] == 0
    assert not receipt['production_execution'] and count_audits(store) == 0


@pytest.mark.parametrize('change', ['plan_bytes', 'source_bytes', 'head', 'quiescence', 'expiry', 'non_COPY', 'apply_string'])
def test_cold_approval_boundaries_refuse_before_append(caller, change):
    store, body, source, approval = caller
    kwargs = {}
    if change == 'plan_bytes':
        body += b' '
    elif change == 'source_bytes':
        source += b' '
    elif change == 'head':
        kwargs['head'] = '0' * 40
    elif change == 'quiescence':
        approval = replace(approval, writer_quiesced=False)
    elif change == 'expiry':
        kwargs['now'] = NOW + timedelta(days=1)
    elif change == 'apply_string':
        kwargs['apply'] = 'false'
    else:
        store.session_factory.configure(info={'environment': 'LIVE'})
    with pytest.raises(ValueError):
        invoke((store, body, source, approval), **kwargs)
    assert count_audits(store) == 0


@pytest.mark.parametrize('shift', [False, True])
def test_timezone_rendering_equivalence_is_not_timestamp_change_waiver(caller, shift):
    store, body, source, approval = caller
    plan = json.loads(body)
    for row in plan['preimages']:
        for field in ('observed_at', 'created_at'):
            row[field] += '+00:00'
    if shift:
        plan['preimages'][0]['observed_at'] = '2026-10-13T00:00:00+00:00'
    body = json.dumps(plan).encode()
    approval = replace(approval, plan_sha256=sha256(body).hexdigest())
    if shift:
        with pytest.raises(ValueError, match='preimages changed'):
            invoke((store, body, source, approval))
        assert count_audits(store) == 0
    else:
        assert invoke((store, body, source, approval))['appended'] == 7


@pytest.mark.parametrize('change', ['payload', 'new_native_row', 'partial', 'conflict'])
def test_fresh_native_population_and_prior_audit_set_refuse_changed_bindings(caller, change):
    store, body, _, _ = caller
    plan = json.loads(body)
    with store.session_factory.begin() as session:
        if change == 'payload':
            row = session.scalar(select(MomentumPaperEvent).where(MomentumPaperEvent.event_type == 'FILLED'))
            row.payload = {'changed': True}
        elif change == 'new_native_row':
            session.add(MomentumPaperEvent(event_key='NEW:DETECTED', logical_id='NEW', strategy_code='momentum_30s',
                event_type='DETECTED', session_date=COLLISION_DAY, symbol='TPC', observed_at=NOW, payload={}))
        else:
            r = dict(plan['records'][0])
            r.update(session_date=COLLISION_DAY, observed_at=NOW)
            if change == 'conflict':
                r['payload'] = {'wrong': True}
            session.add(MomentumPaperEvent(**r))
    before = count_audits(store)
    with pytest.raises(ValueError):
        invoke(caller)
    assert count_audits(store) == before


def test_mid_append_failure_rolls_back_whole_batch(caller, monkeypatch):
    store = caller[0]
    append = store.append_many

    def fail(records, *, session):
        append(records, session=session)
        session.flush()
        raise RuntimeError('CONTROLLED failure after staging')

    monkeypatch.setattr(store, 'append_many', fail)
    with pytest.raises(RuntimeError):
        invoke(caller)
    assert count_audits(store) == 0


@pytest.mark.parametrize('change', ['cache_price', 'target_reason'])
def test_even_resealed_bad_content_cannot_bypass_fresh_semantic_source_checks(caller, change):
    store, body, source, approval = caller
    plan = json.loads(body)
    if change == 'cache_price':
        plan['records'][-1]['payload']['corrections']['TPC'] = '82.08'
        plan['records'][-1]['payload']['provider_price_bindings']['TPC']['value'] = '82.08'
    else:
        plan['records'][0]['payload']['reason'] = 'wrong'
    plan['records_sha256'] = digest(plan['records'])
    body = json.dumps(plan).encode()
    approval = replace(approval, plan_sha256=sha256(body).hexdigest())
    with pytest.raises(ValueError):
        invoke((store, body, source, approval))
    assert count_audits(store) == 0
