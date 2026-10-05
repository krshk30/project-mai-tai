"""Retain the fresh fixture-clock pair and assertion-only controls."""
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

TARGET = Path("docs/review-artifacts/rpgstuck1")
PARENT = "8cfb704c4724ecfc846be11ba649464c965d9fb4"
BASE = "4987353be54c4be15d4196907dec4dd0d6103236"
prior = json.loads((TARGET / "verification-l57.json").read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def retain(path, name):
    (TARGET / name).write_bytes(Path(path).read_bytes())
    return {"source_path": str(path), "retained_output": name, "sha256": digest(path)}


def suite(name):
    path = f"/tmp/rpgstuck-ci-clock-{name}.xml"
    cases = list(ET.parse(path).iter("testcase"))
    def node(case):
        return case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
    failed = sorted(node(c) for c in cases if c.find("failure") is not None or c.find("error") is not None)
    skipped = sum(c.find("skipped") is not None for c in cases)
    return {"passed": len(cases) - len(failed) - skipped, "failed": len(failed), "skipped": skipped,
        "failed_nodes": failed, "test_nodes": sorted(node(c) for c in cases),
        "xml": retain(path, f"ci-clock-{name}.xml"),
        "text": retain(path.replace(".xml", ".txt"), f"ci-clock-{name}.txt")}


main, head, focused, targeted = [suite(name) for name in ("main-full", "head-full", "focused", "targeted")]
assert (main["passed"], main["failed"], main["skipped"]) == (5577, 56, 0)
assert (head["passed"], head["failed"], head["skipped"]) == (5885, 56, 0)
assert main["failed_nodes"] == head["failed_nodes"]
assert (focused["passed"], focused["failed"], focused["skipped"]) == (1571, 0, 0)
assert (targeted["passed"], targeted["failed"], targeted["skipped"]) == (32, 0, 0)
assert subprocess.check_output(["git", "diff", PARENT, "--", "src", "ops/health/expected_flags.json"]) == b""
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == PARENT
production = {path: digest(path) for path in prior["production_sha256"]}
assert production == prior["production_sha256"]
assert digest("ops/health/expected_flags.json") == prior["catalog_sha256"]
fixture = "tests/unit/test_pmprint1_tonight_flags.py"
for path, expected in prior["prior_test_sha256"].items():
    if path != fixture:
        assert digest(path) == expected
assert digest("tests/unit/test_rpgstuck1_loop_liveness.py") == prior["new_test_sha256"]
fixture_before = hashlib.sha256(subprocess.check_output(["git", "show", f"{PARENT}:{fixture}"])).hexdigest()
assert fixture_before == prior["prior_test_sha256"][fixture]

mutations = {}
for label, old in prior["mutations"].items():
    if old["controls"] > 1 and label != "pm6":
        path = Path(f"/tmp/rpgstuck-b-{label}-mutations.txt")
        raw = path.read_text()
        assert len(raw.splitlines()) == old["controls"] and "NOT_PROVEN" not in raw
    else:
        if label == "pm6":
            path = Path("/tmp/rpgstuck-ci-clock-pm-mutants.txt")
        elif label in {"fixture_shared_connection", "exact_R13", "exact_R16"}:
            path = Path(f"/tmp/rpgstuck-fixture-{label}-mutant.txt")
        elif label in {"runtime_edge_wake_disabled", "held_notice_dedup_disabled",
                       "R15_proven_hold_expiry_disabled", "R15_literal_unproven_hold_expires"}:
            path = Path(f"/tmp/rpgstuck-r20-{label}-mutant.txt")
        elif label in {"L5_begin_both_wakes_removed", "L7_recovered_active_wake_removed"}:
            path = Path(f"/tmp/rpgstuck-l57-{label}-mutant.txt")
        else:
            path = Path(f"/tmp/rpgstuck-all-on-{label.removeprefix('all_on_')}.txt")
        raw = path.read_text()
        if label == "pm6":
            assert raw.count(": ASSERTION_RED") == 6 and "all_on_mutations_assertion_red=6/6" in raw
        else:
            assert "AssertionError" in raw
            assert sum(line.startswith("FAILED tests/unit/") for line in raw.splitlines()) == old["assertion_failed_cases"]
    mutations[label] = {**old, **retain(path, f"ci-clock-{label}-controls.txt")}
assert sum(row["controls"] for row in mutations.values()) == 82

matrix = []
for mode in ("before", "after", "mutant"):
    for timezone in ("UTC", "America-New_York"):
        for wall in ("1944", "2005"):
            path = Path(f"/tmp/rpgstuck-ci-clock-{mode}-{timezone}-{wall}.txt")
            raw = path.read_text()
            red = mode in {"before", "mutant"} and wall == "2005"
            assert ("3 failed, 1 passed" if red else "4 passed") in raw
            if red:
                assert "AssertionError" in raw
                assert sum(line.startswith("FAILED tests/unit/") for line in raw.splitlines()) == 3
            matrix.append({"mode": mode, "timezone": timezone.replace("-", "/"), "host_utc_hhmm": wall,
                "passed": 1 if red else 4, "failed": 3 if red else 0,
                **retain(path, path.name.removeprefix("rpgstuck-"))})
path = Path("/tmp/rpgstuck-ci-clock-regression-mutant-UTC-2005.txt")
raw = path.read_text()
assert "AssertionError" in raw and "16 failed" in raw
assert sum(line.startswith("FAILED tests/unit/") for line in raw.splitlines()) == 16
clock_mutation = {"literal": "remove only recorded-time session-gate binding from the new restarted strategy",
    "production_changed": False, "old_fixture_after1600_assertion_failures_each_timezone": 3,
    "new_regression_assertion_failures": 16, "control_executions": 3,
    **retain(path, "ci-clock-regression-mutant.txt")}
ci = Path("/tmp/rpgstuck-l57-ci-push-failed.txt")
assert "3 failed, 5922 passed" in ci.read_text()
command = prior["focused_command"][:]
command[-1] = "--junitxml=/tmp/rpgstuck-ci-clock-focused.xml"
command.insert(-1, "tests/unit/test_rpgstuck1_restart_clock.py")
result = {"base": BASE, "parent": PARENT, "rebase": False,
    "main": main, "head": head, "focused": focused, "targeted": targeted,
    "added_failed_nodes": [], "removed_failed_nodes": [], "production_sha256": production,
    "catalog_sha256": prior["catalog_sha256"], "prior_test_sha256": prior["prior_test_sha256"],
    "fixture_sha256_before": fixture_before, "fixture_sha256_after": digest(fixture),
    "new_test_sha256": digest("tests/unit/test_rpgstuck1_restart_clock.py"),
    "l57_test_sha256_unchanged": prior["new_test_sha256"], "focused_command": command,
    "prior_assertion_red_control_runs": 82, "mutations": mutations, "clock_matrix": matrix,
    "clock_mutation": clock_mutation, "assertion_red_control_executions_including_clock": 85,
    "duplicate_control_note": "82 existing executions retained (not all distinct); one new clock-binding falsifier run in two host-TZ legacy configurations and the 16-case regression. Pre-cutoff PASS controls and before-fix reproduction failures are not counted as mutants.",
    "actual_prior_ci_failure": {"run": 37366739332, "head": PARENT, "passed": 5922, "failed": 3,
        "timestamp_utc": "2026-10-05T20:05:33Z", **retain(ci, "ci-clock-prior-push-failed.txt")},
    "limits": "Fixture-only: real restarted predicates get recorded time; explicit hostile time still fails real gates. Host TZ not causal. Controlled Mac replay plus actual Linux CI evidence, not a local Linux run. Early checker/import prototypes NOT_PROVEN and excluded. Production/default/catalog and prior L5/L7/APUS proofs unchanged; all14 captured startup guards retained. New exact-head CI SUCCESS x2 and fresh review pin required. No production/service/broker/ledger writes, deployment, merge, rebase or other worktree edit."}
(TARGET / "verification-ci-clock.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({name: {key: data[key] for key in ("passed", "failed", "skipped")}
    for name, data in (("main", main), ("head", head), ("focused", focused), ("targeted", targeted))}))
