"""Assertion-only, in-memory policy mutations; no source or broker writes."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "strict_flat_readonly.py"
MUTATIONS = {
    "session": ('or details.get("market_session") != expected_session', "or False"),
    "flow": ('or details.get("data_flow") != "stalled_offhours_rest_dry"', "or False"),
    "loop_health": ('or details.get("loop_health") != "healthy"', "or False"),
    "exceptions": ('or exceptions != 0', "or False"),
    "connected": ('or not (details.get("streamer_connected") is True or details.get("streamer_connected") == "true")', "or False"),
    "enabled": ('or not (details.get("enabled") is True or details.get("enabled") == "true")', "or False"),
    "warmup": ('or warmed != watchlist or warmed != warmed.to_integral_value()', "or False"),
    "population": ('or watchlist <= 0 or watchlist != watchlist.to_integral_value()', "or False"),
    "bar_bound": ('or since_end < 0 or not 0 <= bar_age <= quantity(since_end + 300)', "or False"),
    "heartbeat": ('observed = fresh(row.get("observed_at_raw"), now, "published v2 heartbeat")', 'observed = now'),
    "afterhours_anchor": ('anchor_hour = 16 if time(16) <= local.time().replace(tzinfo=None) < time(20) else 20', 'anchor_hour = 20'),
}


def child(name):
    import pytest

    source = SOURCE.read_text()
    old, new = MUTATIONS[name]
    assert source.count(old) == 1, name
    changed = source.replace(old, new)

    class Patch:
        def pytest_collection_modifyitems(self, items):
            seen = set()
            for item in items:
                gate = item.obj.__globals__.get("gate")
                if gate is not None and id(gate) not in seen:
                    seen.add(id(gate))
                    exec(compile(changed, str(SOURCE), "exec"), gate.__dict__)

    return pytest.main([str(ROOT / "test_v2_offhours_allowance.py"), "-q"], plugins=[Patch()])


if __name__ == "__main__":
    if len(sys.argv) == 2:
        raise SystemExit(child(sys.argv[1]))
    receipts = []
    for name in MUTATIONS:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), name],
                                capture_output=True, text=True, timeout=30)
        # A collection/crash error is not evidence of a killed mutation.
        red = result.returncode == 1 and "AssertionError" in result.stdout
        receipts.append({"mutation": name, "rc": result.returncode, "assertion_RED": red})
    print(json.dumps(receipts, indent=2))
    raise SystemExit(0 if all(row["assertion_RED"] for row in receipts) else 1)
