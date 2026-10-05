"""Freeze exact L5/L7 paired suites, all fresh controls and CI infra evidence."""
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

TARGET = Path("docs/review-artifacts/rpgstuck1")
PARENT = "5ddb50f55498232915bd54171d5cc21579f43240"
prior = json.loads((TARGET / "verification-all-on.json").read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def suite(path):
    cases = list(ET.parse(path).iter("testcase"))
    def node(case):
        return case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
    failed = sorted(node(c) for c in cases if c.find("failure") is not None or c.find("error") is not None)
    skipped = sum(c.find("skipped") is not None for c in cases)
    return {"passed": len(cases) - len(failed) - skipped, "failed": len(failed), "skipped": skipped,
        "failed_nodes": failed, "test_nodes": sorted(node(c) for c in cases),
        "xml_path": path, "xml_sha256": digest(path),
        "text_path": path.replace(".xml", ".txt"), "text_sha256": digest(path.replace(".xml", ".txt"))}


main, head, focused, targeted = [suite(f"/tmp/rpgstuck-l57-{name}.xml")
    for name in ("main-full", "head-full", "focused", "targeted")]
assert (main["passed"], main["failed"], main["skipped"]) == (5577, 56, 0)
assert (head["passed"], head["failed"], head["skipped"]) == (5869, 56, 0)
assert main["failed_nodes"] == head["failed_nodes"]
assert (focused["passed"], focused["failed"], focused["skipped"]) == (1555, 0, 0)
assert (targeted["passed"], targeted["failed"], targeted["skipped"]) == (5, 0, 0)
assert subprocess.check_output(["git", "diff", PARENT, "--", "src", "ops/health/expected_flags.json"]) == b""
production = {path: digest(path) for path in prior["production_sha256"]}
assert production == prior["production_sha256"]
assert digest("ops/health/expected_flags.json") == prior["catalog_sha256"]
for path, expected in prior["test_sha256"].items():
    assert digest(path) == expected

mutations = {}
for label, old in prior["mutations"].items():
    if old["controls"] > 1 and label != "pm6":
        path = Path(f"/tmp/rpgstuck-b-{label}-mutations.txt")
        raw = path.read_text()
        assert len(raw.splitlines()) == old["controls"] and "NOT_PROVEN" not in raw
    else:
        if label == "pm6":
            path = Path("/tmp/rpgstuck-l57-pm-mutants.txt")
        elif label in {"fixture_shared_connection", "exact_R13", "exact_R16"}:
            path = Path(f"/tmp/rpgstuck-fixture-{label}-mutant.txt")
        elif label in {"runtime_edge_wake_disabled", "held_notice_dedup_disabled",
                       "R15_proven_hold_expiry_disabled", "R15_literal_unproven_hold_expires"}:
            path = Path(f"/tmp/rpgstuck-r20-{label}-mutant.txt")
        else:
            name = label.removeprefix("all_on_")
            path = Path(f"/tmp/rpgstuck-all-on-{name}.txt")
        raw = path.read_text()
        if label == "pm6":
            assert raw.count(": ASSERTION_RED") == 6 and "all_on_mutations_assertion_red=6/6" in raw
        else:
            assert "AssertionError" in raw
            assert sum(line.startswith("FAILED tests/unit/") for line in raw.splitlines()) == old["assertion_failed_cases"]
    retained = f"l57-{label}-controls.txt"
    (TARGET / retained).write_text(raw)
    mutations[label] = {**old, "retained_output": retained, "sha256": digest(path)}

for label, failures in (("L5_begin_both_wakes_removed", 2), ("L7_recovered_active_wake_removed", 2)):
    path = Path(f"/tmp/rpgstuck-l57-{label}-mutant.txt")
    raw = path.read_text()
    assert "AssertionError" in raw
    assert sum(line.startswith("FAILED tests/unit/") for line in raw.splitlines()) == failures
    retained = f"l57-{label}-mutant.txt"
    (TARGET / retained).write_text(raw)
    mutations[label] = {"controls": 1, "assertion_failed_cases": failures,
        "retained_output": retained, "sha256": digest(path)}
assert sum(row["controls"] for row in mutations.values()) == 82

job = json.loads(Path("/tmp/rpgstuck-l57-old-ci-job.json").read_text())
annotations = json.loads(Path("/tmp/rpgstuck-l57-old-ci-annotations.json").read_text())
assert job["head_sha"] == PARENT and job["runner_id"] == 0 and job["runner_name"] == "" and job["steps"] == []
assert job["conclusion"] == "cancelled"
assert any(row["message"] == "The job was not acquired by Runner of type hosted even after multiple attempts"
           for row in annotations)
(TARGET / "l57-prior-ci-infrastructure.json").write_text(json.dumps({"job": job, "annotations": annotations}, indent=2) + "\n")
literal = Path("/tmp/rpgstuck-all-on-literal_both_placed_unmet.txt")
assert "AssertionError" in literal.read_text() and "1 failed" in literal.read_text()
(TARGET / "l57-literal-both-placed-unmet.txt").write_text(literal.read_text())
command = prior["focused_command"][:]
command[-1] = "--junitxml=/tmp/rpgstuck-l57-focused.xml"
command.insert(-1, "tests/unit/test_rpgstuck1_loop_liveness.py")
result = {"base": prior["base"], "parent": PARENT, "rebase": False,
    "main": main, "head": head, "focused": focused, "targeted": targeted,
    "added_failed_nodes": [], "removed_failed_nodes": [], "production_sha256": production,
    "catalog_sha256": prior["catalog_sha256"], "prior_test_sha256": prior["test_sha256"],
    "new_test_sha256": digest("tests/unit/test_rpgstuck1_loop_liveness.py"),
    "focused_command": command, "assertion_red_control_runs": 82, "mutations": mutations,
    "duplicate_control_note": prior["duplicate_control_note"], "resolved_review_text": ["L5", "L7"],
    "prior_ci_infrastructure": {"retained_output": "l57-prior-ci-infrastructure.json",
        "sha256": digest(TARGET / "l57-prior-ci-infrastructure.json")},
    "historical_literal_both_placed_experiment": {"result": "ASSERTION_RED_SUPERSEDED_REQUIREMENT",
        "retained_output": "l57-literal-both-placed-unmet.txt",
        "sha256": digest(literal)},
    "amended_apus_requirement": {"result": "PASS_CONTROLLED_REPLAY", "original_market": "4.78",
        "original_outcome": "refused_no_wire_then_ownership_released", "later_controlled_market": "5.25",
        "same_segment": True, "new_generation": True, "webull_sdk_place_calls": 1,
        "acceptance": "simulated, not historical venue evidence"},
    "limits": "Original APUS refusal unchanged; later quote/acceptance is a separate controlled stage. Only loop-emitted ticks handled for L5/L7; bot-authorized ticks deliberately unhandled. All14 actual startup guards retained. No production/default/catalog changes, rebase, merge, deployment, broker/service/ledger writes."}
(TARGET / "verification-l57.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({name: {key: data[key] for key in ("passed", "failed", "skipped")}
    for name, data in (("main", main), ("head", head), ("focused", focused), ("targeted", targeted))}))
