"""File-only dry-run/COPY rehearsal. No production DSN, provider or execution interface."""

import argparse
from datetime import UTC, date, datetime
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import Base, MomentumPaperEvent
from project_mai_tai.momentum_paper.case_collision_audit import (
    AUDIT_KIND, INVALIDATED, REASON, REFERENCE_CORRECTED, digest,
)


DAY = date(2026, 10, 13)
MAX_ROWS = 20_000


def preimage(row):
    return {c.name: str(getattr(row, c.name)) if c.name in {'id', 'session_date', 'observed_at', 'created_at'}
            else getattr(row, c.name) for c in MomentumPaperEvent.__table__.columns}


def prepare_plan(session, manifest, reference, *, observed_at):
    targets = manifest.get('targets', [])
    if (manifest.get('reason') != REASON or not str(manifest.get('reviewer', '')).strip()
            or not str(manifest.get('review_id', '')).strip() or len(targets) != 2
            or len({t['event_id'] for t in targets}) != 2
            or manifest.get('session_date') != DAY.isoformat()):
        raise ValueError('requires exactly two explicitly reviewed Oct13 TPC targets')
    ready = session.scalar(select(MomentumPaperEvent).where(
        MomentumPaperEvent.session_date == DAY, MomentumPaperEvent.strategy_code == 'momentum_paper',
        MomentumPaperEvent.event_type == 'SESSION_READY').order_by(MomentumPaperEvent.observed_at.desc()).limit(1))
    if ready is None or digest(ready.payload) != manifest.get('session_payload_sha256'):
        raise ValueError('saved session preimage missing or changed')
    prior_day = ready.payload['prior_close_date']
    if (reference.get('date') != prior_day or reference.get('adjusted') is not True
            or reference.get('source') != '/v2/aggs/grouped/locale/us/market/stocks/' + prior_day
            or digest(reference) != manifest.get('reference_body_sha256')
            or not reference.get('acquired_at') or len(reference.get('results', [])) > MAX_ROWS):
        raise ValueError('requires retained exact common-source prior-close evidence/hash')
    corrections = {}
    from decimal import Decimal
    for symbol in ('TPC', 'BCPC'):
        matches = [row for row in reference['results'] if row.get('T') == symbol]
        if len(matches) != 1:
            raise ValueError('missing/duplicate exact common ticker')
        value = Decimal(str(matches[0]['c']))
        if not value.is_finite() or value <= 0:
            raise ValueError('invalid common prior close')
        corrections[symbol] = str(value)
    records, before = [], []
    for target in targets:
        row = session.get(MomentumPaperEvent, UUID(target['event_id']))
        if (row is None or row.symbol != 'TPC' or row.session_date != DAY or row.event_type != 'FILLED'
                or row.strategy_code not in {'momentum_30s', 'momentum_60s'}
                or row.event_key != target.get('event_key') or row.logical_id != target.get('logical_id')
                or row.strategy_code != target.get('strategy_code')
                or digest(row.payload) != target.get('payload_sha256') or not row.payload.get('fill')):
            raise ValueError('exact filled event preimage mismatch')
        before.append(preimage(row))
    if len({r['logical_id'] for r in before}) != 2:
        raise ValueError('not exactly two logical trades')
    before.append(preimage(ready))
    plan_id = digest([manifest, digest(reference), before])
    for target in targets:
        row = session.get(MomentumPaperEvent, UUID(target['event_id']))
        records.append(dict(event_key=f'momtpc:{plan_id}:{row.id}:INVALIDATED', logical_id=row.logical_id,
            strategy_code=row.strategy_code, event_type=INVALIDATED, session_date=DAY.isoformat(), symbol='TPC',
            observed_at=observed_at.isoformat(), payload=dict(version=1, audit_kind=AUDIT_KIND,
                status=INVALIDATED, target_event_id=str(row.id), target_event_key=row.event_key,
                target_payload_sha256=digest(row.payload), reason=REASON, reviewer=manifest['reviewer'],
                review_id=manifest['review_id'], plan_id=plan_id)))
    records.append(dict(event_key=f'momtpc:{plan_id}:REFERENCE_CORRECTED', logical_id=ready.logical_id,
        strategy_code='momentum_paper', event_type=REFERENCE_CORRECTED, session_date=DAY.isoformat(), symbol='*',
        observed_at=observed_at.isoformat(), payload=dict(version=1, audit_kind=AUDIT_KIND,
            target_session_key=ready.event_key, target_session_payload_sha256=digest(ready.payload),
            before={s: ready.payload['prior_closes'].get(s) for s in corrections}, corrections=corrections,
            source=reference['source'], source_date=prior_day, source_body_sha256=digest(reference),
            reviewer=manifest['reviewer'], review_id=manifest['review_id'], plan_id=plan_id)))
    return dict(mode='DRY_RUN_PLAN_ONLY', production_execution_authorized=False, plan_id=plan_id,
                preimages=before, records=records, manifest_sha256=digest(manifest),
                records_sha256=digest(records), reference_body_sha256=digest(reference),
                known_limit='Current live preimages/install/reviewer approval still required')


def rehearse_copy(session, plan):
    if session.info.get('environment') != 'COPY' or session.bind.dialect.name != 'sqlite':
        raise ValueError('COPY SQLite only; no production execution interface')
    records = plan['records']
    if (digest(records) != plan.get('records_sha256')
            or len(records) != 3 or [r['event_type'] for r in records].count(INVALIDATED) != 2
            or [r['event_type'] for r in records].count(REFERENCE_CORRECTED) != 1):
        raise ValueError('exact three audit rows required')
    for before in plan['preimages']:
        row = session.get(MomentumPaperEvent, UUID(before['id']))
        if row is None or preimage(row) != before:
            raise ValueError('preimage changed; refuse append')
    added = 0
    for record in records:
        existing = session.scalar(select(MomentumPaperEvent).where(MomentumPaperEvent.event_key == record['event_key']))
        if existing is not None:
            if existing.payload != record['payload']:
                raise ValueError('conflicting audit key')
            continue
        values = {**record, 'session_date': date.fromisoformat(record['session_date']),
                  'observed_at': datetime.fromisoformat(record['observed_at'])}
        session.add(MomentumPaperEvent(**values))
        added += 1
    session.flush()
    assert all(preimage(session.get(MomentumPaperEvent, UUID(p['id']))) == p for p in plan['preimages'])
    return added


def load_json(path):
    if path.stat().st_size > 16 * 1024**2:
        raise ValueError('16MiB input bound')
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('capture', 'manifest', 'reference', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--rehearse-copy', action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    capture = load_json(args.capture)
    if len(capture) > MAX_ROWS:
        raise ValueError('row bound')
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, info={'environment': 'COPY'})
    with factory.begin() as session:
        for item in capture:
            row = dict(item)
            row.update(id=UUID(row['id']), session_date=date.fromisoformat(row['session_date']),
                       observed_at=datetime.fromisoformat(row['observed_at']),
                       created_at=datetime.fromisoformat(row['created_at']))
            session.add(MomentumPaperEvent(**row))
    with factory.begin() as session:
        plan = prepare_plan(session, load_json(args.manifest), load_json(args.reference), observed_at=datetime.now(UTC))
        plan['COPY_appends'] = rehearse_copy(session, plan) if args.rehearse_copy else 0
    with args.output.open('x') as handle:
        json.dump(plan, handle, indent=2, default=str)
    engine.dispose()


if __name__ == '__main__':
    main()
