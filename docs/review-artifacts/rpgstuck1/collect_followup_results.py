"""Keep the initial record immutable and capture the fresh test-only full pair."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

target = Path("docs/review-artifacts/rpgstuck1")
prior = json.loads((target / "verification.json").read_text())


def suite(path):
    cases = list(ET.parse(path).iter("testcase"))
    failed = sorted(case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
                    for case in cases if case.find("failure") is not None or case.find("error") is not None)
    skipped = sum(case.find("skipped") is not None for case in cases)
    return {"passed": len(cases) - len(failed) - skipped, "failed": len(failed), "skipped": skipped,
            "failed_nodes": failed, "modules": sorted({case.attrib["classname"] for case in cases}),
            "xml_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


baseline = suite("/tmp/rpgstuck-followup-main-full.xml")
head = suite("/tmp/rpgstuck-followup-head-full.xml")
focused = suite("/tmp/rpgstuck-followup-focused.xml")
sequences = suite("/tmp/rpgstuck-followup-sequences.xml")
sequences["passed_nodes"] = sorted(case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
                                   for case in ET.parse("/tmp/rpgstuck-followup-sequences.xml").iter("testcase"))
production = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in prior["production_sha256"]}
assert production == prior["production_sha256"], "test-only follow-up must not alter production source"
assert baseline["failed_nodes"] == head["failed_nodes"] == prior["baseline"]["failed_nodes"]
assert (baseline["passed"], baseline["failed"], baseline["skipped"]) == (5412, 56, 0)
assert (head["passed"], head["failed"], head["skipped"]) == (5461, 56, 0)
assert (focused["passed"], focused["failed"], focused["skipped"]) == (1111, 0, 0)
assert (sequences["passed"], sequences["failed"], sequences["skipped"]) == (3, 0, 0)
result = {"base": prior["base"], "parent": "73e9e4c8f92f672ecc03e7c460b134123015840a",
          "sequencing": "standalone later; explicitly not tonight", "production_source_unchanged": True,
          "production_sha256": production, "baseline": baseline, "head": head,
          "focused": focused, "sequences": sequences, "added_failed_nodes": [], "removed_failed_nodes": [],
          "test_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (
              Path("tests/unit/test_rpgstuck1_schwab_sequences.py"), Path("tests/fixtures/rpgstuck1_sequences.json"))}}
(target / "verification-followup.json").write_text(json.dumps(result, indent=2) + "\n")
(target / "sequence-mutations.txt").write_text(Path("/tmp/rpgstuck-followup-sequence-mutations.txt").read_text())
for name in ("mutations", "original9-mutations"):
    log = Path(f"/tmp/rpgstuck-followup-{name}.txt").read_text()
    assert len(log.splitlines()) == 9 and "NOT_PROVEN" not in log
    (target / f"followup-{name}.txt").write_text(log)
print(json.dumps({key: {k: v for k, v in result[key].items() if k in {"passed", "failed", "skipped"}}
                  for key in ("baseline", "head", "focused", "sequences")}))
