"""§137 — a module that nothing imports is doing no work, however green its tests are.

⛔⭐⭐ FOUND IN OUR OWN OUTPUT, ONE DAY OLD. `backtest/broker_refusal.py` shipped 2026-08-18 —
complete, documented, unit-tested, CI-green, merged — and **imported by nothing**. It sat inert for
a day while the replay engine's broker model stayed "orders fill". Every test passed. Nothing was
wrong except that it was not connected to anything.

"It exists" is not "it works". A module's first test should assert that SOMETHING IMPORTS IT;
every other test can pass while the module is dead code.

## Two categories, and the difference matters

`LAUNCHED_NOT_IMPORTED` — real code reached another way: a `console_scripts` entrypoint, a
`python -m` target, an ops runbook invocation. Not imported is CORRECT for these.

`KNOWN_INERT` — imported by nothing and launched by nothing. **These are findings, recorded here
rather than hidden.** Allowlisting one silently would rebuild the exact defect this lint exists to
catch. The set may only SHRINK, and its size is pinned.

⛔ NOT a general dead-code detector. It answers one narrow question — is there a path from
production code to this module — and says so out loud when there is not.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "project_mai_tai"
PYPROJECT = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"

# Reached another way. Each entry names HOW, so a wrong one is visible on reading.
LAUNCHED_NOT_IMPORTED: dict[str, str] = {
    "project_mai_tai.backtest.__main__": "python -m project_mai_tai.backtest",
    "project_mai_tai.backtest.dot_entry": "study CLI; referenced from scripts/ and docs/",
    "project_mai_tai.backtest.proximity_sweep": "study CLI; referenced from scripts/ and docs/",
    "project_mai_tai.backtest.study_report": "study CLI; referenced from docs/",
    "project_mai_tai.deploy_preflight": "standalone blocking deploy tooling; has __main__",
    "project_mai_tai.post_restart_health_gate": (
        "Deploy Service invokes it with python -m after each systemd restart"
    ),
    "project_mai_tai.maintenance.reset_active_state": "maintenance CLI; has __main__",
}

# ⛔⭐⭐ IMPORTED BY NOTHING **AND** LAUNCHED BY NOTHING. Findings, not exemptions.
KNOWN_INERT: dict[str, str] = {
    "project_mai_tai.trade_reasons": (
        "Parses `trade_intents.reason` and exists to BAN substring-matching on it — a bug that "
        "produced a wrong answer twice in 24 hours (08-04 inflated a headline 75%; 08-05 reported "
        "385/394 when the truth was 1/394). It has tests and NO consumer: nothing in src/ imports "
        "it, there is no CLI, and no ops/docs reference. So the ban it encodes is not enforced "
        "anywhere — reason strings are still matched by hand at every call site. Found by this "
        "lint on 2026-08-19, the same shape as broker_refusal. Wiring it is open work."
    ),
}


def _module_name(path: pathlib.Path) -> str:
    return path.relative_to(SRC.parent).with_suffix("").as_posix().replace("/", ".")


def _console_script_targets() -> set[str]:
    """Modules named as console_scripts — launched by name, never imported."""
    out: set[str] = set()
    try:
        text = PYPROJECT.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if "=" in line and ":" in line and "project_mai_tai" in line:
            target = line.split("=", 1)[1].strip().strip('"').strip("'")
            out.add(target.split(":")[0])
    return out


def _imported_names() -> set[str]:
    """Every module path referenced by an import anywhere in src/.

    ⛔ `from pkg import mod` must count as importing `pkg.mod`. Missing that form reported
    `strategy_core.entry_gate` as an orphan on the first pass, while `replay.py` imports it on
    line 1 — a false positive that would have made the whole lint untrustworthy.
    """
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
                for alias in node.names:
                    found.add(f"{node.module}.{alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name)
    return found


def find_inert_modules() -> list[str]:
    imported = _imported_names()
    entrypoints = _console_script_targets()
    out = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "__init__.py" or "__pycache__" in str(path):
            continue
        name = _module_name(path)
        if name in imported or name in entrypoints:
            continue
        out.append(name)
    return out


def test_no_new_module_is_imported_by_nothing():
    unexplained = [
        m for m in find_inert_modules() if m not in LAUNCHED_NOT_IMPORTED and m not in KNOWN_INERT
    ]
    assert unexplained == [], (
        "these modules are imported by nothing in src/ and are not launched entrypoints — "
        "they are doing NO WORK however green their tests are:\n  "
        + "\n  ".join(unexplained)
        + "\n\nIf a module is reached another way, add it to LAUNCHED_NOT_IMPORTED naming HOW. "
        "If it is genuinely unwired, that is a finding — wire it, or record it in KNOWN_INERT."
    )


def test_known_inert_set_may_only_shrink():
    """⛔ Pinned so adding one is a deliberate, reviewed edit rather than a way to silence the lint."""
    assert len(KNOWN_INERT) == 1
    assert all(len(reason) > 120 for reason in KNOWN_INERT.values()), (
        "a KNOWN_INERT entry must explain what the module was for and what is lost while it is dead"
    )


def test_every_launched_entry_says_how_it_is_launched():
    assert all(len(reason) > 15 for reason in LAUNCHED_NOT_IMPORTED.values())


def test_broker_refusal_is_actually_wired():
    """⛔ The module this lint was written for. It shipped green and inert for a day."""
    assert "project_mai_tai.backtest.broker_refusal" in _imported_names()


def test_the_detector_actually_detects():
    """⛔⭐ Drive it with a known-bad tape: a brand-new module nothing imports."""
    victim = SRC / "zz_lint_inert_selfcheck_tmp.py"
    victim.write_text('"""temp."""\n\nVALUE = 1\n', encoding="utf-8")
    try:
        assert "project_mai_tai.zz_lint_inert_selfcheck_tmp" in find_inert_modules()
    finally:
        victim.unlink()


def test_the_detector_does_not_flag_an_imported_module():
    """A module imported via `from pkg import mod` must NOT be flagged — the first-pass false positive."""
    assert "project_mai_tai.strategy_core.entry_gate" not in find_inert_modules()
