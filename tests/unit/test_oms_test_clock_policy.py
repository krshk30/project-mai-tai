"""Keep clock-dependent OMS tests independent of the machine clock."""

from __future__ import annotations

import ast
from pathlib import Path

CLOCK_DEPENDENT_CALLS = {
    "_attach_webull_protection",
    "_spawn_webull_protection",
    "_v2_rth_edge_bracket",
    "_v2_stand_down_rearm_due",
    "_post_exit_stale_held_action",
}
STALE_HELD_EVALUATOR = "_evaluate_v2_managed_exit"
STALE_HELD_SETTING = "oms_post_exit_stale_held_max_age_seconds"
CLOCK_BOUNDARIES = {"utcnow", "datetime", "_is_regular_market_session"}
UNIT_DIR = Path(__file__).parent


def _called_attributes(tree: ast.AST) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _exercises_clock_dependent_behavior(tree: ast.AST) -> bool:
    calls = _called_attributes(tree)
    if calls & CLOCK_DEPENDENT_CALLS:
        return True
    exercises_stale_held_retry = STALE_HELD_EVALUATOR in calls and any(
        isinstance(node, ast.keyword) and node.arg == STALE_HELD_SETTING
        for node in ast.walk(tree)
    )
    return exercises_stale_held_retry


def _is_autouse_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Call)
        and any(
            keyword.arg == "autouse"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in decorator.keywords
        )
        for decorator in node.decorator_list
    )


def _injects_clock_boundary(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for call in (candidate for candidate in ast.walk(node) if isinstance(candidate, ast.Call)):
        if not isinstance(call.func, ast.Attribute) or call.func.attr != "setattr":
            continue
        if any(
            isinstance(argument, ast.Constant) and argument.value in CLOCK_BOUNDARIES
            for argument in call.args
        ):
            return True
    return False


def _owns_clock(tree: ast.Module) -> bool:
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _is_autouse_fixture(node)
        and _injects_clock_boundary(node)
        for node in tree.body
    )


def test_every_clock_dependent_oms_suite_owns_its_clock() -> None:
    clock_dependent_suites: list[Path] = []
    missing_injection: list[Path] = []

    for path in sorted(UNIT_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not _exercises_clock_dependent_behavior(tree):
            continue
        clock_dependent_suites.append(path)
        if not _owns_clock(tree):
            missing_injection.append(path)

    assert len(clock_dependent_suites) >= 5, "clock-dependent OMS census unexpectedly shrank"
    assert missing_injection == [], (
        "clock-dependent OMS tests must inject their clock or session boundary in an autouse "
        f"fixture; missing: {[path.name for path in missing_injection]}"
    )
