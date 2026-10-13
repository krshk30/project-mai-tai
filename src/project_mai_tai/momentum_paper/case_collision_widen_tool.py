"""Reviewed Oct13 ratio assessment and file-only DRYRUN/private SQLite COPY plans."""

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import Base, MomentumPaperEvent
from project_mai_tai.momentum_paper.case_collision_audit import (
    AUDIT_KIND, COLLISION_DAY, COLLISION_EVENT_TYPES, COLLISION_REASONS,
    INVALIDATED, RATIO_RULE, REFERENCE_CORRECTED, digest,
)
from project_mai_tai.momentum_paper.case_collision_tool import MAX_ROWS, load_json, preimage


def _positive(value):
    if value is None or len(str(value)) > 128:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _in_scope(row):
    return (row.session_date == COLLISION_DAY and row.symbol in COLLISION_REASONS
            and row.strategy_code in {'momentum_30s', 'momentum_60s'}
            and row.event_type in COLLISION_EVENT_TYPES)


def _binding(row):
    return dict(event_id=str(row.id), event_key=row.event_key, logical_id=row.logical_id,
                strategy_code=row.strategy_code, symbol=row.symbol, event_type=row.event_type,
                payload_sha256=digest(row.payload))


def assess_rows(rows):
    """Missing facts stay UNKNOWN; the approved ratio is not a native SIP-cause claim."""
    if len(rows) > MAX_ROWS:
        raise ValueError('row bound')
    ready = [r for r in rows if r.session_date == COLLISION_DAY
             and r.event_type == 'SESSION_READY' and r.strategy_code == 'momentum_paper']
    groups = {}
    for row in rows:
        if _in_scope(row):
            groups.setdefault((row.logical_id, row.strategy_code, row.symbol), []).append(row)
    results = []
    for siblings in groups.values():
        numerators, denominators = [], []
        for row in siblings:
            for name in ('reference', 'prior_close', 'detect', 'fill'):
                raw = row.payload.get(name)
                if name == 'prior_close':
                    value = _positive(raw)
                else:
                    value = (_positive(raw.get('price')) if isinstance(raw, dict)
                             and raw.get('symbol', row.symbol) == row.symbol else None)
                if value is not None:
                    fact = {**_binding(row), 'field': name, 'price': str(value)}
                    (numerators if name in {'reference', 'prior_close'} else denominators).append(fact)
        # Sparse FILLED payloads lack reference facts. Use the exact saved session only
        # when no retained per-trade prior close exists, never overrule a good trade cache.
        if not any(f['field'] == 'prior_close' for f in numerators) and len(ready) == 1:
            row = ready[0]
            value = _positive(row.payload.get('prior_closes', {}).get(siblings[0].symbol))
            if value is not None:
                numerators.append({**_binding(row), 'field': 'prior_closes.' + siblings[0].symbol,
                                   'price': str(value)})
        proof = next(({'numerator': low, 'denominator': high}
                      for low in numerators for high in denominators
                      if Decimal(low['price']) * 2 < Decimal(high['price'])), None)
        state = 'PROVEN_RATIO_COLLISION' if proof else ('NOT_PROVEN' if numerators and denominators else 'UNKNOWN')
        for row in siblings:
            results.append({**_binding(row), 'state': state, 'scope_rule': RATIO_RULE,
                            'ratio_proof': proof, 'native_SIP_cause': 'UNMEASURED'})
    return sorted(results, key=lambda item: item['event_id'])


