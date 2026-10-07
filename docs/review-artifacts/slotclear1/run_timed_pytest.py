"""Observe testcase UTC bounds/current node without changing test gates or trading code."""
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

import pytest


target = Path(sys.argv[1]).resolve()
target.parent.mkdir(parents=True, exist_ok=True)


class ClockReceipt:
    def append(self, event, nodeid):
        with target.open("a") as stream:
            stream.write(json.dumps({"event": event, "nodeid": nodeid,
                                     "utc": datetime.now(UTC).isoformat()}) + "\n")

    def pytest_runtest_logstart(self, nodeid, location):
        self.append("test_start", nodeid)

    def pytest_runtest_logfinish(self, nodeid, location):
        self.append("test_end", nodeid)


raise SystemExit(pytest.main(sys.argv[2:], plugins=[ClockReceipt()]))
