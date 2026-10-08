"""Compare completed HEAD XML with the parent's retained exact-base receipt."""
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

BASELINE_NAMES_SHA256 = "e0b9fc7478ff484d88d9882194d3525632c0d2d43353d515b28f63a28464e9a9"


def receipt(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failures = sorted(f"{case.attrib['classname']}::{case.attrib['name']}" for case in cases
                      if case.find("failure") is not None or case.find("error") is not None)
    skipped = sum(case.find("skipped") is not None for case in cases)
    return {"path": str(path), "total": len(cases), "failed": len(failures),
            "passed": len(cases) - len(failures) - skipped, "skipped": skipped,
            "suite_seconds": sum(float(suite.attrib.get("time", 0)) for suite in root.iter("testsuite")),
            "failed_names": failures,
            "failed_names_sha256": hashlib.sha256("\n".join(failures).encode()).hexdigest(),
            "xml_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


if __name__ == "__main__":
    baseline, head = (receipt(path) for path in sys.argv[1:])
    assert baseline["failed_names_sha256"] == BASELINE_NAMES_SHA256, "not the retained parent baseline"
    added = sorted(set(head["failed_names"]) - set(baseline["failed_names"]))
    missing = sorted(set(baseline["failed_names"]) - set(head["failed_names"]))
    print(json.dumps({"baseline": baseline, "head": head, "added_failures": added,
                      "missing_baseline_failures": missing,
                      "hash_format": "sorted classname::name; newline-joined; no terminal newline"}, indent=2))
    raise SystemExit(bool(added or missing))
