"""Append-only exact-event case-collision audit semantics; no broker or network I/O."""

from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.orm import aliased

from project_mai_tai.db.models import MomentumPaperEvent


REASON = 'case_collision_TpC'
INVALIDATED = 'INVALIDATED'
REFERENCE_CORRECTED = 'SESSION_REFERENCE_CORRECTED'
AUDIT_KIND = 'momtpc_case_collision'
COLLISION_DAY = date(2026, 10, 13)
COLLISION_REASONS = {'TPC': REASON, 'BCPC': 'case_collision_BCpC'}
COLLISION_EVENT_TYPES = ('DETECTED', 'NO_FILL', 'FILLED', 'EXITED', 'FINAL', 'UNANSWERABLE')
RATIO_RULE = 'oct13_reference_lt_half_detect_or_fill'


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def invalidated_logical_ids():
    """Legacy filled bindings or reviewed widened event bindings, never a ticker-wide exclusion."""
    audit, target = aliased(MomentumPaperEvent), aliased(MomentumPaperEvent)
    return select(audit.logical_id).join(target, target.event_key == audit.payload['target_event_key'].as_string()).where(
        audit.event_type == INVALIDATED,
        audit.logical_id == target.logical_id, audit.strategy_code == target.strategy_code,
        audit.session_date == target.session_date, audit.symbol == target.symbol,
        audit.strategy_code.in_(('momentum_30s', 'momentum_60s')),
        audit.payload['audit_kind'].as_string() == AUDIT_KIND,
        or_(
            and_(audit.payload['version'].as_integer() == 1, target.event_type == 'FILLED',
                 audit.symbol == 'TPC', audit.payload['reason'].as_string() == REASON),
            and_(audit.payload['version'].as_integer() == 2, audit.session_date == COLLISION_DAY,
                 target.event_type.in_(COLLISION_EVENT_TYPES),
                 audit.payload['target_event_type'].as_string() == target.event_type,
                 audit.payload['scope_rule'].as_string() == RATIO_RULE,
                 audit.payload['ratio_proof']['numerator']['event_id'].as_string().is_not(None),
                 audit.payload['ratio_proof']['denominator']['event_id'].as_string().is_not(None),
                 audit.payload['reviewer'].as_string() == 'claude-1',
                 func.trim(audit.payload['review_id'].as_string()) != '',
                 or_(*(and_(audit.symbol == symbol, audit.payload['reason'].as_string() == reason)
                       for symbol, reason in COLLISION_REASONS.items()))),
        ),
        audit.payload['reviewer'].as_string().is_not(None),
        func.trim(audit.payload['reviewer'].as_string()) != '',
        func.replace(cast(target.id, String), '-', '') == func.replace(
            audit.payload['target_event_id'].as_string(), '-', ''),
    )


def invalidated_event_scopes():
    """Logical IDs alone are not an account of strategy/symbol/session ownership."""
    query = invalidated_logical_ids()
    audit = query.selected_columns[0].table
    return query.with_only_columns(audit.c.logical_id, audit.c.strategy_code, audit.c.symbol, audit.c.session_date)


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
        source_ok = (p.get('version') == 1 and p.get('source') ==
                     '/v2/aggs/grouped/locale/us/market/stocks/' + str(p.get('source_date')))
        if p.get('version') == 2:
            bindings = p.get('native_price_bindings', {})
            source_ok = (p.get('source') == 'momentum_paper_events:prior_close'
                         and p.get('reviewer') == 'claude-1' and bool(str(p.get('review_id', '')).strip())
                         and row.session_date == COLLISION_DAY and isinstance(bindings, dict)
                         and set(bindings) == set(COLLISION_REASONS)
                         and all(isinstance(b, dict) and b.get('field') == 'prior_close'
                                 and b.get('value') == corrections.get(s)
                                 and bool(b.get('event_id')) and bool(b.get('event_key'))
                                 and isinstance(b.get('payload_sha256'), str) and len(b['payload_sha256']) == 64
                                 for s, b in bindings.items()))
        if (row.session_date != ready.session_date or type(p.get('version')) is not int
                or p.get('audit_kind') != AUDIT_KIND or not str(p.get('reviewer', '')).strip()
                or p.get('target_session_key') != ready.event_key
                or p.get('target_session_payload_sha256') != digest(ready.payload)
                or p.get('source_date') != ready.payload.get('prior_close_date')
                or set(corrections) != {'TPC', 'BCPC'} or not valid_prices
                or not source_ok
                or not isinstance(p.get('source_body_sha256'), str) or len(p['source_body_sha256']) != 64):
            continue
        result = replace(ready, payload={**ready.payload, 'prior_closes': {
            **ready.payload['prior_closes'], **corrections,
        }, 'reference_correction_audit': row.event_key})
    return result
