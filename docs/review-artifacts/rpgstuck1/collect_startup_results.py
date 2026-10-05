"""Freeze the complete startup follow-up's paired counts, identities and hashes."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

target = Path("docs/review-artifacts/rpgstuck1")


def suite(path):
    cases = list(ET.parse(path).iter("testcase"))
    failed = sorted(case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
                    for case in cases if case.find("failure") is not None or case.find("error") is not None)
    skipped = sum(case.find("skipped") is not None for case in cases)
    return {"passed": len(cases) - len(failed) - skipped, "failed": len(failed), "skipped": skipped,
            "failed_nodes": failed, "xml_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


baseline = suite("/tmp/rpgstuck-startup-main-full.xml")
head = suite("/tmp/rpgstuck-startup-head-frozen-full.xml")
focused = suite("/tmp/rpgstuck-startup-focused-frozen.xml")
recorded = suite("/tmp/rpgstuck-startup-recorded-final.xml")
recorded["passed_nodes"] = sorted(case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
                                 for case in ET.parse("/tmp/rpgstuck-startup-recorded-final.xml").iter("testcase"))
prior = json.loads((target / "verification-followup.json").read_text())
assert baseline["failed_nodes"] == head["failed_nodes"] == prior["baseline"]["failed_nodes"]
assert (baseline["passed"], baseline["failed"], baseline["skipped"]) == (5412, 56, 0)
assert (head["passed"], head["failed"], head["skipped"]) == (5515, 56, 0)
assert (focused["passed"], focused["failed"], focused["skipped"]) == (1165, 0, 0)
assert (recorded["passed"], recorded["failed"], recorded["skipped"]) == (54, 0, 0)
files = [*prior["production_sha256"], "src/project_mai_tai/broker_adapters/atr_buy_readback.py"]
result = {"base": prior["base"], "parent": "289524631f91f26e922a964067f3ef9907cbc188",
    "baseline": baseline, "head": head, "focused": focused, "recorded": recorded,
    "added_failed_nodes": [], "removed_failed_nodes": [],
    "production_sha256": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in files},
    "test_fixture_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (
        Path("tests/unit/test_rpgstuck1_startup.py"), Path("tests/fixtures/rpgstuck1_startup.json"),
        Path("tests/fixtures/rpgstuck1_startup_broker.json"))}}
for source, expected_count in (("new11-verified", 11), ("original9", 9), ("runtime9", 9), ("sequence3", 3)):
    log = Path(f"/tmp/rpgstuck-startup-{source}-mutations.txt").read_text()
    assert len(log.splitlines()) == expected_count and "NOT_PROVEN" not in log
    (target / f"startup-{source}-mutations.txt").write_text(log)
result["assertion_killed_mutations"] = 32
(target / "verification-startup.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({name: {key: data[key] for key in ("passed", "failed", "skipped")}
                  for name, data in (("baseline", baseline), ("head", head), ("focused", focused), ("recorded", recorded))}))
