"""Reproduce frozen offline proof and retain each outcome without overwriting logs."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
TEST = "tests/unit/test_nfq2_hotfix1_combined_proof.py"
MUTATIONS = {
    "nfq-tick-sql": ("nfq", "_evaluate_nfq2_holds", "if not self._nfq2_holds:",
        "with self.session_factory() as session:\n        session.execute(select(1))\n    if not self._nfq2_holds:",
        "test_combined_memory_admission_zero_sql"),
    "nfq-eligible-loop-sql": ("nfq", "_evaluate_nfq2_holds",
        "changed = await self._run_db(lambda session: self._nfq2_transition(\n                session, before, after, reason=reason))",
        "with self.session_factory() as session:\n                changed = self._nfq2_transition(session, before, after, reason=reason)\n                session.commit()",
        "test_combined_slow_eligible_persistence_offloop_duplicate_quotes"),
    "drift-tick-sql": ("oms", "_cancel_drifted_working_orders",
        "tolerance_dollars = self._quote_drift_tolerance_dollars()",
        "with self.session_factory() as session:\n        session.execute(select(1))\n    tolerance_dollars = self._quote_drift_tolerance_dollars()",
        "test_combined_memory_admission_zero_sql"),
    "irrelevant-symbol-read": ("nfq", "_evaluate_nfq2_holds", "if not matches:",
        "self._nfq2_reading(symbol)\n    if not matches:",
        "test_combined_memory_admission_zero_sql[irrelevant-active]"),
    "duplicate-inflight": ("nfq", "_evaluate_nfq2_holds", "if key in busy or", "if False or",
        "test_combined_slow_eligible_persistence_offloop_duplicate_quotes"),
    "drift-disabled": ("oms", "_cancel_drifted_working_orders",
        "tolerance_dollars = self._quote_drift_tolerance_dollars()",
        "return\n    tolerance_dollars = self._quote_drift_tolerance_dollars()",
        "test_combined_drift_dispatch_enabled_offloop_and_isolated"),
}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metadata():
    import project_mai_tai.oms.service as oms
    from project_mai_tai.settings import Settings

    assert Path(oms.__file__).resolve().is_relative_to(ROOT / "src"), oms.__file__
    settings = Settings(
        redis_stream_prefix="test", oms_adapter="simulated",
        strategy_schwab_1m_v2_entry_notional_usd=0,
        strategy_schwab_1m_v2_webull_entry_notional_usd=0,
        oms_v2_eh_entry_enabled=True, oms_v2_eh_fresh_price_enabled=True,
        strategy_schwab_1m_v2_slotclear_fresh_flip_enabled=True,
        strategy_schwab_1m_v2_slotclear_fresh_sell_enabled=True,
        strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=True,
        oms_quote_drift_cancel_tolerance_cents=1.0,
        oms_v2_webull_mirror_retained_hold_enabled=True,
        oms_broker_sync_interval_seconds=5)
    tracked = git("ls-files", "src", "tests/unit/test_nfq2_hotfix1_combined_proof.py",
                  "tests/unit/test_nfq2_eh_fresh_price.py", "tests/unit/test_oms_v2_eh_reactive_entry.py",
                  "docs/review-artifacts/nfq2-hotfix1-proof/run_proof.py").splitlines()
    return {"utc": datetime.now(UTC).isoformat(), "head": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"), "worktree": str(ROOT),
        "status": git("status", "--short"), "python": sys.version,
        "executable": sys.executable, "platform": platform.platform(),
        "source_import": oms.__file__,
        "dependencies": {name: importlib.metadata.version(name) for name in
            ("pytest", "pytest-asyncio", "sqlalchemy", "pydantic", "pydantic-settings")},
        "boolean_settings": {key: value for key, value in settings.model_dump().items()
                             if isinstance(value, bool)},
        "numeric_settings": {key: value for key, value in settings.model_dump().items()
                             if type(value) in (int, float)},
        "file_sha256": {path: digest(ROOT / path) for path in tracked},
        "boundaries": ["SQLite memory database, StaticPool; real ORM/SQL; no PostgreSQL",
            "fake Redis xadd; simulated broker, no network",
            "fixed NOW=2026-10-07T12:20:00.439000+00:00; forced AM; real 60s monotonic pacing",
            "one NFQ2 hold; separate same-symbol working limit on paper:drift-proof",
            "stale matched BIYA and fresh irrelevant OTHER alternate at target 240 events/s",
            "real drift reader and cache refresh enabled; no exit positions or armed stops",
            "periodic NFQ2 proof and drift cache refresh only; full broker sync loop not started",
            "initial hold/cache SQL excluded as setup; all measured SQL tagged by work category",
            "fast eligible DB control sleeps 150ms in real worker transition",
            "serial replay forces fillable and bypasses fanout collision for simulated accounts",
            "drift dispatch replay replaces broker boundary with _DirectCancelAdapter only",
            "serial claim/pipeline SQL outside measured tick window remains on-loop in current source",
            "GC remains enabled; no benchmark assertions or thresholds relaxed"]}


def mutated(name):
    import pytest
    from project_mai_tai.oms.eh_fresh_price import EhFreshPriceMixin
    from project_mai_tai.oms.service import OmsRiskService

    target, method, old, new, test = MUTATIONS[name]
    cls = EhFreshPriceMixin if target == "nfq" else OmsRiskService
    original = getattr(cls, method)
    source = textwrap.dedent(inspect.getsource(original))
    assert source.count(old) == 1, (name, source.count(old))
    namespace = dict(original.__globals__)
    exec(compile("from __future__ import annotations\n" + source.replace(old, new),
                 f"<combined-mutation-{name}>", "exec"), namespace)
    setattr(cls, method, namespace[method])
    result = pytest.main(["-p", "no:cacheprovider", "-q", "-s", TEST + "::" + test])
    print(f"MUTATION {name} pytest_rc={result} {'RED' if result == 1 else 'NOT_RED'}")
    return 0 if result == 1 else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["fast", "benchmark", "controls", "suite", "legacy-controls"])
    parser.add_argument("--label", required=True)
    parser.add_argument("--mutation-child", choices=list(MUTATIONS))
    args = parser.parse_args()
    if args.mutation_child:
        return mutated(args.mutation_child)
    assert args.label and all(char.isalnum() or char in "-_" for char in args.label)
    receipt_dir = OUT / args.label
    receipt_dir.mkdir(exist_ok=False)
    before = metadata()
    (receipt_dir / "environment.json").write_text(json.dumps(before, indent=2) + "\n")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    base = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-s"]
    if args.mode in ("fast", "benchmark"):
        commands = [(args.mode, base + [TEST, "-k",
            "60_seconds" if args.mode == "benchmark" else "not 60_seconds"])]
    elif args.mode == "suite":
        commands = [("suite", base + ["tests/unit/test_nfq2_eh_fresh_price.py",
            "tests/unit/test_nfq2_tick_path.py", "tests/unit/test_hotfix1_symbol_tick_cache.py",
            "tests/unit/test_oms_direct_cancel_dead_target_bound.py",
            "tests/unit/test_oms_v2_eh_reactive_entry.py", "tests/unit/test_oms_v2_eh_resting_entry.py",
            "tests/unit/test_all_on_pm.py", "tests/unit/test_keeprest1.py",
            "tests/unit/test_mirrorhold1_retained_hold.py", "tests/unit/test_expected_flags_check.py",
            "tests/unit/test_pmprint1_tonight_flags.py", "tests/unit/test_webull_mirror_eh.py",
            "-k", "not 60_seconds"])]
    elif args.mode == "legacy-controls":
        commands = [(f"N{number}", [sys.executable,
            "docs/review-artifacts/nfq2/check_mutations.py", f"N{number}"])
            for number in range(1, 18)]
    else:
        commands = [(name, [sys.executable, str(Path(__file__).resolve()), "controls",
            "--label", args.label, "--mutation-child", name]) for name in MUTATIONS]
    results = []
    for name, command in commands:
        log = receipt_dir / f"{name}.txt"
        command_start_utc = datetime.now(UTC).isoformat()
        with log.open("x") as output:
            completed = subprocess.run(command, cwd=ROOT, env=env, stdout=output,
                                       stderr=subprocess.STDOUT, check=False)
        command_end_utc = datetime.now(UTC).isoformat()
        text = log.read_text()
        metrics = [json.loads(line.split("COMBINED-60S-RECEIPT ", 1)[1])
                   for line in text.splitlines() if line.startswith("COMBINED-60S-RECEIPT ")]
        results.append({"name": name, "command": command, "exit": completed.returncode,
                        "command_start_utc": command_start_utc,
                        "command_end_utc": command_end_utc,
                        "log": str(log), "sha256": digest(log), "metrics": metrics})
        print(json.dumps(results[-1]), flush=True)
    after = metadata()
    unchanged = before["file_sha256"] == after["file_sha256"] and before["head"] == after["head"]
    result = {"head": before["head"], "tree": before["tree"],
              "end_utc": after["utc"],
              "files_and_head_unchanged": unchanged, "runs": results}
    (receipt_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0 if unchanged and all(row["exit"] == 0 for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
