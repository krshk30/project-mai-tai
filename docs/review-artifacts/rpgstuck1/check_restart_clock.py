"""Reproduce the CI wall-clock leak, then falsify the fixture clock binding."""
import os
from pathlib import Path
import subprocess
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "before"
for timezone in (("UTC",) if mode == "regression-mutant" else ("UTC", "America/New_York")):
    for wall in (("2026-10-05T20:05:33+00:00",) if mode == "regression-mutant" else
                 ("2026-10-05T19:44:00+00:00", "2026-10-05T20:05:33+00:00")):
        code = f"""
from datetime import datetime, UTC
import importlib, inspect, time, pytest
time.tzset()
module = importlib.import_module('project_mai_tai.strategy_core.schwab_1m_v2')
wall = datetime.fromisoformat({wall!r})
class HostClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return wall.astimezone(tz or UTC)
module.datetime = HostClock
"""
        if mode in {"mutant", "regression-mutant"}:
            code += """
import sys
from pathlib import Path
sys.path.insert(0, str(Path('tests/unit').resolve()))
from tests.unit import test_pmprint1_tonight_flags as fixture
source = inspect.getsource(fixture)
start = source.index('    # Bind every session gate on the NEW restored strategy, not just _now_ms.')
end = source.index('    state = restarted.watchlist_state', start)
exec(compile(source[:start] + source[end:], fixture.__file__, 'exec'), fixture.__dict__)
import sys
sys.modules['test_pmprint1_tonight_flags'] = fixture
"""
        path, selected = (("tests/unit/test_rpgstuck1_restart_clock.py", "recorded_restart")
            if mode == "regression-mutant" else
            ("tests/unit/test_pmprint1_tonight_flags.py", "current_rpg_off_startup"))
        code += f"\nraise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', {path!r}, '-k', {selected!r}, '--tb=long']))\n"
        run = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": "src", "TZ": timezone, "PYTHONDONTWRITEBYTECODE": "1"})
        raw = run.stdout + run.stderr
        label = timezone.replace("/", "-") + "-" + wall[11:16].replace(":", "")
        Path(f"/tmp/rpgstuck-ci-clock-{mode}-{label}.txt").write_text(raw)
        print(f"{mode} {timezone} host={wall} rc={run.returncode}: " +
              next((line for line in reversed(raw.splitlines()) if "passed" in line or "failed" in line),
                   "NO_TEST_RESULT"), flush=True)
        expected_fail = mode == "regression-mutant" or (mode in {"before", "mutant"} and "20:05" in wall)
        assert run.returncode == int(expected_fail), raw
        if expected_fail:
            failures = 16 if mode == "regression-mutant" else 3
            assert "AssertionError" in raw and f"{failures} failed" in raw
            assert sum(line.startswith("FAILED tests/unit/") for line in raw.splitlines()) == failures
