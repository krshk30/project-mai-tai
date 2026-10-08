"""Compare literal failed names and preserved source methods against exact main."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[3]
BASE = "1e15adb03c3758e647d333828bbe3090e24e832e"
SOURCE = "src/project_mai_tai/oms/mirror_retained_hold.py"
BASE_XML = Path("/tmp/five-lane-main-1e15adb0-unit.xml")
EXPECTED_HASH = "e0b9fc7478ff484d88d9882194d3525632c0d2d43353d515b28f63a28464e9a9"


def source_receipt():
    before = subprocess.check_output(["git", "show", BASE + ":" + SOURCE], cwd=ROOT, text=True)
    after = (ROOT / SOURCE).read_text()

    def methods(text):
        tree = ast.parse(text)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        lines = text.splitlines(keepends=True)
        return {n.name: "".join(lines[n.lineno - 1:n.end_lineno]) for n in cls.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    old, new = methods(before), methods(after)
    changed = sorted(name for name in old if old[name] != new[name])
    return {"base": BASE, "source_sha256": hashlib.sha256(after.encode()).hexdigest(),
            "changed_existing_methods": changed, "added_methods": sorted(set(new) - set(old)),
            "protected_methods_unchanged": sorted(name for name in old if old[name] == new[name]),
            "protected": changed == ["_mirrorhold_dispatch", "_mirrorhold_gate"]}


def xml_receipt(path):
    nodes = ET.parse(path).findall(".//testcase")
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    failures = []
    failed_tuples = []
    for node in nodes:
        status = "failed" if node.find("failure") is not None else "error" if node.find("error") is not None else "skipped" if node.find("skipped") is not None else "passed"
        counts[status] += 1
        if status in {"failed", "error"}:
            failed_tuples.append((node.attrib["classname"], node.attrib["name"]))
            failures.append(node.attrib["classname"].replace(".", "/") + ".py::" + node.attrib["name"])
    failures.sort()
    formats = {}
    for label, values in {
        "nodeid": failures,
        "classname::name": sorted(c + "::" + n for c, n in failed_tuples),
        "classname.name": sorted(c + "." + n for c, n in failed_tuples),
        "name": sorted(n for c, n in failed_tuples),
    }.items():
        for encoding, payload in {"newline": "\n".join(values), "newline-terminated": "\n".join(values) + "\n",
                                  "json": json.dumps(values), "json-compact": json.dumps(values, separators=(",", ":"))}.items():
            formats[label + "/" + encoding] = hashlib.sha256(payload.encode()).hexdigest()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "counts": counts,
            "failed_names": failures, "hash_formats": formats}


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--source":
        receipt = source_receipt()
        print(json.dumps(receipt, indent=2))
        return 0 if receipt["protected"] else 1
    head_path = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("/tmp/mirrorholdG-final-head-unit.xml")
    base, head = xml_receipt(BASE_XML), xml_receipt(head_path)
    added = sorted(set(head["failed_names"]) - set(base["failed_names"]))
    absent = sorted(set(base["failed_names"]) - set(head["failed_names"]))
    receipt = {"base_commit": BASE, "base": base, "head": head,
               "expected_base_failed_name_hash": EXPECTED_HASH,
               "matching_hash_formats": [k for k, v in base["hash_formats"].items() if v == EXPECTED_HASH],
               "added_failed_names": added, "base_failures_absent_on_head": absent,
               "literal_failed_names_identical": not added and not absent, "source": source_receipt()}
    print(json.dumps(receipt, indent=2))
    return 0 if receipt["literal_failed_names_identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
