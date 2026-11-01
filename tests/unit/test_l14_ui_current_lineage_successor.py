"""Preserve historical UI evidence and the explicit installed source successor.

Literal9c historical fixture/history and original55-file set remain intact.
Current source is installed6ddb plus reviewed Ruff, owner-phone, OMS loop-lag and DST changes,
bound by content so a squash-only checkout does not need branch Git objects.
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
CONTENT_PATH = ROOT / "tests/fixtures/atr_simulation/l14_installed_successor_content_20261031.json"
CONTENT_SHA = "754e85ba62a0fbcc0fa3d8d2cd79a1cafec182e7e59e4c31a91a9936ffb2f583"
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
LOOP_LAG_SOURCE_PATH = "src/project_mai_tai/oms/loop_lag.py"
OMS_SERVICE_PATH = "src/project_mai_tai/oms/service.py"
DST_SOURCE_PATHS = {
    "src/project_mai_tai/orb_live_decisions.py",
    "src/project_mai_tai/services/control_plane.py",
    "src/project_mai_tai/services/schwab_1m_v2_bot.py",
    "src/project_mai_tai/services/trade_coach_app.py",
}
BUILD_METADATA_PATHS = {
    "src/project_mai_tai.egg-info/PKG-INFO",
    "src/project_mai_tai.egg-info/SOURCES.txt",
    "src/project_mai_tai.egg-info/dependency_links.txt",
    "src/project_mai_tai.egg-info/entry_points.txt",
    "src/project_mai_tai.egg-info/requires.txt",
    "src/project_mai_tai.egg-info/top_level.txt",
}


def current_runtime_paths():
    # Editable installs/wheel builds generate these six nonruntime files.
    # Keep all other untracked paths, including code inside the same directory.
    return history.current_runtime_paths() - BUILD_METADATA_PATHS


def validate_current_content(value):
    assert value["scope"] == "installed6ddb_plus_reviewed_G2_Ruff_and_C1_owner_phone"
    assert value["runtime_path_count"] == 643
    assert set(value["changed_source_sha256"]) == RUFF_SOURCE_PATHS | {
        PHONE_SOURCE_PATH, LOOP_LAG_SOURCE_PATH, OMS_SERVICE_PATH} | DST_SOURCE_PATHS
    assert len(value["runtime_paths_sha256"]) == 64
    assert len(value["cp_sim_display_sha256"]) == 64
    assert all(len(digest) == 64 for digest in value["changed_source_sha256"].values())


def current_content():
    # Materialized once from reviewed source objects, never from the tested checkout.
    raw = CONTENT_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CONTENT_SHA, "literal_installed_successor_content_required"
    value = json.loads(raw)
    validate_current_content(value)
    return value


def expected_current_hashes():
    value = lineage()
    expected = {item["path"]: item["sha256"]
        for group in ("copied_files", "shared_files") for item in history.CONTRACT[group]}
    expected.update({name: row["after_sha256"] for name, row in value["changes"].items()})
    content = current_content()
    expected.update(content["changed_source_sha256"])
    expected[ADDED] = content["cp_sim_display_sha256"]
    return expected


def assert_current_bytes(name, actual):
    assert hashlib.sha256(actual).hexdigest() == expected_current_hashes()[name], name


def assert_current_membership(actual):
    value = current_content()
    assert len(actual) == value["runtime_path_count"]
    raw = ("\n".join(sorted(actual)) + "\n").encode("utf-8")
    assert hashlib.sha256(raw).hexdigest() == value["runtime_paths_sha256"]


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
    value = lineage()
    protected = {item["path"] for item in history.CONTRACT["copied_files"]
        if item["path"].startswith(("src/", "sql/"))}
    protected.add("src/project_mai_tai/services/control_plane.py")
    assert len(protected) == 55 and MODIFIED <= protected
    original = {item["path"]: item["sha256"] for group in ("copied_files", "shared_files")
        for item in history.CONTRACT[group]}
    for name in sorted(protected):
        # Original historical blob proofs remain in the historical-only tests.
        if name in MODIFIED:
            row = value["changes"][name]
            assert row["before_sha256"] == original[name]
        path = ROOT / name
        assert not path.is_symlink() and path.resolve() == path
        assert_current_bytes(name, path.read_bytes())
    # Cover all seventeen explicitly reviewed source postimages, including modules
    # outside the original55; no branch-only Git objects or fallback skips.
    for name in current_content()["changed_source_sha256"]:
        path = ROOT / name
        assert not path.is_symlink() and path.resolve() == path
        assert_current_bytes(name, path.read_bytes())
    path = ROOT / ADDED
    assert not path.is_symlink() and path.resolve() == path
    assert_current_bytes(ADDED, path.read_bytes())
    package = "src/project_mai_tai/atr_simulation/"
    historical_package = {name for name in protected if name.startswith(package)}
    assert len(historical_package) == 48
    actual_paths = current_runtime_paths()
    assert historical_package | {ADDED} <= actual_paths
    assert_current_membership(actual_paths)
    assert (ROOT / "src/project_mai_tai/settings.py").read_bytes().count(history.SETTINGS_ADDITION.encode("ascii")) == 1


def assert_current_runtime_membership_successor():
    lineage()
    # B1 bytes and historical UI pins stay intact; membership follows installed6ddb.
    reference = "src/project_mai_tai/services/scanner_reference_recovery.py"
    path = ROOT / reference
    assert not path.is_symlink() and path.resolve() == path
    assert hashlib.sha256(path.read_bytes()).hexdigest() == "d23cc6304794453c26dc1ceaf4e94728ed7b66bfb060a3defcac013cc9f28570"
    assert_current_membership(current_runtime_paths())


def test_installed_successor_protected_postimages_are_content_addressed():
    assert_current_protected_l14_successor()


def test_only_installed_successor_paths_enter_current_runtime_membership():
    assert_current_runtime_membership_successor()


@pytest.mark.parametrize("fault", ["extra_path", "missing_path", "substituted_path", "untracked_source",
    "protected_bytes", "ruff_path", "phone_path", "loop_lag_bytes", "loop_lag_path",
    "loop_lag_missing", "service_bytes"])
def test_installed_successor_rejects_unreviewed_source_changes(fault, tmp_path, monkeypatch):
    paths = current_runtime_paths()
    assert_current_membership(paths)
    with pytest.raises(AssertionError):
        if fault == "extra_path":
            assert_current_membership(paths | {"src/project_mai_tai/unreviewed.py"})
        elif fault == "missing_path":
            assert_current_membership(paths - {ADDED})
        elif fault == "substituted_path":
            assert_current_membership((paths - {ADDED}) | {"src/project_mai_tai/unreviewed.py"})
        elif fault == "loop_lag_missing":
            assert_current_membership(paths - {LOOP_LAG_SOURCE_PATH})
        elif fault == "untracked_source":
            source = tmp_path / "src/project_mai_tai/unreviewed.py"
            source.parent.mkdir(parents=True)
            source.write_text("# unreviewed source\n")
            monkeypatch.setattr(history, "ROOT", tmp_path)
            assert_current_membership(paths | current_runtime_paths())
        elif fault == "protected_bytes":
            protected = "src/project_mai_tai/atr_simulation/firstbar_binding.py"
            assert_current_bytes(protected, (ROOT / protected).read_bytes() + b"\n")
        elif fault in {"loop_lag_bytes", "service_bytes"}:
            name = LOOP_LAG_SOURCE_PATH if fault == "loop_lag_bytes" else OMS_SERVICE_PATH
            assert_current_bytes(name, (ROOT / name).read_bytes() + b"\n")
        else:
            value = deepcopy(current_content())
            name = (ADDED if fault == "ruff_path" else LOOP_LAG_SOURCE_PATH
                    if fault == "loop_lag_path" else PHONE_SOURCE_PATH)
            value["changed_source_sha256"][name + ".unreviewed"] = "0" * 64
            validate_current_content(value)


@pytest.mark.parametrize("name", sorted(DST_SOURCE_PATHS))
def test_reviewed_DST_postimages_reject_modified_bytes(name):
    actual = (ROOT / name).read_bytes()
    assert_current_bytes(name, actual)
    with pytest.raises(AssertionError):
        assert_current_bytes(name, actual + b"\n")


def test_reviewed_DST_postimage_set_rejects_an_unreviewed_path():
    value = deepcopy(current_content())
    value["changed_source_sha256"]["src/project_mai_tai/services/trade_coach_app.py.unreviewed"] = "0" * 64
    with pytest.raises(AssertionError):
        validate_current_content(value)


@pytest.mark.parametrize("unreviewed", [None, "unreviewed.py", "arbitrary_metadata.txt"])
def test_current_census_excludes_only_exact_generated_build_metadata(tmp_path, monkeypatch, unreviewed):
    accepted_paths = current_runtime_paths()
    assert_current_membership(accepted_paths)
    for name in BUILD_METADATA_PATHS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated package metadata\n")
    monkeypatch.setattr(history, "ROOT", tmp_path)
    assert current_runtime_paths() == set()
    if unreviewed is None:
        assert_current_membership(accepted_paths | current_runtime_paths())
    else:
        path = tmp_path / "src/project_mai_tai.egg-info" / unreviewed
        path.write_text("# unreviewed source or metadata\n")
        assert current_runtime_paths() == {path.relative_to(tmp_path).as_posix()}
        with pytest.raises(AssertionError):
            assert_current_membership(accepted_paths | current_runtime_paths())


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
