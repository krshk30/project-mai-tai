"""Retain the frozen R20 pair, exact IDs, source hashes and fresh mutation outputs."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

target = Path("docs/review-artifacts/rpgstuck1")


def suite(path):
    cases = list(ET.parse(path).iter("testcase"))
    def node(case):
        return case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
    failed = sorted(node(c) for c in cases if c.find("failure") is not None or c.find("error") is not None)
    skipped = sum(c.find("skipped") is not None for c in cases)
    return {"passed": len(cases) - len(failed) - skipped, "failed": len(failed), "skipped": skipped,
        "failed_nodes": failed, "test_nodes": sorted(node(c) for c in cases),
        "xml_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


main = suite("/tmp/rpgstuck-r20-main-full.xml")
head = suite("/tmp/rpgstuck-r20-frozen-full.xml")
focused = suite("/tmp/rpgstuck-r20-frozen-focused.xml")
assert (main["passed"], main["failed"], main["skipped"]) == (5500, 56, 0)
assert (head["passed"], head["failed"], head["skipped"]) == (5692, 56, 0)
assert main["failed_nodes"] == head["failed_nodes"]
assert (focused["passed"], focused["failed"], focused["skipped"]) == (1378, 0, 0)
main.pop("test_nodes")
head.pop("test_nodes")
prior = json.loads((target / "verification-b-review.json").read_text())
production = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in prior["production_sha256"]}
assert [p for p in production if production[p] != prior["production_sha256"][p]] == [
    "src/project_mai_tai/oms/atr_reprice_runtime.py"]
mutations = {}
for label, count in (("strict8", 8), ("coordinator8", 8), ("original9", 9), ("runtime9", 9),
                     ("sequence3", 3), ("startup11", 11), ("breview16", 16)):
    path = Path(f"/tmp/rpgstuck-b-{label}-mutations.txt")
    raw = path.read_text()
    assert len(raw.splitlines()) == count and "NOT_PROVEN" not in raw
    retained = f"r20-{label}-mutations.txt"
    (target / retained).write_text(raw)
    mutations[label] = {"controls": count, "retained_output": retained,
        "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
for label, path, failures in (
    ("fixture_shared_connection", "/tmp/rpgstuck-fixture-fixture_shared_connection-mutant.txt", 1),
    ("exact_R13", "/tmp/rpgstuck-fixture-exact_R13-mutant.txt", 1),
    ("exact_R16", "/tmp/rpgstuck-fixture-exact_R16-mutant.txt", 6),
    ("runtime_edge_wake_disabled", "/tmp/rpgstuck-r20-runtime_edge_wake_disabled-mutant.txt", 2),
    ("held_notice_dedup_disabled", "/tmp/rpgstuck-r20-held_notice_dedup_disabled-mutant.txt", 1),
    ("R15_proven_hold_expiry_disabled", "/tmp/rpgstuck-r20-R15_proven_hold_expiry_disabled-mutant.txt", 2),
    ("R15_literal_unproven_hold_expires", "/tmp/rpgstuck-r20-R15_literal_unproven_hold_expires-mutant.txt", 2),
):
    raw = Path(path).read_text()
    assert "AssertionError" in raw and f"{failures} failed" in raw
    assert sum(line.startswith("FAILED tests/unit/") for line in raw.splitlines()) == failures
    retained = f"r20-{label}-mutant.txt"
    (target / retained).write_text(raw)
    mutations[label] = {"controls": 1, "assertion_failed_cases": failures, "retained_output": retained,
        "output_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
assert sum(item["controls"] for item in mutations.values()) == 71
result = {"base": "7e10baf0319da796b84934fe38994f6db4fcfc0b",
    "parent": "c6548875bab15fb52bf35ca491e2e0ea63cc7148", "rebase": False,
    "main": main, "head": head, "focused": focused, "added_failed_nodes": [], "removed_failed_nodes": [],
    "production_sha256": production, "runtime_tests_sha256": hashlib.sha256(
        Path("tests/unit/test_rpgstuck1_runtime_exhaustion.py").read_bytes()).hexdigest(),
    "assertion_red_control_runs": 71, "mutations": mutations,
    "duplicate_control_note": "Explicit reviewer R15 literal reruns the retained prior B4 mutation; not claimed as a new distinct falsifier.",
    "limits": "Own standalone main pair. New OWN source not composed here; parent independently owns composition. No prod/service/broker/ledger writes, no merge or age clearance."}
(target / "verification-r20.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({name: {key: data[key] for key in ("passed", "failed", "skipped")}
    for name, data in (("main", main), ("head", head), ("focused", focused))}))
