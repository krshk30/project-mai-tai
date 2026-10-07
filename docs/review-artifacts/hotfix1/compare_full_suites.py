"""Report the exact JUnit pair, without suppressing volatile failures."""
import json
import sys
import xml.etree.ElementTree as ET


def read(path):
    cases = ET.parse(path).getroot().findall(".//testcase")
    failed = sorted(
        case.attrib["classname"] + "::" + case.attrib["name"]
        for case in cases
        if case.find("failure") is not None or case.find("error") is not None
    )
    skipped = sum(case.find("skipped") is not None for case in cases)
    return {"xml": path, "failed": len(failed), "passed": len(cases) - len(failed) - skipped,
            "skipped": skipped, "failed_names": failed}


base, head = read(sys.argv[1]), read(sys.argv[2])
print(json.dumps({"tested_commit": sys.argv[3], "base": base, "head": head,
    "new_failed_names": sorted(set(head["failed_names"]) - set(base["failed_names"])),
    "removed_failed_names": sorted(set(base["failed_names"]) - set(head["failed_names"]))}, indent=2))
