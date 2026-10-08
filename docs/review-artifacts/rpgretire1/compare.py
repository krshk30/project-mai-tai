"""Compare completed clean-baseline and frozen-head pytest XML receipts."""
from pathlib import Path
import hashlib
import json
import sys
import xml.etree.ElementTree as ET


def receipt(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failures = sorted(case.attrib["classname"].replace(".", "/") + ".py::" +
                      case.attrib["name"] for case in cases
                      if case.find("failure") is not None or case.find("error") is not None)
    skipped = sum(case.find("skipped") is not None for case in cases)
    return {"total": len(cases), "failed": len(failures), "skipped": skipped,
            "passed": len(cases) - len(failures) - skipped,
            "time": sum(float(suite.attrib.get("time", 0)) for suite in root.iter("testsuite")),
            "failed_names": failures,
            "failed_names_sha256": hashlib.sha256(("\n".join(failures) + "\n").encode()).hexdigest(),
            "xml_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


if __name__ == "__main__":
    baseline, head = (receipt(path) for path in sys.argv[1:])
    added = sorted(set(head["failed_names"]) - set(baseline["failed_names"]))
    removed = sorted(set(baseline["failed_names"]) - set(head["failed_names"]))
    print(json.dumps({"baseline": baseline, "head": head, "added_failures": added,
                      "removed_failures": removed}, indent=2))
    raise SystemExit(bool(added or removed))
