"""Retain exact frozen ALL-ON suite IDs, recorded dispositions and controls."""
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

TARGET = Path("docs/review-artifacts/rpgstuck1")
BASE = "4987353be54c4be15d4196907dec4dd0d6103236"
PARENT = "462c8aa1ffc2f5515f50ba14964fc38e839ddc89"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def suite(path):
    cases = list(ET.parse(path).iter("testcase"))
    def nodes(selected):
        return sorted(c.attrib["classname"].replace(".", "/") + ".py::" +
            c.attrib["name"] for c in selected)
    failed = [c for c in cases if c.find("failure") is not None or c.find("error") is not None]
    skipped = [c for c in cases if c.find("skipped") is not None]
    return {"passed": len(cases) - len(failed) - len(skipped), "failed": len(failed),
        "skipped": len(skipped), "failed_nodes": nodes(failed), "test_nodes": nodes(cases),
        "xml_sha256": digest(path), "raw_path": path,
        "text_sha256": digest(path.replace(".xml", ".txt")),
        "text_path": path.replace(".xml", ".txt")}


main = suite("/tmp/rpgstuck-all-on-main-full.xml")
head = suite("/tmp/rpgstuck-all-on-head-full.xml")
focused = suite("/tmp/rpgstuck-all-on-focused.xml")
targeted = suite("/tmp/rpgstuck-all-on-targeted-final.xml")
assert (main["passed"], main["failed"], main["skipped"]) == (5577, 56, 0)
assert (targeted["passed"], targeted["failed"], targeted["skipped"]) == (140, 0, 0)
assert focused["failed"] == focused["skipped"] == 0
assert main["failed_nodes"] == head["failed_nodes"] and head["skipped"] == 0
assert subprocess.check_output(["git", "diff", PARENT, "--", "src"]) == b""

production_paths = json.loads((TARGET / "verification-r20.json").read_text())["production_sha256"]
production = {}
for path in [*production_paths, "src/project_mai_tai/settings.py"]:
    prior = subprocess.check_output(["git", "show", f"{PARENT}:{path}"])
    assert hashlib.sha256(prior).hexdigest() == digest(path)
    production[path] = digest(path)

dispositions = {}
for case in ET.parse("/tmp/rpgstuck-all-on-targeted-final.xml").iter("testcase"):
    for prop in case.findall("properties/property"):
        if prop.attrib["name"] == "dispositions":
            dispositions[case.attrib["name"]] = json.loads(prop.attrib["value"])
assert len(dispositions) == 4 and all(len(rows) == 14 for rows in dispositions.values())
(TARGET / "all-on-dispositions.json").write_text(json.dumps(dispositions, indent=2) + "\n")

mutations = {}
for label, count in (("strict8", 8), ("coordinator8", 8), ("original9", 9), ("runtime9", 9),
                     ("sequence3", 3), ("startup11", 11), ("breview16", 16)):
    path = Path(f"/tmp/rpgstuck-b-{label}-mutations.txt")
    raw = path.read_text()
    assert len(raw.splitlines()) == count and "NOT_PROVEN" not in raw
    retained = f"all-on-{label}-mutations.txt"
    (TARGET / retained).write_text(raw)
    mutations[label] = {"controls": count, "retained_output": retained, "sha256": digest(path)}

for label, path, failures in (
    ("fixture_shared_connection", "/tmp/rpgstuck-fixture-fixture_shared_connection-mutant.txt", 1),
    ("exact_R13", "/tmp/rpgstuck-fixture-exact_R13-mutant.txt", 1),
    ("exact_R16", "/tmp/rpgstuck-fixture-exact_R16-mutant.txt", 6),
    ("runtime_edge_wake_disabled", "/tmp/rpgstuck-r20-runtime_edge_wake_disabled-mutant.txt", 2),
    ("held_notice_dedup_disabled", "/tmp/rpgstuck-r20-held_notice_dedup_disabled-mutant.txt", 1),
    ("R15_proven_hold_expiry_disabled", "/tmp/rpgstuck-r20-R15_proven_hold_expiry_disabled-mutant.txt", 2),
    ("R15_literal_unproven_hold_expires", "/tmp/rpgstuck-r20-R15_literal_unproven_hold_expires-mutant.txt", 2),
    ("all_on_runtime_edge_disabled", "/tmp/rpgstuck-all-on-runtime_edge_disabled.txt", 2),
    ("catalog_handoff_off", "/tmp/rpgstuck-all-on-catalog_handoff_off.txt", 1),
    ("catalog_oms_omitted", "/tmp/rpgstuck-all-on-catalog_oms_omitted.txt", 1),
):
    raw = Path(path).read_text()
    assert "AssertionError" in raw and f"{failures} failed" in raw
    assert sum(line.startswith("FAILED tests/unit/") for line in raw.splitlines()) == failures
    retained = f"all-on-{label}-mutant.txt"
    (TARGET / retained).write_text(raw)
    mutations[label] = {"controls": 1, "assertion_failed_cases": failures,
        "retained_output": retained, "sha256": digest(path)}

