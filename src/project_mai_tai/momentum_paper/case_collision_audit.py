"""Append-only TPC invalidation/reference audit semantics; no broker or network I/O."""

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import aliased

from project_mai_tai.db.models import MomentumPaperEvent


REASON = 'case_collision_TpC'
INVALIDATED = 'INVALIDATED'
REFERENCE_CORRECTED = 'SESSION_REFERENCE_CORRECTED'
AUDIT_KIND = 'momtpc_case_collision'


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def invalidated_logical_ids():
    """Only an audit bound to the exact filled row can suppress that logical trade."""
    audit, target = aliased(MomentumPaperEvent), aliased(MomentumPaperEvent)
    return select(audit.logical_id).join(target, target.event_key == audit.payload['target_event_key'].as_string()).where(
        audit.event_type == INVALIDATED, target.event_type == 'FILLED',
        audit.logical_id == target.logical_id, audit.strategy_code == target.strategy_code,
        audit.session_date == target.session_date, audit.symbol == target.symbol,
        audit.symbol == 'TPC', audit.strategy_code.in_(('momentum_30s', 'momentum_60s')),
        audit.payload['version'].as_integer() == 1,
        audit.payload['audit_kind'].as_string() == AUDIT_KIND,
        audit.payload['reason'].as_string() == REASON,
        audit.payload['reviewer'].as_string().is_not(None),
        func.trim(audit.payload['reviewer'].as_string()) != '',
        func.replace(cast(target.id, String), '-', '') == func.replace(
            audit.payload['target_event_id'].as_string(), '-', ''),
    )


def apply_reference_audit(ready, rows):
    """Overlay an audited correction on read; the saved SESSION_READY row is immutable."""
    if ready is None:
        return None
    result = ready
    for row in rows:
        if row.event_type != REFERENCE_CORRECTED or row.strategy_code != 'momentum_paper':
            continue
        p = row.payload
        corrections = p.get('corrections', {})
        if not isinstance(corrections, dict):
            continue
        try:
            valid_prices = all(Decimal(str(value)).is_finite() and Decimal(str(value)) > 0
                               for value in corrections.values())
        except (InvalidOperation, ValueError, TypeError):
            continue
        if (row.session_date != ready.session_date or type(p.get('version')) is not int or p.get('version') != 1
                or p.get('audit_kind') != AUDIT_KIND or not str(p.get('reviewer', '')).strip()
                or p.get('target_session_key') != ready.event_key
                or p.get('target_session_payload_sha256') != digest(ready.payload)
                or p.get('source_date') != ready.payload.get('prior_close_date')
                or set(corrections) != {'TPC', 'BCPC'} or not valid_prices
                or p.get('source') != '/v2/aggs/grouped/locale/us/market/stocks/' + str(p.get('source_date'))
                or not isinstance(p.get('source_body_sha256'), str) or len(p['source_body_sha256']) != 64):
            continue
        result = replace(ready, payload={**ready.payload, 'prior_closes': {
            **ready.payload['prior_closes'], **corrections,
        }, 'reference_correction_audit': row.event_key})
    return result
