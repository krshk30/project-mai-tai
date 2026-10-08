"""Isolated evidence branch only; unchanged test, serial exact-SHA Linux pair."""
import gc
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

TEST = "tests/unit/test_nfq2_hotfix1_combined_proof.py"
NODE = TEST + "::test_combined_slow_eligible_persistence_offloop_duplicate_quotes"
REFS = {"main": "1e15adb03c3758e647d333828bbe3090e24e832e",
        "head": "031be6334c048cfafeec9b826d0cc0ee2c6d1c72"}


def child(output):
    import pytest
    report = {}
    gc_intervals = []
    started = {}

    def observe(phase, info):
        key = info["generation"]
        if phase == "start":
            started[key] = time.monotonic()
        elif key in started:
            gc_intervals.append({"generation": key, "seconds": time.monotonic() - started.pop(key)})

    class Receipt:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            result = (yield).get_result()
            if result.when != "call":
                return
            frames = []
            values = {}
            if call.excinfo:
                for entry in call.excinfo.traceback:
                    frame = entry.frame
                    frames.append({"file": str(frame.code.path), "line": entry.lineno + 1,
                                   "function": frame.code.name})
                    locals_ = frame.raw.f_locals
                    for key in ("duplicate_seconds", "gate_start_utc", "gate_end_utc"):
                        if key in locals_:
                            values[key] = locals_[key]
                    meter = locals_.get("meter")
                    if meter is not None:
                        values["meter"] = meter.counts()
            report.update({"outcome": result.outcome, "failure_frames": frames,
                           "failure_timing": values,
                           "failure": str(result.longrepr) if result.failed else None})

    # Same passive GC observer on both refs; no GC/clock/threshold policy changes.
    gc.callbacks.append(observe)
    try:
        code = pytest.main(["-q", "-s", "-p", "no:cacheprovider", "--tb=short",
                            "--junitxml=" + str(output.with_suffix(".xml")), NODE], plugins=[Receipt()])
    finally:
        gc.callbacks.remove(observe)
    report.update({"exit_code": int(code), "gc_intervals": gc_intervals,
                   "max_observed_gc_seconds": max((r["seconds"] for r in gc_intervals), default=0)})
    output.write_text(json.dumps(report, indent=2))
    return int(code)


def run():
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    output = workspace / "evidence-output"
    output.mkdir(exist_ok=True)
    source_hashes = {}
    for name, ref in REFS.items():
        root = workspace / name
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        assert actual == ref, (name, actual, ref)
        source_hashes[name] = hashlib.sha256((root / TEST).read_bytes()).hexdigest()
    assert source_hashes["main"] == source_hashes["head"]
    environment = {"platform": platform.platform(), "uname": list(platform.uname()),
                   "python": sys.version, "cpu_count": os.cpu_count(),
                   "runner_os": os.environ.get("RUNNER_OS"), "runner_arch": os.environ.get("RUNNER_ARCH"),
                   "image_os": os.environ.get("ImageOS"), "image_version": os.environ.get("ImageVersion"),
                   "packages": {p: importlib.metadata.version(p) for p in
                                ("pytest", "pytest-asyncio", "SQLAlchemy", "pydantic")},
                   "test_sha256": source_hashes, "refs": REFS,
                   "method": "Serial, fresh pytest process per sample; alternating pair order; unchanged test/thresholds; passive GC observer.",
                   "no_production_or_broker_network": True}
    (output / "environment.json").write_text(json.dumps(environment, indent=2))
    results = []
    for index in range(1, 21):
        order = ("main", "head") if index % 2 else ("head", "main")
        for name in order:
            root = workspace / name
            stem = output / f"{name}-{index:02}"
            receipt = stem.with_suffix(".json")
            env = {**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
            command = [sys.executable, str(Path(__file__).resolve()), "--child", str(receipt)]
            start = time.monotonic()
            proc = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=60)
            stem.with_suffix(".log").write_text(proc.stdout + proc.stderr)
            data = json.loads(receipt.read_text()) if receipt.exists() else {"outcome": "missing_receipt"}
            for line in proc.stdout.splitlines():
                if line.startswith("COMBINED-SLOW "):
                    data["passed_timing"] = json.loads(line.removeprefix("COMBINED-SLOW "))
            data.update({"ref_name": name, "sha": REFS[name], "sample": index,
                         "process_exit_code": proc.returncode, "wall_seconds": time.monotonic() - start,
                         "log": str(stem.with_suffix(".log")), "receipt": str(receipt)})
            results.append(data)
            print(json.dumps({"sample": index, "ref": name, "outcome": data["outcome"],
                              "duplicate_seconds": data.get("passed_timing", {}).get("duplicate_seconds",
                                  data.get("failure_timing", {}).get("duplicate_seconds")),
                              "max_gc_seconds": data.get("max_observed_gc_seconds")}), flush=True)
    summary = {name: {"samples": 20,
                      "failed": sum(r["outcome"] == "failed" for r in results if r["ref_name"] == name),
                      "passed": sum(r["outcome"] == "passed" for r in results if r["ref_name"] == name),
                      "other": sum(r["outcome"] not in {"passed", "failed"} for r in results if r["ref_name"] == name)}
               for name in REFS}
    (output / "pair.json").write_text(json.dumps({"environment": environment, "summary": summary,
                                                "results": results}, indent=2))
    print("LINUX_PAIR_SUMMARY=" + json.dumps(summary), flush=True)
    return 0 if all(v["other"] == 0 for v in summary.values()) else 2


if __name__ == "__main__":
    raise SystemExit(child(Path(sys.argv[2])) if len(sys.argv) == 3 and sys.argv[1] == "--child" else run())