pm_path = Path("/tmp/rpgstuck-all-on-pm-mutants.txt")
pm_raw = pm_path.read_text()
assert pm_raw.count(": ASSERTION_RED") == 6 and "all_on_mutations_assertion_red=6/6" in pm_raw
(TARGET / "all-on-pm-mutants.txt").write_text(pm_raw)
mutations["pm6"] = {"controls": 6, "retained_output": "all-on-pm-mutants.txt", "sha256": digest(pm_path)}
assert sum(row["controls"] for row in mutations.values()) == 80

literal = Path("/tmp/rpgstuck-all-on-literal_both_placed_unmet.txt")
raw = literal.read_text()
assert "AssertionError" in raw and "1 failed" in raw
(TARGET / "all-on-literal-both-placed-unmet.txt").write_text(raw)
tests = ["tests/unit/test_rpgstuck1_all_on.py", "tests/unit/test_all_on_pm.py",
    "tests/unit/mutate_all_on_pm.py", "tests/unit/test_expected_flags_check.py",
    "tests/unit/test_pmprint1_tonight_flags.py"]
fixture_paths = ["tests/fixtures/rpgstuck1_startup_later.json", "tests/fixtures/rpgstuck1_startup_broker.json"]
result = {"base": BASE, "parent": PARENT, "rebase": False,
    "pm_proof_origin": "430686a32c581160ed211c895e6c390bac315e64",
    "main": main, "head": head, "focused": focused, "targeted": targeted,
    "added_failed_nodes": [], "removed_failed_nodes": [], "production_sha256": production,
    "test_sha256": {path: digest(path) for path in tests},
    "fixture_sha256": {path: digest(path) for path in fixture_paths},
    "catalog_sha256": digest("ops/health/expected_flags.json"),
    "assertion_red_control_runs": 80, "mutations": mutations,
    "duplicate_control_note": "Reviewer literal R15 repeats retained B4; ALL-ON repeats R20. Counts are control executions, not distinct falsifiers.",
    "literal_both_placed_requirement": {"result": "UNMET", "symbol": "APUS", "time_et": "09:32",
        "market": "4.78", "raw_stop": "5.2720", "phase": "refused", "wire_calls": 0,
        "retained_output": "all-on-literal-both-placed-unmet.txt", "sha256": digest(literal)},
    "missing_review_text": ["L5", "L7"],
    "limits": "Captured 14-ticket census only; future quote, stage rewinds, MI/SCKT line reconstruction and adapter acceptances controlled. No real venue acceptance, complete MI/SCKT tape, install, production/service/broker/ledger writes or merge."}
extra = """v2_entry_composition_slot v2_flip_owned_first_entry v2_resting_cancel_rth_placed
v2_retry_one v2_webull_resting_mirror rpgstuck1_schwab_sequences rpgstuck1_startup
rpgstuck1_review_completion rpgstuck1_later_startup rpgstuck1_runtime_exhaustion
rpgstuck1_all_on all_on_pm pmprint1_pmflip1 pmprint1_tonight_flags oco_exit_ownership
oms_native_oco_stand_down orphan_order_ownership_and_oversell ownmix1_entry_binding
ownmix1_oco_hint ownmix1_review_followup""".split()
result["focused_command"] = ["env", "PYTHONPATH=src",
    "/Users/velkris/Projects/project-mai-tai/.venv/bin/python",
    "docs/review-artifacts/rpgstuck1/run_focused.py",
    *[f"tests/unit/test_{name}.py" for name in extra],
    "--junitxml=/tmp/rpgstuck-all-on-focused.xml"]
(TARGET / "verification-all-on.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({name: {key: data[key] for key in ("passed", "failed", "skipped")}
    for name, data in (("main", main), ("head", head), ("focused", focused), ("targeted", targeted))}))
