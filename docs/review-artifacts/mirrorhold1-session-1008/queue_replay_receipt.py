"""Three requested path replays and separate actual D28 cap controls; no live ACK claim."""
import json
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, Fill
from project_mai_tai.oms.mirror_retained_hold import SNAPSHOT_TYPE

receipts = []


class ReplayReceipt:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = (yield).get_result()
        if report.when != 'call':
            return
        service, client, factory, clock = item.funcargs['lane']
        with factory() as session:
            data = session.scalar(select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE)).payload
            orders = session.scalars(select(BrokerOrder)).all()
            fills = session.scalars(select(Fill)).all()
        caplog = item.funcargs.get('caplog')
        receipts.append({'nodeid': item.nodeid, 'outcome': report.outcome,
            'clock_utc': clock[0].isoformat(), 'identity': data['identity'], 'phase': data['phase'],
            'wire_submissions': data['wire_submissions'], 'price_aggressive_refusals': data['price_aggressive_refusals'],
            'controlled_sdk_place_calls': client.calls.get('place', 0), 'dispatch_client': data['dispatch_client'],
            'orders_after_replay': len(orders), 'actual_fill_ids_loaded': [str(fill.id) for fill in fills],
            'refusal_warnings': [r.message for r in caplog.records if r.levelname == 'WARNING' and 'phase=refused' in r.message] if caplog else [],
            'scope': 'actual recorded order/intent/audit/fill inputs; controlled segment binding and SDK ACK; no new live fill',
            'failure': str(report.longrepr) if report.failed else None})


test = 'tests/unit/test_mirrorhold1_queue_wirecap.py::'
code = pytest.main(['-q', '-p', 'no:cacheprovider', '--tb=short',
    test + 'test_recorded_queue_restore_dispatch_replay',
    test + 'test_current_pending_queue_restore_dispatch_replay',
    test + 'test_recorded_flye_four_accepted_reprices_do_not_cap_next'], plugins=[ReplayReceipt()])
root = Path(__file__).resolve().parents[3]
Path('/tmp/mirrorholdG-queue-replays.json').write_text(json.dumps({'exit_code': int(code),
    'head_at_run': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
    'three_required_replays': receipts[:3], 'separate_D28_controls': receipts[3:]}, indent=2))
raise SystemExit(int(code))
