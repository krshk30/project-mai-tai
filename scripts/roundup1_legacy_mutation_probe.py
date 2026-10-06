"""In-memory recovery mutations; controls are not historical fill evidence."""

import argparse
import inspect
import textwrap

import pytest

from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from project_mai_tai.strategy_core.legacy_resting import classify_legacy_order


MUTATIONS = {
    "refresh_omitted": (SchwabV2BotService._legacy_resting_refresh,
        "records, readable = await asyncio.to_thread(self._fetch_legacy_resting_orders)",
        "records, readable = [], set()"),
    "unreadable_releases": (SchwabV2Strategy._legacy_resting_owned,
        "if account not in self._legacy_resting_readable:\n        return True",
        "if account not in self._legacy_resting_readable:\n        return False"),
    "unknown_releases": (SchwabV2Strategy._legacy_resting_owned,
        'record.phase in {"unknown", "working"}', 'record.phase in {"working"}'),
    "consumed_reusable": (SchwabV2Strategy._legacy_resting_owned,
        'record.phase == "consumed"', 'False and record.phase == "consumed"'),
    "restored_stop_recalculated": (SchwabV2Strategy._restore_legacy_resting_state,
        'setattr(state, f"resting_{leg}_wire_stop", record.stop)',
        'setattr(state, f"resting_{leg}_wire_stop", self._resting_trigger_for_line(record.level))'),
    "segment_ignored": (SchwabV2Strategy._restore_legacy_resting_state,
        "if record.segment != int(state.fanout_segment_id or 0):", "if False:"),
    "terminal_status_only": (classify_legacy_order,
        'if reports and len(latest_types) == 1 and reports[-1][0] in terminal and _decimal(\n'
        '                    reports[-1][1].get("filled_quantity")) == 0:',
        "if True:"),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name", choices=MUTATIONS)
    args = parser.parse_args()
    function, old, new = MUTATIONS[args.name]
    source = textwrap.dedent(inspect.getsource(function))
    if old not in source:
        raise RuntimeError("mutation site missing")
    namespace = {}
    exec(compile(source.replace(old, new), f"<ROUNDUP1-legacy-{args.name}>", "exec"),
         function.__globals__, namespace)
    function.__code__ = namespace[function.__name__].__code__
    rc = pytest.main(["-q", "-p", "no:cacheprovider",
                      "tests/unit/test_roundup1_legacy_restart.py", "--tb=short"])
    verdict = "RED" if rc == 1 else "SURVIVED" if rc == 0 else "UNMEASURED"
    print(f"mutation={args.name} pytest_rc={rc} verdict={verdict}")
    raise SystemExit(0 if rc == 1 else 1)


if __name__ == "__main__":
    main()
