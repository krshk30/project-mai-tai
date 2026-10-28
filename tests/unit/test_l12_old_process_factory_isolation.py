"""Offline source/guard controls; no OLD replay, engine or database execution."""

import builtins
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from tools.l12_original122_branch_adapter import (
    OLD_APP_TREE, OLD_EXECUTION_VERSION, OLD_HEAD, validate_original122_world,
)


def isolated_factory_module(monkeypatch):
    original = builtins.__import__
    forbidden = ("project_mai_tai", "tools.l12_service_boot_replay", "tools.l12_scoped_chunks_v2",
        "tools.l12_scoped_restore_v2", "tools.l12_private_schema_evidence")

    def controlled_import(name, *args, **kwargs):
        if any(name == value or name.startswith(value + ".") for value in forbidden):
            raise AssertionError("CONTROLLED_NEW_APP_IMPORT_FORBIDDEN:" + name)
        return original(name, *args, **kwargs)

    name = "_l12_controlled_isolated_factory"
    path = Path(__file__).parents[2] / "tools/l12_private_full23_lifecycle.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(builtins, "__import__", controlled_import)
    spec.loader.exec_module(module)
    return module


def test_existing_factory_module_imports_with_all_APP_and_new_copy_imports_forbidden(monkeypatch):
    module = isolated_factory_module(monkeypatch)
    assert module.CheckedFactory.__name__ == "CheckedFactory"
    assert module.PinnedSession.__name__ == "PinnedSession"
    assert module.PrivateAuthority.__name__ == "PrivateAuthority"


@pytest.mark.parametrize("kind", ["factory", "session"])
def test_nonce_wrong_capability_stops_without_APP_import_or_target_use(monkeypatch, kind):
    module = isolated_factory_module(monkeypatch)
    with pytest.raises(ValueError, match="reviewed_Main_nonce_target"):
        if kind == "factory":
            module.require_private_factory(None, target_capability=object())
        else:
            module.verify_private_session(None, target_capability=object())


def test_legacy_path_remains_explicit_legacy_not_nonce_or_target_waiver(monkeypatch):
    module = isolated_factory_module(monkeypatch)
    with pytest.raises(AssertionError, match="CONTROLLED_NEW_APP_IMPORT_FORBIDDEN"):
        module.require_private_factory(SimpleNamespace())


def world():
    return dict(label="OLD", head=OLD_HEAD, source_review_sha256="a" * 64,
        version=OLD_EXECUTION_VERSION, execution_head="b" * 40, app_tree=OLD_APP_TREE)


def test_tools_only_execution_head_is_separate_from_literal_old_APP():
    value = world()
    assert validate_original122_world(value) == value
    assert value["head"] != value["execution_head"]


@pytest.mark.parametrize("change", [dict(head="b" * 40), dict(label="NEW"),
    dict(app_tree="c" * 40), dict(execution_head="0" * 40), dict(execution_head=None),
    dict(source_review_sha256="0" * 64), dict(extra=True)])
def test_explicit_old_execution_carry_refuses_wrong_component_or_unreviewed_root(change):
    value = dict(world(), **change)
    with pytest.raises(ValueError):
        validate_original122_world(value)


def test_historical_three_field_world_contract_is_unchanged():
    value = dict(label="OLD", head=OLD_HEAD, source_review_sha256="a" * 64)
    assert validate_original122_world(value) == value
