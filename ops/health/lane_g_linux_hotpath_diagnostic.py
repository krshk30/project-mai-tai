"""Separate profiled runs; never included in the forty benchmark samples."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from lane_g_linux_hotpath_pair import NODE, REFS, TEST

TARGETS = {"_handle_quote_tick_event", "_mirrorhold_schedule",
           "_mirrorhold_price_budget_exhausted", "_mirrorhold_upgrade_budget",
           "_mirrorhold_prior_wires", "_mirrorhold_read", "_mirrorhold_write"}


def child(output):
    import pytest
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    loop_thread = threading.get_ident()
    calls, sql_counts, cache_sizes = Counter(), Counter(), Counter()
    synchronous_spans, sampled_stacks = [], []
    entries = {}
    stop = threading.Event()
    report = {"diagnostic_only": True, "excluded_from_twenty_per_ref": True,
              "method": "Profile target method calls and SQL thread/context; sample loop stack every 5ms. Instrumentation can change timing; no GC policy change."}

    def work_label():
        module = sys.modules.get("tests.unit.test_nfq2_hotfix1_combined_proof")
        return module.WORK.get() if module else "unknown"

    def profile(frame, kind, _arg):
        name = frame.f_code.co_name
        if name not in TARGETS:
            return
        if kind == "call":
            label = work_label()
            calls[(name, threading.get_ident(), label)] += 1
            entries[id(frame)] = (time.monotonic(), label)
            if name == "_mirrorhold_schedule":
                owner = frame.f_locals.get("self")
                cache_sizes[len(owner.__dict__.get("_mirrorhold_by_symbol", {}))] += 1
        elif kind == "return":
            start, label = entries.pop(id(frame), (time.monotonic(), "unknown"))
            elapsed = time.monotonic() - start
            if elapsed >= 0.001:
                synchronous_spans.append({"function": name, "seconds": elapsed,
                                          "thread": threading.get_ident(), "label": label})

    def sql(*_args):
        sql_counts[(threading.get_ident(), work_label())] += 1

    def sample():
        while not stop.wait(0.005):
            frame = sys._current_frames().get(loop_thread)
            stack, label = [], None
            while frame is not None:
                if frame.f_code.co_name == "tagged":
                    label = frame.f_locals.get("label")
                stack.append({"file": frame.f_code.co_filename, "line": frame.f_lineno,
                              "function": frame.f_code.co_name})
                frame = frame.f_back
            sampled_stacks.append({"monotonic": time.monotonic(), "label": label, "stack": stack})

    class Receipt:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(self, item):
            sampler = threading.Thread(target=sample, daemon=True)
            event.listen(Engine, "before_cursor_execute", sql)
            sys.setprofile(profile)
            threading.setprofile(profile)
            sampler.start()
            try:
                yield
            finally:
                sys.setprofile(None)
                threading.setprofile(None)
                stop.set()
                sampler.join()
                event.remove(Engine, "before_cursor_execute", sql)

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            result = (yield).get_result()
            if result.when == "call":
                report.update({"outcome": result.outcome,
                               "failure": str(result.longrepr) if result.failed else None})

    code = pytest.main(["-q", "-s", "-p", "no:cacheprovider", "--tb=short",
                        "--junitxml=" + str(output.with_suffix(".xml")), NODE], plugins=[Receipt()])
    report.update({"loop_thread": loop_thread, "exit_code": int(code),
                   "calls": [{"function": name, "thread": thread, "label": label, "count": count}
                             for (name, thread, label), count in sorted(calls.items())],
                   "sql": [{"thread": thread, "label": label, "count": count}
                           for (thread, label), count in sorted(sql_counts.items())],
                   "mirrorhold_schedule_cache_sizes": dict(cache_sizes),
                   "synchronous_spans_ge_1ms": synchronous_spans,
                   "sampled_loop_stacks": sampled_stacks})
    output.write_text(json.dumps(report, indent=2))
    return 0 if report.get("outcome") in {"passed", "failed"} else 2


def run():
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    output = workspace / "evidence-output"
    output.mkdir(exist_ok=True)
    results = []
    for name, ref in REFS.items():
        root = workspace / name
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        assert actual == ref
        receipt = output / f"{name}-diagnostic.json"
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", str(receipt)],
                              cwd=root, env={**os.environ, "PYTHONPATH": str(root / "src"),
                                             "PYTHONDONTWRITEBYTECODE": "1"},
                              capture_output=True, text=True, timeout=60)
        receipt.with_suffix(".log").write_text(proc.stdout + proc.stderr)
        data = json.loads(receipt.read_text())
        data.update({"sha": actual, "ref_name": name,
                     "test_sha256": hashlib.sha256((root / TEST).read_bytes()).hexdigest()})
        receipt.write_text(json.dumps(data, indent=2))
        results.append(data)
        print(json.dumps({"ref": name, "outcome": data["outcome"], "calls": data["calls"],
                          "sql": data["sql"]}), flush=True)
        if proc.returncode:
            return 2
    assert results[0]["test_sha256"] == results[1]["test_sha256"]
    return 0


if __name__ == "__main__":
    raise SystemExit(child(Path(sys.argv[2])) if len(sys.argv) == 3 else run())
