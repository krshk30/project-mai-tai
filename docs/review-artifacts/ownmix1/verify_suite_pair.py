"""Retain exact failed IDs and refuse new failures in the paired unit suite."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


def suite(path: Path) -> dict:
    output = path.read_text()
    matches = re.findall(r"(\d+) failed, (\d+) passed.*?in ([0-9.]+)s", output)
    assert len(matches) == 1, f"missing or ambiguous completed suite: {path}"
    failed, passed, seconds = matches[0]
    # Async shutdown warnings can be appended to pytest's summary without a newline.
    # Parse the node ID itself, not that trailing stderr text.
    ids = sorted(match[1] for line in output.splitlines() if line.startswith("FAILED ")
                 and (match := re.match(
                     r"FAILED (tests/\S+?::(?:\w+::)*\w+(?:\[[^\n]*?\])?)", line
                 )))
    assert len(ids) == int(failed)
    return {"log": str(path), "sha256": hashlib.sha256(output.encode()).hexdigest(),
            "failed": int(failed), "passed": int(passed), "seconds": float(seconds),
            "failed_ids": ids}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("head", type=Path)
    parser.add_argument("--independent-baseline", type=Path, required=True)
    parser.add_argument("--focused", type=Path, required=True)
    parser.add_argument("--mutations", type=Path, required=True)
    parser.add_argument("--baseline-xml", type=Path)
    parser.add_argument("--head-xml", type=Path)
    args = parser.parse_args()
    baseline, head, independent = (suite(path) for path in
                                   (args.baseline, args.head, args.independent_baseline))
    introduced = sorted(set(head["failed_ids"]) - set(baseline["failed_ids"]))
    resolved = sorted(set(baseline["failed_ids"]) - set(head["failed_ids"]))
    focused = args.focused.read_text()
    focused_passed = re.search(r"^(\d+) passed.*?in ([0-9.]+)s", focused, re.MULTILINE)
    assert focused_passed and "FAILED tests/" not in focused
    mutations = args.mutations.read_text()
    assert "ownership_mutations_killed=17/17" in mutations
    for run, path in [(baseline, args.baseline_xml), (head, args.head_xml)]:
        if path is None:
            continue
        failed_ids = []
        cases = ET.parse(path).findall(".//testcase")
        for case in cases:
            if case.find("failure") is not None or case.find("error") is not None:
                parts = case.attrib["classname"].split(".")
                module_end = next((i for i, part in enumerate(parts) if part.startswith("Test")), len(parts))
                node = "/".join(parts[:module_end]) + ".py"
                failed_ids.append("::".join([node, *parts[module_end:], case.attrib["name"]]))
        assert sorted(failed_ids) == run["failed_ids"], f"XML/log failed names differ: {path}"
        run["junit_xml"] = str(path)
        run["junit_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = {
        "base_commit": "7e10baf0319da796b84934fe38994f6db4fcfc0b",
        "branch": "codex/ownmix1-owned-entry-binding",
        "scope": "F3 bounded flat-fetch deferral and exact-episode pending-writer priority; H4 account/symbol isolation; no rebase atop55563",
        "independent_baseline_base_commit": "a80b51816abf0aefc269f0fdfc473468fd3fe62c",
        "baseline": baseline, "head": head, "independent_baseline": independent,
        "introduced_failed_ids": introduced, "resolved_failed_ids": resolved,
        "independent_failed_ids_equal": baseline["failed_ids"] == independent["failed_ids"],
        "focused": {"passed": int(focused_passed[1]), "seconds": float(focused_passed[2])},
        "mutations": mutations.splitlines(),
    }
    print(json.dumps(result, indent=2))
    assert not introduced, f"new unit failures: {introduced}"
    assert not resolved, f"paired failed names changed: {resolved}"
    assert result["independent_failed_ids_equal"], "independent baseline failed names differ"


if __name__ == "__main__":
    main()
