"""Run guard-removal mutants in disposable copies, never the writer checkout."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[3]
RUNTIME = "src/project_mai_tai/oms/atr_reprice_runtime.py"
STRATEGY = "src/project_mai_tai/strategy_core/schwab_1m_v2.py"
BOT = "src/project_mai_tai/services/schwab_1m_v2_bot.py"
TEST = "tests/unit/test_rpgretire1_flag_off.py"


def method_guard(path, method, guard):
    source = (ROOT / path).read_text()
    start = source.index(f"    {'async ' if f'async def {method}' in source else ''}def {method}(")
    end = source.find("\n    def ", start + 1)
    async_end = source.find("\n    async def ", start + 1)
    ends = [value for value in (end, async_end) if value != -1]
    section = source[start:min(ends) if ends else len(source)]
    assert section.count(guard) == 1, method
    return path, section, section.replace(guard, "", 1)


def mutants():
    off = "        if not self._rpg_enabled():\n"
    for method, value in (("_rpg_external_retry", "False"), ("_rpg_begin_cancel", "[]"),
                          ("_rpg_advance", "None"), ("_rpg_retry_jobs", "[]"),
                          ("_rpg_mark_committed_evidence", "[]")):
        yield method, method_guard(RUNTIME, method, off + f"            return {value}\n")
    yield "oms_open_refusal", (RUNTIME, "not self._rpg_enabled() or event.payload.strategy_code",
                              "event.payload.strategy_code")
    yield "oms_old_order_ownership", (RUNTIME, "not self._rpg_enabled() or session is None",
                                     "session is None")
    yield "oms_retry_startup", method_guard(RUNTIME, "_run_rpg_retry_loop", off +
        "            self._rpg_retry_started().set()\n            return\n")
    yield "oms_retry_switch_off", method_guard(RUNTIME, "_run_rpg_retry_loop",
        "            if not self._rpg_enabled():\n                return\n")
    yield "oms_cancel_route", ("src/project_mai_tai/oms/service.py",
                              "and self._rpg_enabled() and event.payload.metadata",
                              "and event.payload.metadata")
    setting = '"strategy_schwab_1m_v2_atr_reprice_handoff_enabled", False'
    guard = f"        if not getattr(self.settings, {setting}):\n"
    yield "v2_restore", method_guard(BOT, "_rpg_handoff_pass", guard + "            return\n")
    yield "v2_authorization", method_guard(STRATEGY, "rpg_handoff_authorization", guard +
        '            return {"at": self._now_ms() / 1000, "verdict": "wait", "reason": "disabled"}\n')
    yield "v2_cached_ownership", method_guard(STRATEGY, "_rpg_entry_owned",
        '        if not getattr(getattr(self, "settings", None),\n'
        f"                       {setting}):\n            return False\n")
    yield "v2_refused_legs", method_guard(STRATEGY, "_rpg_refused_legs",
        f"        if not getattr(settings, {setting}):\n            return set()\n")
    yield "v2_placement_cache", (STRATEGY,
        '        jobs = (getattr(self, "_rpg_handoffs", {}) if getattr(\n'
        f"            settings, {setting}) else {{}})\n",
        '        jobs = getattr(self, "_rpg_handoffs", {})\n')


def main():
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    receipts = []
    for name, (path, before, after) in mutants():
        with tempfile.TemporaryDirectory(prefix=f"rpgretire1-{name}-") as directory:
            clone = Path(directory)
            for tree in ("src", "tests"):
                shutil.copytree(ROOT / tree, clone / tree,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copy(ROOT / "pyproject.toml", clone / "pyproject.toml")
            target = clone / path
            source = target.read_text()
            assert source.count(before) == 1, name
            target.write_text(source.replace(before, after, 1))
            env = {**os.environ, "PYTHONPATH": str(clone / "src")}
            run = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--tb=short"],
                                 cwd=clone, env=env, capture_output=True, text=True)
            log = run.stdout + run.stderr
            (output / f"{name}.log").write_text(log)
            summary = re.findall(r"\d+ failed.* in [^\n]+", log)
            red = run.returncode == 1 and bool(summary) and "ERROR collecting" not in log
            receipts.append({"mutation": name, "red": red, "exit": run.returncode,
                             "summary": summary[-1] if summary else "NO ASSERTION FAILURE",
                             "log_sha256": hashlib.sha256(log.encode()).hexdigest()})
            print(json.dumps(receipts[-1]), flush=True)
    (output / "summary.json").write_text(json.dumps(receipts, indent=2) + "\n")
    return 0 if all(row["red"] for row in receipts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
