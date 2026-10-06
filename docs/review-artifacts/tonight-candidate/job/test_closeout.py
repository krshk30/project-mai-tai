"""Close-out mechanics only; no production API or mutation."""
import copy
import json
from pathlib import Path
import pytest
import proof


def test_offhours_no_live_rows_is_not_forged_bar_pass():
    result = proof.offhours_bar_receipt("2026-10-06T00:51:09.736114Z", "2026-10-06T00:53:31Z", 0)
    assert result["verdict"] == "NOT_APPLICABLE_OFFHOURS"
    assert result["market_bar_hole_minutes"] is None
    assert result["process_downtime_seconds"] == pytest.approx(141.263886)
    assert result["delivery"].startswith("UNMEASURED")


@pytest.mark.parametrize("stop,start,count", [
    ("2026-10-05T19:51:00Z", "2026-10-05T19:53:00Z", 0),
    ("2026-10-06T00:51:00Z", "2026-10-06T00:53:00Z", 1),
    ("2026-10-06T00:51:00Z", "2026-10-07T00:53:00Z", 0),
    ("2026-10-06T00:53:00Z", "2026-10-06T00:51:00Z", 0),
])
def test_offhours_receipt_refuses_in_session_rows_next_day_or_reversed(stop, start, count):
    with pytest.raises(proof.Blocked):
        proof.offhours_bar_receipt(stop, start, count)


@pytest.fixture
def recovered():
    # The fixture is the actual pre-install recorded ticket, not an invented price path.
    token = 'faa55c1f-262b-52e2-adcc-280ab1e5e2ff'
    old = json.loads(Path(__file__).with_name('recorded-veea-before.json').read_text())
    job = copy.deepcopy(old)
    job.update(phase='expired', reason='window_closed', local_no_wire=True)
    job['old']['client_order_id'] = proof.RECOVERED[token]
    m = job['old']['metadata']
    m['fanout_attempt_id'] = proof.RECOVERED[token]
    evidence = [{'status': 'rejected', 'account': 'live:orb', 'symbol': 'VEEA', 'quantity': job['old']['quantity'],
                 'payload': {'metadata': copy.deepcopy(m), 'refusal_origin': 'skipped_before_submit',
                             'refusal_code': 'webull_mirror_precheck_deferred'}}]
    return token, old, job, evidence, []


def test_recorded_local_no_wire_recovery_keeps_core_and_requires_db_proof(recovered):
    proof.validate_recovered_identity(*recovered)


@pytest.mark.parametrize('change', ['coid', 'quantity', 'old_metadata', 'slot', 'segment', 'phase', 'wire', 'origin', 'code', 'account', 'generation', 'missing'])
def test_recovery_refuses_unproven_or_changed_identity(recovered, change):
    token, old, job, rows, orders = recovered
    if change == 'coid': job['old']['client_order_id'] += '-foreign'
    elif change == 'quantity': job['old']['quantity'] = '1'
    elif change == 'old_metadata': job['old']['metadata']['rpg_resting_generation'] = 'foreign'
    elif change == 'slot': job['slot'] = 'reclaim'
    elif change == 'segment': job['segment_id'] += 1
    elif change == 'phase': job['phase'] = 'placed'
    elif change == 'wire': orders.append({'id': 'broker-dispatched'})
    elif change == 'origin': rows[0]['payload']['refusal_origin'] = 'broker_reject'
    elif change == 'code': rows[0]['payload']['refusal_code'] = 'client_abort'
    elif change == 'account': rows[0]['account'] = 'live:schwab_1m_v2'
    elif change == 'generation': rows[0]['payload']['metadata']['rpg_resting_generation'] = 'foreign'
    elif change == 'missing': rows.clear()
    with pytest.raises(proof.Blocked):
        proof.validate_recovered_identity(token, old, job, rows, orders)


def test_paper_pin_is_exact_and_active():
    state = dict(MainPID=366242, NRestarts=0, ActiveState='active', SubState='running', ExecMainStartTimestamp=proof.PAPER_START)
    assert proof.approved_paper(state)
    for key, value in [('MainPID', 366243), ('NRestarts', 1), ('ActiveState', 'inactive'), ('ExecMainStartTimestamp', 'other')]:
        changed = dict(state, **{key: value})
        assert not proof.approved_paper(changed)


def test_active_paper_gate_requires_every_catalog_row_and_numeric8():
    flags = json.loads(Path(__file__).with_name('recorded-app-flags.json').read_text())
    numeric = json.loads(Path(__file__).with_name('recorded-app-numeric.json').read_text())
    rows = ['PASS flag='+r['name']+' service='+s for r in flags['flags']+numeric['settings']
            for s in [r['owning_service'], *r.get('also_check_services', [])]]
    text = '\n'.join(rows+['Final call: PASS; checked=147/147 mismatches=0 unknown=0'])
    result = proof.parse_gate(text, 0, flags, numeric, paper_active=True)
    assert result['actual_verdict']=='PASS' and result['checked']==147 and result['numeric_pass']==8
    with pytest.raises(proof.Blocked):
        proof.parse_gate(text.replace('PASS flag=momentum_paper_enabled','UNKNOWN flag=momentum_paper_enabled'), 0, flags, numeric, paper_active=True)


def test_closeout_cannot_run_service_schema_env_or_source_sequence():
    script = Path(__file__).with_name('closeout.sh').read_text()
    for forbidden in ['systemctl start', 'systemctl stop', 'systemctl restart', 'alembic', 'checkout', 'switch --', 'actions.py" env', 'actions.py" migration']:
        assert forbidden not in script
