"""Compare retained full suites, optionally run serial isolated timing controls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import xml.etree.ElementTree as ET

CASES = (
    "tests/unit/test_hotfix1_symbol_tick_cache.py::test_recorded_240_events_per_second_60_seconds_slow_db_flat_transactions[retained-off-existing-row]",
    "tests/unit/test_nfq2_hotfix1_combined_proof.py::test_combined_240_events_per_second_60_seconds_active_hold",
)


def unit(path):
    root = ET.parse(path).getroot()
    names = sorted({row.get("classname", "") + "::" + row.get("name", "") for row in root.iter("testcase")
                    if row.find("failure") is not None or row.find("error") is not None})
    tests = list(root.iter("testcase"))
    skipped = sum(row.find("skipped") is not None for row in tests)
    return {"path": str(path), "failed_names": names,
            "failed_names_sha256": hashlib.sha256("\n".join(names).encode()).hexdigest(),
            "failed": len(names), "passed": len(tests) - len(names) - skipped, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--head", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--control-base", type=Path)
    parser.add_argument("--control-head", type=Path)
    parser.add_argument("--python", type=Path)
    args = parser.parse_args()
    base, head = unit(args.baseline), unit(args.head)
    result = {"baseline": base, "head": head,
              "base_only": sorted(set(base["failed_names"]) - set(head["failed_names"])),
              "head_only": sorted(set(head["failed_names"]) - set(base["failed_names"])),
              "isolated_controls": [], "causal_flakiness_claim": False}
    if args.control_base or args.control_head or args.python:
        if not all((args.control_base, args.control_head, args.python)):
            parser.error("all three isolated-control arguments are required")
        for label, worktree in (("base", args.control_base), ("head", args.control_head)):
            env = {**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                   "PYTHONPATH": str(worktree.resolve() / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
            imported = subprocess.check_output([str(args.python), "-c",
                "import project_mai_tai; print(project_mai_tai.__file__)"], cwd=worktree, env=env, text=True).strip()
            if not Path(imported).resolve().is_relative_to(worktree.resolve() / "src"):
                raise RuntimeError("import path escaped isolated worktree")
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()
            for index, case in enumerate(CASES):
                xml = args.output.with_name(args.output.stem + f"-{label}-{index}.xml")
                log = xml.with_suffix(".log")
                if xml.exists() or log.exists():
                    raise FileExistsError("isolated control receipt already exists")
                started = time.monotonic()
                with log.open("x") as stream:
                    process = subprocess.run([str(args.python), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        case, "--junitxml=" + str(xml)], cwd=worktree, env=env, stdout=stream,
                        stderr=subprocess.STDOUT, timeout=180, check=False)
                result["isolated_controls"].append({"worktree": str(worktree), "head": sha,
                    "import_path": imported, "case": case, "rc": process.returncode,
                    "wall_seconds": time.monotonic() - started, "xml": unit(xml),
                    "log": str(log), "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest()})
    result["failed_names_identical"] = not result["base_only"] and not result["head_only"]
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: result[key] for key in ("base_only", "head_only", "failed_names_identical", "causal_flakiness_claim")}))


if __name__ == "__main__":
    main()
