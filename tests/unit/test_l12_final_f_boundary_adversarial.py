"""Adverse F carry seams; no actual Main source/copy acceptance is minted."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_l12_final_f_explicit_carry import final_operands, reseal
from tools import l12_private_restore_main as restore
from tools.l12_composed_execution_binding import bind_composed_execution
from tools.l12_private_schema_evidence import CURRENT_F_APP_COMPONENT


def test_whole_334_inverse_does_not_rewrite_any_approved_operand():
    approval, pins = final_operands()
    before = deepcopy((approval, pins))
    assert sum(path.startswith("src/project_mai_tai/") for path in pins) == 334
    restore.require_current_f_app_inventory(pins)
    assert (approval, pins) == before
    assert pins[CURRENT_F_APP_COMPONENT["path"]] == CURRENT_F_APP_COMPONENT["after_sha256"]


@pytest.mark.parametrize("path", [
    "sql/migrations/env.py",
    "sql/migrations/versions/20261022_l12_dispatch_retirement.py",
    "alembic.ini",
])
def test_f_inverse_does_not_bypass_existing_schema_or_ini_pins(path):
    approval, pins = final_operands()
    pins[path] = "0" * 64
    reseal(approval, pins)
    with pytest.raises(ValueError):
        bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("field", ["execution_root", "consumer_source_head", "creation_source_head"])
def test_actual_execution_and_original_creation_are_not_the_literal_app_component(field):
    approval, pins = final_operands()
    if field == "execution_root":
        approval[field] = CURRENT_F_APP_COMPONENT["head"]
    else:
        approval["consumer_binding"][field] = CURRENT_F_APP_COMPONENT["head"]
    if field == "creation_source_head":
        # Existing binding accepts an independently reviewed original creation
        # head. The guard below, not an APP inverse, must pin the real target.
        subject = SimpleNamespace(values={"source_head": "e" * 40})
        guard = restore.CopySourceGuard(Path("/CONTROLLED_NOT_READ"), "f" * 40, (),
            10 ** 20, creation_head=approval["consumer_binding"][field],
            combined_app=True, current_f_app=True)
        assert guard(subject) is False
    else:
        with pytest.raises(ValueError):
            bind_composed_execution(approval, observed_root="f" * 40, observed_pins=pins)


@pytest.mark.parametrize("directory", ["src/project_mai_tai", "sql", "ops", "tools"])
def test_existing_complete_tree_check_still_refuses_changed_bound_bytes(tmp_path, directory):
    # Exercise the unchanged filesystem checker directly, not a synthetic APP
    # inventory disguised as the real334 component.
    inventory = {}
    for name in restore.v10.SOURCE_DIRS:
        path = tmp_path / name / "bounded.txt"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"original\n")
        inventory[path.relative_to(tmp_path).as_posix()] = sha256(path.read_bytes()).hexdigest()
    (tmp_path / directory / "bounded.txt").write_bytes(b"changed\n")
    with pytest.raises(ValueError, match="source_bytes_changed"):
        restore._verify_bound_combined_tree(tmp_path, inventory)


@pytest.mark.parametrize("fault", ["foreign_import", "ini_drift", "extra_member"])
def test_f_guard_retains_import_ini_and_membership_checks(tmp_path, monkeypatch, fault):
    # UNIT F-selection seam only. Real334 verification is separately tested
    # above and in the14 original controls; this is not a Main root receipt.
    ini = tmp_path / "alembic.ini"
    ini.write_bytes(b"reviewed ini\n")
    member = tmp_path / "tools" / "bounded.py"
    member.parent.mkdir()
    member.write_bytes(b"# controlled source\n")
    inventory = {"alembic.ini": sha256(ini.read_bytes()).hexdigest(),
                 "tools/bounded.py": sha256(member.read_bytes()).hexdigest()}
    modules = {}
    if fault == "foreign_import":
        modules["tools.foreign"] = SimpleNamespace(__file__="/FOREIGN/bounded.py")
    elif fault == "ini_drift":
        ini.write_bytes(b"unreviewed ini\n")
    else:
        inventory["tools/not_loaded.py"] = "a" * 64

    def selected_f_tree(root, tree):
        assert root == tmp_path
        if set(tree) != {"tools/bounded.py"}:
            raise ValueError("controlled_membership_changed")

    monkeypatch.setattr(restore, "verify_current_f_tree", selected_f_tree)
    monkeypatch.setattr(restore, "sys", SimpleNamespace(modules=modules))
    guard = restore.CopySourceGuard(tmp_path, "f" * 40, tuple(inventory.items()),
        10 ** 20, creation_head="e" * 40, combined_app=True, current_f_app=True)
    assert guard(SimpleNamespace(execution_head="f" * 40)) is False
