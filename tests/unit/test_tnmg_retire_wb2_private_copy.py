"""Finite Main-only SQLite copy checks; no PostgreSQL/native/install claim."""
from copy import deepcopy
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select, update

from project_mai_tai.db.models import AccountPosition, Fill, SystemIncident
from tests.unit.test_tnmg_retire_wb2_source import retire, CONTROLLED_NOW
from tests.unit.wbexit_stale1_episode_port import seed_episode, snapshot, WB2_ID, WB3_ID


CASES = ('dry_run_rollback', 'apply_then_noop', 'missing_child_fill',
         'partial_child_fill', 'positive_account_position', 'active_invocation',
         'generation_CAS_rollback')


@pytest.mark.parametrize('case_name', CASES)
def test_actual_retained_copy_transaction_guards(tmp_path, monkeypatch, case_name, record_property):
    case = seed_episode(tmp_path)
    try:
        if case_name in {'missing_child_fill', 'partial_child_fill',
                         'positive_account_position', 'active_invocation'}:
            with case.factory.begin() as db:
                if case_name in {'missing_child_fill', 'partial_child_fill'}:
                    child = db.scalar(select(Fill).where(Fill.side == 'sell'))
                    if case_name == 'missing_child_fill': db.delete(child)
                    else: child.quantity = Decimal(68)
                elif case_name == 'positive_account_position':
                    db.scalar(select(AccountPosition)).quantity = Decimal(1)
                else:
                    owner = db.get(SystemIncident, WB2_ID)
                    payload = deepcopy(owner.payload)
                    payload['invocation'] = 'CONTROLLED-live-invocation'
                    owner.payload = payload
        before = snapshot(case.factory)
        if case_name == 'generation_CAS_rollback':
            actual = retire.load_locked_snapshot
            def changed_generation(db):
                captured = actual(db)
                owner = next(row for row in captured['system_incidents'] if row['id'] == retire.WB2)
                changed = deepcopy(owner['payload'])
                changed['generation'] += 1
                db.execute(update(SystemIncident).where(SystemIncident.id == WB2_ID)
                    .values(payload=changed).execution_options(synchronize_session=False))
                return captured
            monkeypatch.setattr(retire, 'load_locked_snapshot', changed_generation)
        if case_name in {'dry_run_rollback', 'apply_then_noop'}:
            with case.factory() as db:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                result = retire.run_transaction(db, apply=case_name == 'apply_then_noop', now=CONTROLLED_NOW)
                if case_name == 'dry_run_rollback':
                    assert result['outcome'] == 'WOULD_RETIRE_ONLY_WB2'
                    db.rollback()
                else:
                    assert result['outcome'] == 'RETIRED_ONLY_WB2_PENDING_COMMIT'
                    db.commit()
            after = snapshot(case.factory)
            if case_name == 'dry_run_rollback':
                assert after == before
            else:
                assert {table for table in before if before[table] != after[table]} == {'system_incidents'}
                assert after['system_incidents'][str(WB3_ID)] == before['system_incidents'][str(WB3_ID)]
                old, new = before['system_incidents'][str(WB2_ID)], after['system_incidents'][str(WB2_ID)]
                assert {key for key in old if old[key] != new[key]} == {'payload', 'status', 'closed_at'}
                assert new['status'] == 'closed' and new['closed_at'] is not None
                payload = new['payload']
                assert payload['remaining'] == '0' and payload['phase'] == 'RESOLVED'
                assert payload['original_attempt_outcome'] == old['payload']['original_attempt_outcome'] == 'FAIL'
                assert payload['originals'] == old['payload']['originals']
                assert payload['cancel_bases'] == old['payload']['cancel_bases'] == []
                assert {key for key in set(payload) | set(old['payload'])
                        if payload.get(key) != old['payload'].get(key)} == {
                    'remaining', 'child_fill_highwater', 'generation', 'phase',
                    'resolved_at', 'updated_at', 'durable_accounting_retirement'}
                assert payload['durable_accounting_retirement']['wire_authority'] is False
                with case.factory() as db:
                    db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                    second = retire.run_transaction(db, apply=True, now='2026-10-30T14:06:00+00:00')
                    assert second['outcome'] == 'ALREADY_RESOLVED_NOOP' and second['update'] is None
                    db.commit()
                assert snapshot(case.factory) == after
        else:
            with pytest.raises(retire.Refusal) as error:
                with case.factory.begin() as db:
                    db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                    retire.run_transaction(db, apply=True, now=CONTROLLED_NOW)
            if case_name == 'generation_CAS_rollback':
                assert str(error.value) == 'owner_compare_and_swap_failed'
            assert snapshot(case.factory) == before
        assert case.attempts == []
        record_property('private_copy_case', case_name)
        record_property('scope', 'CONTROLLED_SQLite_copy_only_no_PG_native_or_install_proof')
    finally:
        case.engine.dispose()
