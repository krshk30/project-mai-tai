"""Capture exact failed node names, suite counts, and frozen production hashes."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

target = Path("docs/review-artifacts/rpgstuck1")


def suite(path):
    tree = ET.parse(path)
    cases = list(tree.iter("testcase"))
    failed = sorted(case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
                    for case in cases if case.find("failure") is not None or case.find("error") is not None)
    skipped = sum(case.find("skipped") is not None for case in cases)
    return {"passed": len(cases) - len(failed) - skipped, "failed": len(failed), "skipped": skipped,
            "failed_nodes": failed, "modules": sorted({case.attrib["classname"] for case in cases}),
            "xml_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


baseline = suite("/tmp/rpgstuck-main-git-full.xml")
head = suite("/tmp/rpgstuck-head-reviewed-full.xml")
focused = suite("/tmp/rpgstuck-focused-reviewed.xml")
composition = suite("/tmp/rpgstuck-composition-reviewed.xml")
result = {"base": "a80b51816abf0aefc269f0fdfc473468fd3fe62c", "baseline": baseline,
          "head": head, "focused": focused,
          "composition_base": "71f5f6bc5ae63a2a02b4a6fe5ed469b71cfbe1c8", "composition": composition,
          "added_failed_nodes": sorted(set(head["failed_nodes"]) - set(baseline["failed_nodes"])),
          "removed_failed_nodes": sorted(set(baseline["failed_nodes"]) - set(head["failed_nodes"])),
          "production_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (
              Path("src/project_mai_tai/oms/atr_reprice_handoff.py"),
              Path("src/project_mai_tai/oms/atr_reprice_runtime.py"),
              Path("src/project_mai_tai/oms/service.py"),
              Path("src/project_mai_tai/strategy_core/schwab_1m_v2.py"))},
          "test_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (
              Path("tests/unit/test_rpg1_runtime.py"), Path("tests/unit/test_rpgstuck1.py"),
              Path("tests/fixtures/rpgstuck1_recorded.json"))}}
composition_root = Path("/tmp/rpgstuck-pm-reviewed-composition")
result["composition_production_sha256"] = {
    name: hashlib.sha256((composition_root / name).read_bytes()).hexdigest()
    for name in result["production_sha256"]}
result["composition_patch_sha256"] = hashlib.sha256(
    Path("/tmp/rpgstuck-source-reviewed.patch").read_bytes()).hexdigest()
(target / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
for name in ("before", "after"):
    (target / f"recorded-{name}.json").write_text(Path(f"/tmp/rpgstuck-{name}.json").read_text())
for name, source in (("mutations", "/tmp/rpgstuck-mutations.txt"),
                     ("original9-mutations", "/tmp/rpgstuck-original9-mutations.txt")):
    (target / f"{name}.txt").write_text(Path(source).read_text())
assert baseline["failed"] == head["failed"] == 56
assert not result["added_failed_nodes"] and not result["removed_failed_nodes"]
assert focused["passed"] == 1108 and not focused["failed"]
assert composition["passed"] == 587 and not composition["failed"]
print(json.dumps({key: {k: v for k, v in result[key].items() if k in {"passed", "failed", "skipped"}}
                  for key in ("baseline", "head", "focused", "composition")}))