def _reference_corrections(rows, ready, manifest, reference):
    if (reference.get('source') != 'momentum_paper_events:prior_close'
            or reference.get('date') != ready.payload.get('prior_close_date')
            or digest(reference) != manifest.get('reference_body_sha256')
            or set(reference.get('prices', {})) != set(COLLISION_REASONS)):
        raise ValueError('requires retained native exact prior-close authority for both keys')
    by_id = {str(row.id): row for row in rows}
    corrections = {}
    for symbol, binding in reference['prices'].items():
        row = by_id.get(binding.get('event_id'))
        if (row is None or row.session_date != COLLISION_DAY or row.symbol != symbol
                or row.strategy_code not in {'momentum_30s', 'momentum_60s'}
                or row.event_key != binding.get('event_key')
                or digest(row.payload) != binding.get('payload_sha256')
                or binding.get('field') != 'prior_close'):
            raise ValueError('native prior-close binding missing; fill/reference is not a daily close')
        value = _positive(row.payload.get('prior_close'))
        if value is None or value != _positive(binding.get('value')):
            raise ValueError('native prior-close value missing or mismatched')
        if row.payload.get('prior_close_date', reference['date']) != reference['date']:
            raise ValueError('native prior-close date mismatch')
        if value == _positive(ready.payload.get('prior_closes', {}).get(symbol)):
            raise ValueError('unchanged collided saved prior close is not a rebuild')
        for event in rows:
            if _in_scope(event) and event.symbol == symbol:
                for name in ('detect', 'fill'):
                    price = event.payload.get(name)
                    observed = _positive(price.get('price')) if isinstance(price, dict) else None
                    if observed is not None and value * 2 < observed:
                        raise ValueError('native prior-close authority still fails the collision ratio')
        corrections[symbol] = str(value)
    return corrections


def retained_snapshot_reference(path, prior_close_date):
    if path.stat().st_size > 16 * 1024**2:
        raise ValueError('16MiB input bound')
    body = path.read_bytes()
    return dict(source='massive_snapshot:prevDay.close', date=prior_close_date,
                snapshot_path=str(path), snapshot_sha256=sha256(body).hexdigest(), snapshot_body=body.decode('utf-8'))


def _snapshot_corrections(rows, ready, manifest, reference):
    raw = reference.get('snapshot_body')
    if (not isinstance(raw, str) or len(raw.encode('utf-8')) > 16 * 1024**2
            or sha256(raw.encode('utf-8')).hexdigest() != reference.get('snapshot_sha256')):
        raise ValueError('retained snapshot bytes do not match the authorized source hash')
    capture = json.loads(raw)
    event = capture.get('event', {})
    if (reference.get('date') != ready.payload.get('prior_close_date')
            or reference.get('date') != '2026-10-12'
            or not isinstance(reference.get('snapshot_sha256'), str) or len(reference['snapshot_sha256']) != 64
            or reference.get('snapshot_sha256') != manifest.get('reference_body_sha256')
            or event.get('source_service') != 'market-data-gateway' or not event.get('event_id')
            or not capture.get('redis_id')):
        raise ValueError('retained exact snapshot source/session binding required')
    produced = datetime.fromisoformat(event['produced_at'].replace('Z', '+00:00'))
    if produced.tzinfo is None or produced.astimezone(ZoneInfo('America/New_York')).date() != COLLISION_DAY:
        raise ValueError('retained snapshot must be from the collision session')
    snapshots = event.get('payload', {}).get('snapshots', [])
    if not isinstance(snapshots, list) or len(snapshots) > MAX_ROWS:
        raise ValueError('snapshot row bound')
    corrections, bindings, excluded = {}, {}, []
    for symbol in COLLISION_REASONS:
        matches = [item for item in snapshots if isinstance(item, dict) and item.get('symbol') == symbol]
        value = _positive(matches[0].get('previous_close')) if len(matches) == 1 else None
        prices = [_positive(row.payload.get(field, {}).get('price'))
                  for row in rows if _in_scope(row) and row.symbol == symbol
                  for field in ('detect', 'fill') if isinstance(row.payload.get(field), dict)]
        if (value is None or value == _positive(ready.payload.get('prior_closes', {}).get(symbol))
                or any(price is not None and value * 2 < price for price in prices)):
            excluded.append(symbol)
            continue
        corrections[symbol] = str(value)
        bindings[symbol] = dict(event_id=event['event_id'], redis_id=capture['redis_id'],
            produced_at=event['produced_at'], symbol=symbol, field='previous_close',
            provider_field='prevDay.close', row_sha256=digest(matches[0]), value=str(value),
            retention='NORMALIZED_PROVIDER_PREVDAY_NOT_RAW_FRAME')
    return corrections, bindings, excluded


