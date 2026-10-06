"""Compare the complete failure/error name sets from two full pytest runs."""

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


def read(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failures = sorted(f"{item.attrib['classname']}::{item.attrib['name']}"
                      for item in cases if item.find("failure") is not None)
    errors = sorted(f"{item.attrib['classname']}::{item.attrib['name']}"
                    for item in cases if item.find("error") is not None)
    skipped = sum(item.find("skipped") is not None for item in cases)
    return {"total": len(cases), "passed": len(cases) - len(failures) - len(errors) - skipped,
            "skipped_or_xfailed": skipped, "failed": len(failures), "errors": len(errors),
            "failed_names": failures, "error_names": errors}


def read_stdout(path):
    raw = Path(path).read_text()
    summaries = [line for line in raw.splitlines() if re.search(r"\d+ passed.* in ", line)]
    if not summaries:
        raise ValueError(f"unfinished pytest stdout: {path}")
    summary = summaries[-1]

    def count(kind):
        match = re.search(rf"(\d+) {kind}", summary)
        return int(match[1]) if match else 0

    failed = sorted(line.removeprefix("FAILED ").split(" - ", 1)[0]
                    for line in raw.splitlines() if line.startswith("FAILED "))
    errors = sorted(line.removeprefix("ERROR ").split(" - ", 1)[0]
                    for line in raw.splitlines() if line.startswith("ERROR "))
    assert len(failed) == count("failed") and len(errors) == count("errors?"), summary
    skipped = count("skipped") + count("xfailed")
    return {"total": count("passed") + len(failed) + len(errors) + skipped,
            "passed": count("passed"), "skipped_or_xfailed": skipped,
            "failed": len(failed), "errors": len(errors), "summary": summary,
            "failed_names": failed, "error_names": errors}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("head")
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()
    reader = read_stdout if args.stdout else read
    base, head = reader(args.base), reader(args.head)
    results = {"base": base, "head": head,
               "same_failed_names": base["failed_names"] == head["failed_names"],
               "same_error_names": base["error_names"] == head["error_names"],
               "new_failures": sorted(set(head["failed_names"]) - set(base["failed_names"])),
               "new_errors": sorted(set(head["error_names"]) - set(base["error_names"]))}
    print(json.dumps(results, indent=2))
    return 0 if results["same_failed_names"] and results["same_error_names"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
