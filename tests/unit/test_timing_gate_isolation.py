"""Fixture/selection controls only: no application timing scenario runs here."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests import conftest


ROOT = Path(__file__).resolve().parents[2]


def fixture_request(marked):
    return SimpleNamespace(node=SimpleNamespace(
        get_closest_marker=lambda name: object() if marked and name == "timing" else None,
    ))


def test_unmarked_fixture_does_not_touch_gc(monkeypatch):
    calls = []
    for name in ("collect", "freeze", "unfreeze"):
        monkeypatch.setattr(conftest.gc, name, lambda name=name: calls.append(name))
    scope = conftest.timing_gc_isolation.__wrapped__(fixture_request(False))
    next(scope)
    with pytest.raises(StopIteration):
        next(scope)
    assert calls == []


@pytest.mark.parametrize("outcome", ["return", "failure", "cancel"])
def test_marked_fixture_collects_then_freezes_and_always_unfreezes(monkeypatch, outcome):
    calls = []
    for name in ("collect", "freeze", "unfreeze"):
        monkeypatch.setattr(conftest.gc, name, lambda name=name: calls.append(name))
    scope = conftest.timing_gc_isolation.__wrapped__(fixture_request(True))
    next(scope)
    assert calls == ["collect", "freeze"]
    if outcome == "return":
        with pytest.raises(StopIteration):
            next(scope)
    elif outcome == "failure":
        with pytest.raises(ValueError, match="controlled body failure"):
            scope.throw(ValueError("controlled body failure"))
    else:
        scope.close()
    assert calls == ["collect", "freeze", "unfreeze"]


def test_freeze_setup_failure_still_unfreezes(monkeypatch):
    calls = []
    monkeypatch.setattr(conftest.gc, "collect", lambda: calls.append("collect"))

    def fail():
        calls.append("freeze")
        raise RuntimeError("controlled freeze failure")

    monkeypatch.setattr(conftest.gc, "freeze", fail)
    monkeypatch.setattr(conftest.gc, "unfreeze", lambda: calls.append("unfreeze"))
    scope = conftest.timing_gc_isolation.__wrapped__(fixture_request(True))
    with pytest.raises(RuntimeError, match="controlled freeze failure"):
        next(scope)
    assert calls == ["collect", "freeze", "unfreeze"]


@pytest.mark.parametrize("outcome", ["pass", "body_failure", "setup_failure"])
def test_actual_pytest_lifecycle_unfreezes_before_next_unmarked_test(tmp_path, outcome):
    # A small child process proves real pytest ordering without running a rate body.
    (tmp_path / "conftest.py").write_text((ROOT / "tests/conftest.py").read_text())
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = timing: controlled fixture probe\n")
    (tmp_path / "test_probe.py").write_text(f'''
import gc
import pytest

ENABLED, THRESHOLDS = gc.isenabled(), gc.get_threshold()

@pytest.fixture
def scenario():
    assert gc.get_freeze_count() > 0
    assert (gc.isenabled(), gc.get_threshold()) == (ENABLED, THRESHOLDS)
    if {outcome!r} == "setup_failure":
        raise RuntimeError("controlled scenario setup failure")

@pytest.mark.timing
def test_a_marked(scenario):
    assert gc.get_freeze_count() > 0
    if {outcome!r} == "body_failure":
        raise AssertionError("controlled scenario body failure")

def test_z_unmarked():
    assert gc.get_freeze_count() == 0
    assert (gc.isenabled(), gc.get_threshold()) == (ENABLED, THRESHOLDS)
    print("UNMARKED_CLEAN")
''')
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q", "-s",
         "--confcutdir", str(tmp_path), "--rootdir", str(tmp_path), str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == (0 if outcome == "pass" else 1), result.stdout + result.stderr
    assert "UNMARKED_CLEAN" in result.stdout, result.stdout + result.stderr
    assert "2 passed" in result.stdout if outcome == "pass" else "1 passed" in result.stdout


def test_inventory_matches_all_explicit_markers_without_importing_gate_modules():
    inventory = json.loads((ROOT / "tests/timing_gates.json").read_text())
    expected = {(row["path"], row["function"]) for row in inventory["gates"]}
    actual = set()
    for path in (ROOT / "tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                ast.unparse(marker) == "pytest.mark.timing" for marker in node.decorator_list
            ):
                actual.add((str(path.relative_to(ROOT)), node.name))
    assert actual == expected
    assert any(row["ci_scope"] == "external_qualification_unchanged" for row in inventory["gates"])
    assert all(row["ci_scope"] == "unit_fresh_process" for row in inventory["gates"]
               if row["path"].startswith("tests/unit/"))


def test_workflow_runs_complementary_unit_selectors_in_separate_steps():
    # Read YAML through the already-installed dev dependency; no shell or CI runs.
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/validate.yml").read_text())
    steps = workflow["jobs"]["validate"]["steps"]
    unit = next(step for step in steps if step["name"] == "Run unit tests")
    timing = next(step for step in steps if step["name"] == "Run timing gates in a fresh process")
    assert steps.index(unit) < steps.index(timing)
    unit_command, timing_command = shlex.split(unit["run"]), shlex.split(timing["run"])
    assert unit_command == ["python", "-m", "pytest", "-p", "no:cacheprovider", "tests/unit",
                            "-m", "not timing"]
    assert timing_command == ["python", "-m", "pytest", "-p", "no:cacheprovider", "tests/unit",
                              "-m", "timing"]
    assert unit["env"] == timing["env"]
    assert not any("--deselect" in token for token in unit_command + timing_command)


def test_fixture_preserves_gc_policy_and_does_not_disable_collection():
    tree = ast.parse((ROOT / "tests/conftest.py").read_text())
    fixture = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "timing_gc_isolation")
    calls = [ast.unparse(node.func) for node in ast.walk(fixture) if isinstance(node, ast.Call)]
    assert calls.count("gc.collect") == calls.count("gc.freeze") == calls.count("gc.unfreeze") == 1
    assert "gc.disable" not in calls and "gc.set_threshold" not in calls
