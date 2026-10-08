"""Receipts from the tested real dispatch reservation + adapter/fake SDK path."""
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, Fill
from project_mai_tai.oms.mirror_retained_hold import SNAPSHOT_TYPE


receipts = []


class ReplayReceipt:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = (yield).get_result()
        if report.when != "call":
            return
        lane = item.funcargs["lane"]
        service, client, factory, clock = lane
        with factory() as session:
            owner = session.scalar(select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE))
            data = owner.payload
            orders = session.scalars(select(BrokerOrder)).all()
            fills = session.scalars(select(Fill)).all()
        receipts.append({"nodeid": item.nodeid, "outcome": report.outcome,
            "clock_utc": clock[0].isoformat(), "identity": data["identity"], "phase": data["phase"],
            "controlled_sdk_place_calls": client.calls.get("place", 0),
            "dispatch_client": data["dispatch_client"], "recorded_historical_orders_loaded": len(orders),
            "recorded_fill_ids_loaded": [str(fill.id) for fill in fills],
            "scope": "real per-intent dispatch reservation and real adapter with fake SDK; ACK CONTROLLED; no new historical/live fill",
            "failure": str(report.longrepr) if report.failed else None})


code = pytest.main(["-q", "-p", "no:cacheprovider", "--tb=short",
                   "tests/unit/test_mirrorhold1_session_duplicate_scan.py::test_recorded_dispatch_replay"], plugins=[ReplayReceipt()])
path = Path("/tmp/mirrorholdG-replays.json")
path.write_text(json.dumps({"exit_code": int(code), "replays": receipts}, indent=2))
raise SystemExit(int(code))
