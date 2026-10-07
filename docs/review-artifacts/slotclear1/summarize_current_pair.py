"""Generate a failed-name comparison without rewriting either raw receipt."""
import hashlib
import json
from pathlib import Path
import re


root = Path(__file__).parent


def read_suite(folder):
    path = root / folder
    metadata = json.loads((path / "result.json").read_text())
    raw = (path / "output.txt").read_text()
    counts = re.findall(r"(\d+) failed, (\d+) passed.*?in ([\d.]+)s", raw)
    failed, passed, seconds = counts[-1]
    # Verbose outcome lines avoid an unraisable-warning concatenated to one
    # baseline short-summary node. Preserve that original warning/raw output.
    names = sorted(set(re.findall(r"^(tests/unit/.+?) FAILED(?:\s+\[|$)", raw, re.M)))
    if len(names) != int(failed) and (path / "test-times.jsonl").exists():
        known = {json.loads(line)["nodeid"] for line in (path / "test-times.jsonl").read_text().splitlines()}
        names = []
        for line in raw.splitlines():
            if line.startswith("FAILED tests/unit/"):
                matches = [node for node in known if line.removeprefix("FAILED ").startswith(node)]
                assert matches, line
                names.append(max(matches, key=len))
        names = sorted(set(names))
    assert len(names) == int(failed), (folder, len(names), failed)
    benchmarks = [json.loads(item) for item in re.findall(r"HOTFIX1_BENCHMARK=(\{[^\n]+\})", raw)]
    return {"head": metadata["head"], "tree": metadata["tree"],
            "start_utc": metadata["start_utc"], "end_utc": metadata["end_utc"],
            "returncode": metadata["returncode"], "failed": int(failed), "passed": int(passed),
            "pytest_seconds": float(seconds), "failed_names": names,
            "output_sha256": hashlib.sha256((path / "output.txt").read_bytes()).hexdigest(),
            "hotfix_benchmarks": benchmarks}


baseline = read_suite("rebased-main-unit")
candidate = read_suite("rebased-candidate-unit-standard")
first_timed = read_suite("rebased-candidate-unit")
times = [json.loads(line) for line in (root / "rebased-candidate-unit/test-times.jsonl").read_text().splitlines()]
gates = [entry for entry in times if "test_recorded_240_events_per_second_60_seconds" in entry["nodeid"]]
report = {"scope": "Sequential local macOS unit pair, not Linux CI / deployment readiness",
          "baseline": baseline, "candidate": candidate,
          "first_timed_launcher_run": first_timed,
          "added_failed_names": sorted(set(candidate["failed_names"]) - set(baseline["failed_names"])),
          "removed_failed_names": sorted(set(baseline["failed_names"]) - set(candidate["failed_names"])),
          "first_timed_hotfix_testcase_utc_bounds": gates,
          "timing_boundary": "First timed UTC bounds include testcase setup/teardown, not internal 60s loop. Standard baseline/candidate retain exact suite UTC only; their HOTFIX gate UTC/metrics are not separately available. First timed launcher had later multiprocessing re-entry failures; raw receipt retained."}
destination = root / "CURRENT_MAIN_PAIR.json"
destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
print(json.dumps({"baseline": [baseline["passed"], baseline["failed"]],
                  "candidate": [candidate["passed"], candidate["failed"]],
                  "added_failed_names": report["added_failed_names"],
                  "removed_failed_names": report["removed_failed_names"],
                  "candidate_hotfix_benchmarks": candidate["hotfix_benchmarks"]}, indent=2))