def prepare_widened_plan(session, manifest, reference=None, *, observed_at):
    rows = session.scalars(select(MomentumPaperEvent).order_by(MomentumPaperEvent.event_key)
                           .limit(MAX_ROWS + 1)).all()
    if len(rows) > MAX_ROWS:
        raise ValueError('row bound')
    targets = manifest.get('targets', [])
    if (manifest.get('session_date') != COLLISION_DAY.isoformat()
            or manifest.get('scope_rule') != RATIO_RULE or manifest.get('reviewer') != 'claude-1'
            or manifest.get('population_complete') is not True
            or not str(manifest.get('review_id', '')).strip()
            or not isinstance(targets, list) or not 1 <= len(targets) <= MAX_ROWS
            or len({t.get('event_id') for t in targets}) != len(targets)):
        raise ValueError('requires explicit unique reviewed Oct13 ratio targets')
    assessments = assess_rows(rows)
    proven = {item['event_id']: item for item in assessments if item['state'] == 'PROVEN_RATIO_COLLISION'}
    for target in targets:
        item = proven.get(target.get('event_id'))
        if item is None or any(target.get(key) != item[key] for key in (
                'event_id', 'event_key', 'logical_id', 'strategy_code', 'symbol', 'event_type', 'payload_sha256')):
            raise ValueError('exact target binding or proven ratio missing')
        if target.get('reason') != COLLISION_REASONS[item['symbol']]:
            raise ValueError('exact symbol collision reason required')
    if {t['event_id'] for t in targets} != set(proven):
        raise ValueError('reviewed targets must cover every proven event in the complete captured population')
    before = [preimage(row) for row in rows]
    if digest(before) != manifest.get('capture_preimages_sha256'):
        raise ValueError('capture preimages missing or changed')
    ready = [r for r in rows if r.session_date == COLLISION_DAY and r.strategy_code == 'momentum_paper'
             and r.event_type == 'SESSION_READY']
    corrections = None
    snapshot_bindings, excluded = {}, []
    if reference is not None:
        if len(ready) != 1 or digest(ready[0].payload) != manifest.get('session_payload_sha256'):
            raise ValueError('exact saved session preimage missing or changed')
        if reference.get('source') == 'massive_snapshot:prevDay.close':
            corrections, snapshot_bindings, excluded = _snapshot_corrections(rows, ready[0], manifest, reference)
        else:
            corrections = _reference_corrections(rows, ready[0], manifest, reference)
    plan_id = digest([manifest, reference, before])
    records = []
    for target in targets:
        item = proven[target['event_id']]
        records.append(dict(event_key=f"momtpc:{plan_id}:{item['event_id']}:INVALIDATED",
            logical_id=item['logical_id'], strategy_code=item['strategy_code'], event_type=INVALIDATED,
            session_date=COLLISION_DAY.isoformat(), symbol=item['symbol'], observed_at=observed_at.isoformat(),
            payload=dict(version=2, audit_kind=AUDIT_KIND, status=INVALIDATED, scope_rule=RATIO_RULE,
                target_event_id=item['event_id'], target_event_key=item['event_key'],
                target_event_type=item['event_type'], target_payload_sha256=item['payload_sha256'],
                ratio_proof=item['ratio_proof'], reason=target['reason'], reviewer='claude-1',
                review_id=manifest['review_id'], plan_id=plan_id)))
    if corrections is not None:
        saved = ready[0]
        records.append(dict(event_key=f'momtpc:{plan_id}:REFERENCE_CORRECTED', logical_id=saved.logical_id,
            strategy_code='momentum_paper', event_type=REFERENCE_CORRECTED, session_date=COLLISION_DAY.isoformat(),
            symbol='*', observed_at=observed_at.isoformat(), payload=dict(
                version=3 if reference['source'] == 'massive_snapshot:prevDay.close' else 2, audit_kind=AUDIT_KIND,
                target_session_key=saved.event_key, target_session_payload_sha256=digest(saved.payload),
                before={s: saved.payload['prior_closes'].get(s) for s in COLLISION_REASONS}, corrections=corrections,
                source=reference['source'], source_date=reference['date'],
                source_body_sha256=reference.get('snapshot_sha256') or digest(reference),
                **(dict(provider_price_bindings=snapshot_bindings, excluded_symbols=excluded,
                        exclusion_scope='THIS_SESSION_ONLY', snapshot_path=reference['snapshot_path'])
                   if reference['source'] == 'massive_snapshot:prevDay.close' else dict(
                       native_price_bindings={s: {**b, 'value': corrections[s]} for s, b in reference['prices'].items()})),
                reviewer='claude-1', review_id=manifest['review_id'],
                plan_id=plan_id)))
    return dict(mode='DRY_RUN_PLAN_ONLY', production_execution_authorized=False, plan_id=plan_id,
                preimages=before, assessments=assessments, records=records,
                target_count=len(targets), reference_state=(
                    'PREPARED_FROM_RETAINED_PREVDAY_WITH_EXCLUSION' if excluded else
                    'PREPARED_FROM_RETAINED_PREVDAY' if snapshot_bindings else
                    'PREPARED_FROM_NATIVE_PRIOR_CLOSE' if corrections is not None else
                    'BLOCKED_MISSING_EXACT_TPC_AND_BCPC_PRIOR_CLOSE'),
                manifest_sha256=digest(manifest), records_sha256=digest(records),
                reference_body_sha256=(reference.get('snapshot_sha256') or digest(reference)) if reference is not None else None,
                known_limit='Ratio approval is not native SIP-cause proof or live repair authorization')


