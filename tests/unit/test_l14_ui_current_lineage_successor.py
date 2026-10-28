"""UNRUN bounded CURRENT UI successor proposal; not a C1 approval assertion.

Literal9c historical fixture/history and original55-file set remain intact.
No arbitrary HEAD, branch lookup, accepted-descendant or content wildcard.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from tests.unit import test_l14_only_a2_fallback_composition as history


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "tests/fixtures/atr_simulation/l14_current_ui_successor_20261025.json"
SHA = "4033374f0110b09e93fa9c3cc153354d1f8332db93bf9ea56033930c4c0690d9"
ADDED = "src/project_mai_tai/atr_simulation/cp_sim_display.py"
MODIFIED = {"src/project_mai_tai/atr_simulation/aggregate_entry_display.py",
    "src/project_mai_tai/services/atr_simulation_legacy_routes.py",
    "src/project_mai_tai/services/control_plane.py"}


def validate_bounds(value):
    assert value["state"] == "PROPOSED_CURRENT_UI_LINEAGE_PENDING_C1"
    assert (value["historical_head"], value["historical_tree"]) == (
        history.FALLBACK, history.REVIEWED_TREES[history.FALLBACK])
    assert (value["combined_head"], value["combined_tree"]) == (
        history.COMBINED, history.REVIEWED_TREES[history.COMBINED])
    assert value["historical_fixture_sha256"] == history.CONTRACT_SHA256
    assert value["historical_protected_count"] == 55 and value["historical_package_count"] == 48
    assert set(value["changes"]) == MODIFIED and set(value["additions"]) == {ADDED}
    assert value["additions"][ADDED]["before_sha256"] is None
    for row in (*value["changes"].values(), *value["additions"].values()):
        assert len(row["after_sha256"]) == 64


def lineage():
    history.contract_pin()
    raw = PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHA, "literal_UI_successor_fixture_required"
    value = json.loads(raw)
    validate_bounds(value)
    return value


def assert_current_protected_l14_successor():
    value, blobs = lineage(), history.historical_blobs()
    protected = {item["path"] for item in history.CONTRACT["copied_files"]
        if item["path"].startswith(("src/", "sql/"))}
    protected.add("src/project_mai_tai/services/control_plane.py")
    assert len(protected) == 55 and MODIFIED <= protected
    original = {item["path"]: item["sha256"] for group in ("copied_files", "shared_files")
        for item in history.CONTRACT[group]}
    for name in sorted(protected):
        # All55 immutable historical preimages remain asserted against original fixture.
        assert hashlib.sha256(blobs[name]).hexdigest() == original[name], name
        path = ROOT / name
        assert not path.is_symlink() and path.resolve() == path
        if name in MODIFIED:
            row = value["changes"][name]
            assert row["before_sha256"] == original[name]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == row["after_sha256"], name
        else:
            assert path.read_bytes() == blobs[name], name
    path = ROOT / ADDED
    assert ADDED not in history.tree_paths(history.FALLBACK)
    assert ADDED not in history.tree_paths(history.COMBINED)
    assert not path.is_symlink() and path.resolve() == path
    assert hashlib.sha256(path.read_bytes()).hexdigest() == value["additions"][ADDED]["after_sha256"]
    package = "src/project_mai_tai/atr_simulation/"
    historical_package = {name for name in protected if name.startswith(package)}
    assert len(historical_package) == 48
    assert {name for name in history.current_runtime_paths() if name.startswith(package)} == historical_package | {ADDED}
    assert (ROOT / "src/project_mai_tai/settings.py").read_bytes().count(history.SETTINGS_ADDITION.encode("ascii")) == 1


def assert_current_runtime_membership_successor():
    lineage()
    # C12133: exact separately reviewed B1 component; historical UI pins stay intact.
    reference = "src/project_mai_tai/services/scanner_reference_recovery.py"
    path = ROOT / reference
    assert not path.is_symlink() and path.resolve() == path
    assert hashlib.sha256(path.read_bytes()).hexdigest() == "d23cc6304794453c26dc1ceaf4e94728ed7b66bfb060a3defcac013cc9f28570"
    assert history.current_runtime_paths() == history.tree_paths(history.COMBINED) | {ADDED, reference}


def test_exact_current_UI_postimages_preserve_all55_historical_preimages():
    assert_current_protected_l14_successor()


def test_only_exact_added_UI_module_enters_current_runtime_membership():
    assert_current_runtime_membership_successor()


@pytest.mark.parametrize("fault", ["head", "new_path", "protected_count"])
def test_lineage_cannot_expand_to_an_arbitrary_head_path_or_protected_set(fault):
    value = deepcopy(lineage())
    if fault == "head":
        value["combined_head"] = "HEAD"
    elif fault == "new_path":
        value["additions"]["src/project_mai_tai/unreviewed.py"] = {"before_sha256": None, "after_sha256": "0" * 64}
    else:
        value["historical_protected_count"] = 54
    with pytest.raises(AssertionError):
        validate_bounds(value)
