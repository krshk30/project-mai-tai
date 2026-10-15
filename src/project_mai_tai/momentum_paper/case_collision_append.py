"""Cold reviewed append caller; no DSN/CLI, provider, UPDATE or DELETE interface."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from project_mai_tai.db.models import MomentumPaperEvent
from project_mai_tai.momentum_paper.case_collision_audit import (
    COLLISION_DAY, COLLISION_EVENT_TYPES, COLLISION_REASONS, INVALIDATED,
    REFERENCE_CORRECTED, apply_reference_audit, digest,
)
from project_mai_tai.momentum_paper.case_collision_tool import MAX_ROWS, preimage
from project_mai_tai.momentum_paper.case_collision_widen_tool import _snapshot_corrections, assess_rows
from project_mai_tai.momentum_paper.models import MomentumTapeRecord


@dataclass(frozen=True)
class CollisionAppendApproval:
    plan_sha256: str
    plan_id: str
    source_head: str
    price_source_sha256: str
    review_id: str
    writer_quiesced: bool = False
    production_authorized: bool = False


def _canonical_preimage(value):
    # SQLite COPY drops timezone metadata; its sealed plan came from UTC native
    # exports. Compare UTC-equivalent typed values, not driver-specific formatting.
    result = dict(value)
    for field in ('observed_at', 'created_at'):
        moment = datetime.fromisoformat(result[field])
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        result[field] = moment.astimezone(UTC).isoformat()
    return result


def _record(value):
    return MomentumTapeRecord(**{**value, 'session_date': date.fromisoformat(value['session_date']),
                                'observed_at': datetime.fromisoformat(value['observed_at'])})


def append_reviewed_collision_plan(store, plan_bytes, approval, *, price_source_bytes, current_source_head,
                                   apply=False, now=None):
    """Default fresh-validation DRYRUN; main must explicitly approve a production apply."""
    now = now or datetime.now(UTC)
    cutoff = datetime(2026, 10, 14, 3, 30, tzinfo=ZoneInfo('America/New_York'))
    if (type(apply) is not bool or approval.writer_quiesced is not True or current_source_head != approval.source_head
            or len(approval.source_head) != 40 or now.tzinfo is None or now > cutoff
            or now.date() < COLLISION_DAY or not approval.review_id
            or len(plan_bytes) > 16 * 1024**2
            or len(price_source_bytes) > 16 * 1024**2
            or sha256(price_source_bytes).hexdigest() != approval.price_source_sha256
            or sha256(plan_bytes).hexdigest() != approval.plan_sha256):
        raise ValueError('reviewed source/plan, quiesced writer and unexpired install window required')
    plan = json.loads(plan_bytes)
    records = plan.get('records', [])
    invalid = [r for r in records if r['event_type'] == INVALIDATED]
    reference = [r for r in records if r['event_type'] == REFERENCE_CORRECTED]
    if (plan.get('mode') != 'DRY_RUN_PLAN_ONLY' or plan.get('production_execution_authorized') is not False
            or plan.get('plan_id') != approval.plan_id or digest(records) != plan.get('records_sha256')
            or len(invalid) != plan.get('target_count') or not 1 <= len(invalid) <= MAX_ROWS
            or len(reference) != 1 or len(records) != len(invalid) + 1
            or len({r['event_key'] for r in records}) != len(records)
            or any(r['session_date'] != COLLISION_DAY.isoformat() or r['payload'].get('reviewer') != 'claude-1'
                   or r['payload'].get('review_id') != approval.review_id for r in records)
            or reference[0]['payload'].get('source_body_sha256') != approval.price_source_sha256
            or reference[0]['payload'].get('version') != 3):
        raise ValueError('exact reviewed invalidations plus prevDay cache/exclusion audit required')
    expected = {p['id']: _canonical_preimage(p) for p in plan['preimages']}
    if len(expected) != len(plan['preimages']) or not 1 <= len(expected) <= MAX_ROWS:
        raise ValueError('exact complete original preimages required')
    with store.session_factory.begin() as session:
        dialect = session.get_bind().dialect.name
        copy = dialect == 'sqlite' and session.info.get('environment') == 'COPY'
        if not copy and (dialect != 'postgresql' or (apply and approval.production_authorized is not True)):
            raise ValueError('non-COPY append requires explicit main production approval')
        if dialect == 'postgresql':
            session.connection(execution_options={'isolation_level': 'SERIALIZABLE', 'postgresql_readonly': not apply})
            session.execute(select(func.set_config('lock_timeout', '250ms', True)))
            session.execute(select(func.set_config('statement_timeout', '2000ms', True)))
            if apply:
                lock = int.from_bytes(sha256(b'W3:MOMTPC:2026-10-13').digest()[:8], signed=True)
                if not session.scalar(select(func.pg_try_advisory_xact_lock(lock))):
                    raise ValueError('another collision append writer owns the transaction lock')
        query = select(MomentumPaperEvent).where(
            MomentumPaperEvent.session_date == COLLISION_DAY,
            or_(
                (MomentumPaperEvent.strategy_code.in_(('momentum_30s', 'momentum_60s'))
                 & MomentumPaperEvent.symbol.in_(tuple(COLLISION_REASONS))
                 & MomentumPaperEvent.event_type.in_(COLLISION_EVENT_TYPES)),
                ((MomentumPaperEvent.strategy_code == 'momentum_paper')
                 & (MomentumPaperEvent.event_type == 'SESSION_READY')),
            ),
        ).order_by(MomentumPaperEvent.event_key).limit(MAX_ROWS + 1)
        rows = session.scalars(query.with_for_update(nowait=True) if dialect == 'postgresql' and apply else query).all()
        if dialect == 'postgresql' and any(r.observed_at.tzinfo is None or r.created_at.tzinfo is None for r in rows):
            raise ValueError('native PG timestamp timezone missing; do not assume UTC')
        actual = {str(r.id): _canonical_preimage(preimage(r)) for r in rows}
        if actual != expected:
            raise ValueError('fresh complete native population/preimages changed; refuse append')
        proven = {a['event_id']: a for a in assess_rows(rows) if a['state'] == 'PROVEN_RATIO_COLLISION'}
        if {r['payload']['target_event_id'] for r in invalid} != set(proven):
            raise ValueError('every current ratio-proven event must be exactly bound')
        for r in invalid:
            p = r['payload']
            target = proven[p['target_event_id']]
            if (r['logical_id'] != target['logical_id'] or r['strategy_code'] != target['strategy_code']
                    or r['symbol'] != target['symbol'] or p.get('target_event_key') != target['event_key']
                    or p.get('target_event_type') != target['event_type']
                    or p.get('target_payload_sha256') != target['payload_sha256']
                    or p.get('reason') != COLLISION_REASONS[target['symbol']]
                    or p.get('ratio_proof') != target['ratio_proof']):
                raise ValueError('current native ratio/identity binding mismatch')
        saved = [r for r in rows if r.event_type == 'SESSION_READY']
        if len(saved) != 1:
            raise ValueError('one exact native SESSION_READY required')
        values, bindings, excluded = _snapshot_corrections(rows, saved[0],
            {'reference_body_sha256': approval.price_source_sha256}, dict(
                date=saved[0].payload['prior_close_date'], snapshot_sha256=approval.price_source_sha256,
                snapshot_body=price_source_bytes.decode('utf-8')))
        cache = reference[0]['payload']
        if (cache.get('corrections') != values or cache.get('provider_price_bindings') != bindings
                or cache.get('excluded_symbols') != excluded):
            raise ValueError('reviewed cache values/exclusions do not match retained provider prevDay bytes')
        overlay = apply_reference_audit(store._record(saved[0]), [_record(reference[0])])
        if overlay.payload.get('reference_correction_audit') != reference[0]['event_key']:
            raise ValueError('current saved cache/prior-close authority binding mismatch')
        existing = session.scalars(select(MomentumPaperEvent).where(
            MomentumPaperEvent.event_key.in_([r['event_key'] for r in records]))).all()
        expected_records = {r['event_key']: r for r in records}
        for row in existing:
            r = expected_records[row.event_key]
            if (row.payload != r['payload'] or row.logical_id != r['logical_id']
                    or row.strategy_code != r['strategy_code'] or row.symbol != r['symbol']
                    or row.session_date.isoformat() != r['session_date'] or row.event_type != r['event_type']):
                raise ValueError('conflicting prior audit record; refuse append')
        if existing and len(existing) != len(records):
            raise ValueError('partial prior audit set; main review required, no automatic completion')
        added = 0
        audits = existing
        if apply:
            added = store.append_many([_record(r) for r in records], session=session)
            session.flush()
            if any(_canonical_preimage(preimage(row)) != expected[str(row.id)] for row in rows):
                raise ValueError('original native rows changed inside guarded transaction')
            audits = session.scalars(select(MomentumPaperEvent).where(
                MomentumPaperEvent.event_key.in_(expected_records))).all()
            if len(audits) != len(records):
                raise ValueError('incomplete audit append; roll back')
        receipt = dict(plan_id=approval.plan_id, appended=added, existing=len(existing),
            audit_ids=[str(r.id) for r in audits], original_rows=len(rows), originals_unchanged=True,
            environment='PRIVATE_COPY' if copy else 'MAIN_APPROVED_POSTGRESQL',
            mode='REVIEWED_APPLY' if apply else 'DRY_RUN_FRESH_VALIDATION',
            production_execution=bool(apply and not copy))
    return receipt
