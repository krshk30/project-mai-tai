"""Freeze final rebased counts, exact failure sets, mutation logs and hashes."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

target = Path("docs/review-artifacts/rpgstuck1")


def suite(path):
    cases = list(ET.parse(path).iter("testcase"))
    def node(case):
        return case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
    failed = sorted(node(case) for case in cases if case.find("failure") is not None or case.find("error") is not None)
    skipped = sum(case.find("skipped") is not None for case in cases)
    return {"passed": len(cases) - len(failed) - skipped, "failed": len(failed), "skipped": skipped,
        "failed_nodes": failed, "xml_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "test_nodes": sorted(node(case) for case in cases)}


baseline = suite("/tmp/rpgstuck-7e10-main-full.xml")
head = suite("/tmp/rpgstuck-b-frozen-full.xml")
focused = suite("/tmp/rpgstuck-b-frozen-focused.xml")
recorded = suite("/tmp/rpgstuck-b-frozen-recorded.xml")
assert (baseline["passed"], baseline["failed"], baseline["skipped"]) == (5500, 56, 0)
assert baseline["failed_nodes"] == head["failed_nodes"]
assert head["failed"] == 56 and head["skipped"] == 0
assert focused["passed"] == 1318 and focused["failed"] == focused["skipped"] == 0
assert recorded["passed"] == 97 and recorded["failed"] == recorded["skipped"] == 0
baseline.pop("test_nodes")
head.pop("test_nodes")
files = list(json.loads((target / "verification-startup.json").read_text())["production_sha256"])
test_files = ["tests/unit/test_rpgstuck1_review_completion.py", "tests/unit/test_rpgstuck1_later_startup.py",
    "tests/unit/test_rpgstuck1_startup.py", "tests/unit/test_pmprint1_tonight_flags.py",
    "tests/fixtures/rpgstuck1_startup_later.json"]
result = {"base": "7e10baf0319da796b84934fe38994f6db4fcfc0b",
    "rebased_parent": "d68651686a72200913c6e7e498c574d22907ea18",
    "baseline": baseline, "head": head, "focused": focused, "recorded": recorded,
    "added_failed_nodes": [], "removed_failed_nodes": [],
    "production_sha256": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in files},
    "test_fixture_sha256": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in test_files},
    "limits": "Merged PM source included. OWN new source not composed; existing ownership invariants are independent regressions. Strict broker bodies remain the earlier capture, not fresh later-day venue reads."}
mutations = {}
for label, count in (("strict8", 8), ("coordinator8", 8), ("original9", 9), ("runtime9", 9),
                     ("sequence3", 3), ("startup11", 11), ("breview16-final", 16)):
    raw = Path(f"/tmp/rpgstuck-b-{label}-mutations.txt").read_text()
    assert len(raw.splitlines()) == count and "NOT_PROVEN" not in raw
    (target / f"b-{label}-mutations.txt").write_text(raw)
    mutations[label] = count
result["assertion_killed_mutations"] = mutations
assert sum(mutations.values()) == 64
parent_mutations = {}
for name, failures, old, new, source in (
    ("R13", 1, "None if soft_rest or primary_blocked", "None if soft_rest",
     "src/project_mai_tai/strategy_core/schwab_1m_v2.py"),
    ("R16", 6, "_rpg_persisted_local_open(old_request, candidates)",
     "_rpg_persisted_local_open(old_request, [])", "src/project_mai_tai/oms/atr_reprice_runtime.py"),
):
    path = Path(f"/tmp/oct5-exact-{name}-mutant.txt")
    raw = path.read_text()
    assert "AssertionError" in raw and f"{failures} failed," in raw
    assert sum(line.startswith("FAILED tests/unit/") for line in raw.splitlines()) == failures
    assert "ERROR collecting" not in raw
    retained = f"b-parent-exact-{name}-mutant.txt"
    (target / retained).write_text(raw)
    parent_mutations[name] = {"provenance": "Parent independently ran in memory; no file edits",
        "original_output": str(path), "retained_output": retained, "assertion_failed_cases": failures,
        "old": old, "new": new, "source": source,
        "source_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest(),
        "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
result["parent_exact_literal_mutations"] = parent_mutations
(target / "verification-b-review.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({name: {key: data[key] for key in ("passed", "failed", "skipped")}
    for name, data in (("baseline", baseline), ("head", head), ("focused", focused), ("recorded", recorded))}))
