"""Offline in-memory mutations; never change helper bytes or live policy."""
import importlib.util
import inspect
from pathlib import Path

import pytest

PATH = Path(__file__).with_name("test_v2_offhours_allowance.py")
spec = importlib.util.spec_from_file_location("offhours_tests", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
SOURCE = inspect.getsource(module.gate.admit_v2_offhours)
MUTATIONS = {
    "data-flow": ('or details.get("data_flow") != "stalled_offhours_rest_dry"', "or False"),
    "session": ('or details.get("market_session") not in {"closed", "premarket", "afterhours"}', "or False"),
    "loop-health": ('or details.get("loop_health") != "healthy"', "or False"),
    "exceptions": ('or exceptions != 0', 'or False'),
    "streamer": ('or not (details.get("streamer_connected") is True or details.get("streamer_connected") == "true")', 'or False'),
    "enabled": ('or not (details.get("enabled") is True or details.get("enabled") == "true")', 'or False'),
    "warmed": ('or warmed != watchlist', 'or False'),
    "positive-watchlist": ('or watchlist <= 0', 'or False'),
    "freshness": ('observed = fresh(row.get("observed_at_raw"), now, "published v2 heartbeat")', 'observed = parse_datetime(row.get("observed_at_raw"))'),
    "bar-age-bound": ('or not 0 <= bar_age <= quantity(since_end + 300)', 'or False'),
    "raw-status": ('row.get("raw_status", row.get("status")) != "degraded"', 'False'),
    "in-memory-copy": ('for target in adjusted["services"]:', 'for target in overview["services"]:'),
}


class Mutation:
    def __init__(self, source):
        self.source = source

    def pytest_collection_modifyitems(self, items):
        for item in items:
            namespace = dict(vars(item.module.gate))
            exec(self.source, namespace)
            item.module.gate.admit_v2_offhours = namespace["admit_v2_offhours"]


for name, (old, new) in MUTATIONS.items():
    assert SOURCE.count(old) == 1, name
    rc = pytest.main(["-q", str(PATH), "-p", "no:cacheprovider"],
                     plugins=[Mutation(SOURCE.replace(old, new))])
    print("MUTATION", name, "RED" if rc == 1 else "NOT_ASSERTION_RED", rc, flush=True)
    assert rc == 1, name