def rehearse_widened_copy(session, plan):
    if session.info.get('environment') != 'COPY' or session.bind.dialect.name != 'sqlite':
        raise ValueError('COPY SQLite only; no production execution interface')
    records = plan['records']
    invalid = [r for r in records if r['event_type'] == INVALIDATED]
    references = [r for r in records if r['event_type'] == REFERENCE_CORRECTED]
    expected_refs = int(plan['reference_state'].startswith('PREPARED_FROM_'))
    if (digest(records) != plan.get('records_sha256') or len(invalid) != plan['target_count']
            or len(references) != expected_refs or len(records) != len(invalid) + len(references)):
        raise ValueError('exact reviewed audit records required')
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
    if any(preimage(session.get(MomentumPaperEvent, UUID(p['id']))) != p for p in plan['preimages']):
        raise ValueError('original rows changed')
    return added


def load_capture(capture):
    """Accept an explicitly counted uncapped export, never quietly plan from truncated rows."""
    events = capture.get('events', [])
    count = capture.get('row_count')
    if (not isinstance(events, list) or type(count) is not int or count != len(events)
            or not 1 <= count <= MAX_ROWS or capture.get('truncated') is not False):
        raise ValueError('explicit complete count/truncated=false required')
    if len({r.get('id') for r in events}) != count or len({r.get('event_key') for r in events}) != count:
        raise ValueError('duplicate captured row identity')
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('capture', 'manifest', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    authority = parser.add_mutually_exclusive_group()
    authority.add_argument('--reference', type=Path)
    authority.add_argument('--snapshot', type=Path)
    parser.add_argument('--rehearse-copy', action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    events = load_capture(load_json(args.capture))
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, info={'environment': 'COPY'})
    with factory.begin() as session:
        for item in events:
            row = dict(item)
            row.update(id=UUID(row['id']), session_date=date.fromisoformat(row['session_date']),
                       observed_at=datetime.fromisoformat(row['observed_at']),
                       created_at=datetime.fromisoformat(row['created_at']))
            session.add(MomentumPaperEvent(**row))
    with factory.begin() as session:
        reference = load_json(args.reference) if args.reference else None
        if args.snapshot:
            saved = session.scalar(select(MomentumPaperEvent).where(
                MomentumPaperEvent.event_type == 'SESSION_READY', MomentumPaperEvent.session_date == COLLISION_DAY,
                MomentumPaperEvent.strategy_code == 'momentum_paper'))
            if saved is None:
                raise ValueError('saved SESSION_READY required')
            reference = retained_snapshot_reference(args.snapshot, saved.payload['prior_close_date'])
        plan = prepare_widened_plan(session, load_json(args.manifest), reference, observed_at=datetime.now(UTC))
        plan['COPY_appends'] = rehearse_widened_copy(session, plan) if args.rehearse_copy else 0
    with args.output.open('x') as handle:
        json.dump(plan, handle, indent=2, default=str)
    engine.dispose()


if __name__ == '__main__':
    main()
