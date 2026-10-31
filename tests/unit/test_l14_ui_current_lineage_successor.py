"""Preserve historical UI evidence and the explicit installed source successor.

Literal9c historical fixture/history and original55-file set remain intact.
Current source is installed6ddb plus reviewed Ruff and owner-phone changes;
neither arbitrary HEAD nor a new content inventory selects that successor.
"""

from copy import deepcopy
from functools import lru_cache
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
INSTALLED = "6ddb3e6b0dc8295ba35419cee817d86ba0f97fba"
RUFF = "25f752289e437cedecb6484a6be954584abcdfed"
PHONE = "aeed7cc28597ec18a508d2b21a7586529c73f32d"
CURRENT_TREES = {
    INSTALLED: "c54e907d085b0df5fba362168e1ecedc6accbff7",
    RUFF: "ab41a50339dbfa44a4ed028e94cb5eec1deda12b",
    PHONE: "86f6caeb2d4fad53d851a9883809a343180dc664",
}
RUFF_SOURCE_PATHS = {
    "src/project_mai_tai/atr_simulation/firstbar_binding.py",
    "src/project_mai_tai/atr_simulation/firstbar_live_baseline.py",
    "src/project_mai_tai/atr_simulation/firstbar_study.py",
    "src/project_mai_tai/atr_simulation/models.py",
    "src/project_mai_tai/atr_simulation/same_sip_binding.py",
    "src/project_mai_tai/atr_simulation/same_sip_firstbar_live_baseline.py",
    "src/project_mai_tai/atr_simulation/same_sip_types.py",
    "src/project_mai_tai/brokerdecides1_sync.py",
    "src/project_mai_tai/oms/wbexit3_stop.py",
    "src/project_mai_tai/oms/webull_attach_riskoff.py",
    "src/project_mai_tai/services/control_plane.py",
}
PHONE_SOURCE_PATH = "src/project_mai_tai/brokerdecides1_owner_phone.py"


def validate_current_composition(installed_paths, ruff_paths, phone_paths,
                                 ruff_changes, phone_changes):
    assert len(installed_paths) == 642
    assert installed_paths == ruff_paths == phone_paths
    assert set(ruff_changes) == RUFF_SOURCE_PATHS
    assert set(phone_changes) == {PHONE_SOURCE_PATH}


@lru_cache(maxsize=1)
def installed_successor_paths():
    paths = {}
    for revision, tree in CURRENT_TREES.items():
        assert history.git_read("rev-parse", revision + "^{tree}").decode().strip() == tree
        paths[revision] = frozenset(history.git_read(
            "ls-tree", "-r", "--name-only", revision, "--", "src", "sql", "ops",
        ).decode().splitlines())
    changes = [history.git_read(
        "diff", "--no-renames", "--name-only", before, after, "--", "src", "sql", "ops",
    ).decode().splitlines() for before, after in ((INSTALLED, RUFF), (RUFF, PHONE))]
    validate_current_composition(paths[INSTALLED], paths[RUFF], paths[PHONE], *changes)
    return paths[INSTALLED]


def assert_current_bytes(name, actual):
    # Literal reviewed source objects provide postimages; current files never pin themselves.
    assert actual == history.git_read("cat-file", "blob", PHONE + ":" + name), name


def assert_current_membership(actual):
    assert actual == installed_successor_paths()


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
        if name in MODIFIED:
            row = value["changes"][name]
            assert row["before_sha256"] == original[name]
        path = ROOT / name
        assert not path.is_symlink() and path.resolve() == path
        assert_current_bytes(name, path.read_bytes())
    path = ROOT / ADDED
    assert ADDED not in history.tree_paths(history.FALLBACK)
    assert ADDED not in history.tree_paths(history.COMBINED)
    assert not path.is_symlink() and path.resolve() == path
    assert_current_bytes(ADDED, path.read_bytes())
    package = "src/project_mai_tai/atr_simulation/"
    historical_package = {name for name in protected if name.startswith(package)}
    assert len(historical_package) == 48
    expected_package = {name for name in installed_successor_paths() if name.startswith(package)}
    assert historical_package | {ADDED} <= expected_package
    assert {name for name in history.current_runtime_paths() if name.startswith(package)} == expected_package
    assert (ROOT / "src/project_mai_tai/settings.py").read_bytes().count(history.SETTINGS_ADDITION.encode("ascii")) == 1


def assert_current_runtime_membership_successor():
    lineage()
    # B1 bytes and historical UI pins stay intact; membership follows installed6ddb.
    reference = "src/project_mai_tai/services/scanner_reference_recovery.py"
    path = ROOT / reference
    assert not path.is_symlink() and path.resolve() == path
    assert hashlib.sha256(path.read_bytes()).hexdigest() == "d23cc6304794453c26dc1ceaf4e94728ed7b66bfb060a3defcac013cc9f28570"
    assert_current_membership(history.current_runtime_paths())


def test_installed_successor_postimages_preserve_all55_historical_preimages():
    assert_current_protected_l14_successor()


def test_only_installed_successor_paths_enter_current_runtime_membership():
    assert_current_runtime_membership_successor()


@pytest.mark.parametrize("fault", ["extra_path", "missing_path", "protected_bytes", "ruff_path", "phone_path"])
def test_installed_successor_rejects_unreviewed_source_changes(fault):
    paths = installed_successor_paths()
    with pytest.raises(AssertionError):
        if fault == "extra_path":
            assert_current_membership(paths | {"src/project_mai_tai/unreviewed.py"})
        elif fault == "missing_path":
            assert_current_membership(paths - {ADDED})
        elif fault == "protected_bytes":
            protected = "src/project_mai_tai/atr_simulation/firstbar_binding.py"
            assert_current_bytes(protected, history.git_read("cat-file", "blob", PHONE + ":" + protected) + b"\n")
        elif fault == "ruff_path":
            validate_current_composition(paths, paths, paths, RUFF_SOURCE_PATHS | {ADDED}, {PHONE_SOURCE_PATH})
        else:
            validate_current_composition(paths, paths, paths, RUFF_SOURCE_PATHS, {PHONE_SOURCE_PATH, ADDED})


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
