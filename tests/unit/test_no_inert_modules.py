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
    # C1 LATEST C11821 2026-10-27 18:21: reviewed stopped-writer install helper.
    "project_mai_tai.brokerdecides1_install_chunks": (
        "stopped-writer install API imported by tools/l12_private_full23_lifecycle.py:231; "
        "not an ordinary runtime or admission entrypoint"
    ),
    "project_mai_tai.backtest.__main__": "python -m project_mai_tai.backtest",
    "project_mai_tai.backtest.confirmed_selection_census": (
        "read-only after-close selection study; launched with python -m and referenced from docs/"
    ),
    "project_mai_tai.backtest.dot_entry": "study CLI; referenced from scripts/ and docs/",
    "project_mai_tai.backtest.momentum_live_rule_baseline": (
        "read-only after-close Momentum tape capture and offline replay; launched with python -m "
        "and referenced from docs/review-artifacts/"
    ),
    "project_mai_tai.backtest.momentum_gateway_throughput": (
        "measured by the Step 2 protocol; not wired; launched after hours with python -m"
    ),
    "project_mai_tai.backtest.pre0700_shadow": (
        "read-only after-close PRE07 study; launched with python -m and referenced from docs/"
    ),
    "project_mai_tai.backtest.proximity_sweep": "study CLI; referenced from scripts/ and docs/",
    "project_mai_tai.backtest.retry_one": (
        "read-only after-close RETRY-ONE causal study; launched with python -m and referenced "
        "from docs/review-artifacts/retry-one/"
    ),
    "project_mai_tai.backtest.study_report": "study CLI; referenced from docs/",
    "project_mai_tai.deploy_preflight": "standalone blocking deploy tooling; has __main__",
    "project_mai_tai.post_restart_health_gate": (
        "Deploy Service invokes it with python -m after each systemd restart"
    ),
    "project_mai_tai.maintenance.reset_active_state": "maintenance CLI; has __main__",
    "project_mai_tai.momentum_gateway_handoff": ("measured by the Step 2 protocol; not wired"),
    "project_mai_tai.atr_simulation.firstbar_comparison": (
        "imported only by tools/atr_firstbar_study_replay.py (hand-run FIRSTBAR study replay); not runtime"
    ),
    "project_mai_tai.atr_simulation.firstbar_live_baseline": (
        "ATRSIMLIVE1 offline study; imported only by the hand-run tools/atrsimlive1*.py CLIs (10-31: the box ATR page timer runs tools/atrsim2_job.py instead)"
    ),
    "project_mai_tai.atr_simulation.same_sip_binding": (
        "ATRSIMLIVE1 offline study; imported only by the hand-run tools/atrsimlive1*.py CLIs (10-31: the box ATR page timer runs tools/atrsim2_job.py instead)"
    ),
    "project_mai_tai.atr_simulation.same_sip_engine": (
        "imported only by tools/atrsimlive1_models.py (ATRSIMLIVE1 hand-run study); not runtime"
    ),
    "project_mai_tai.atr_simulation.same_sip_firstbar_live_baseline": (
        "ATRSIMLIVE1 offline study; imported only by the hand-run tools/atrsimlive1*.py CLIs (10-31: the box ATR page timer runs tools/atrsim2_job.py instead)"
    ),
    "project_mai_tai.services.atr_simulation_firstbar_worker": (
        "has __main__ (python -m); 10-31 no systemd unit installed on the box, so it runs only by hand"
    ),
    "project_mai_tai.phone_alert_policy": (
        "imported by ops/health/phone_alert_gate.py, low_priority_alerts.py, orphan_order_check.py (root cron on the box)"
    ),
    "project_mai_tai.reconciliation.operator_closed_exits": (
        "imported by ops/health/oco_capture_check.py (oco_capture_cron.sh, root cron */15 on the box)"
    ),
    "project_mai_tai.services.market_data_snapshot_worker": (
        "python -m via ops/systemd/project-mai-tai-snapshot-worker.service (enabled + active on the box 10-31)"
    ),
}

# ⛔⭐⭐ IMPORTED BY NOTHING **AND** LAUNCHED BY NOTHING. Findings, not exemptions.
KNOWN_INERT: dict[str, str] = {
    "project_mai_tai.momentum_live_routing": (
        "2026-10-15 00:1x reviewed temporary finding: the former type/ineligible routing "
        "proposal was superseded before deployment by momentum_live_sec.CountryRouter. "
        "No runtime imports this module and its policy is not active. Remove in 10-15 close PR; "
        "do not wire it back or change installed source tonight."
    ),
    "project_mai_tai.trade_reasons": (
        "Parses `trade_intents.reason` and exists to BAN substring-matching on it — a bug that "
        "produced a wrong answer twice in 24 hours (08-04 inflated a headline 75%; 08-05 reported "
        "385/394 when the truth was 1/394). It has tests and NO consumer: nothing in src/ imports "
        "it, there is no CLI, and no ops/docs reference. So the ban it encodes is not enforced "
        "anywhere — reason strings are still matched by hand at every call site. Found by this "
        "lint on 2026-08-19, the same shape as broker_refusal. Wiring it is open work."
    ),
    "project_mai_tai.oms.bot_control_cleanup": (
        "10-31 claude-1 finding: KILLSW1 pure cleanup planner (plan_cleanup) shipped in the 10-16 install #1163 and imported by nothing; the live KILLSW1 cleanup refusals live in momentum_live/runtime.py instead. Wire it or delete it in a cleanup PR; nothing live depends on it today."
    ),
    "project_mai_tai.momentum_paper.case_collision_append": (
        "10-31 claude-1 finding: cold reviewed append caller (no DSN/CLI) shipped 10-15 in #1160 for the case-collision repair; no tool, CLI or runtime imports it, so the reviewed append path cannot run. Delete in a cleanup PR or wire it to its tool."
    ),
    "project_mai_tai.atr_simulation.firstbar_study_publication": (
        "10-31 claude-1 finding: ATRSIMLIVE1 FIRSTBAR study publication step; named only in tools/atrsimlive1_failure_literals.json and imported by no tool or runtime, so the publication it encodes never runs. Superseded by tools/atrsim2_job.py dashboard_snapshots."
    ),
    "project_mai_tai.oms.webull_identity_copy_audit": (
        "10-31 claude-1 finding: Exact-13 Webull metadata repair rehearsal CLI (__main__, frozen files only); no launcher or runbook on the box invokes it, so the rehearsal it encodes never runs. Wire it to a reviewed runbook or delete it."
    ),
    "project_mai_tai.services.momentum_live_replay": (
        "10-31 claude-1 finding: offline retained-detection arithmetic CLI (__main__, never dispatches) shipped 10-15 in #1160; no launcher, cron or runbook on the box invokes it, so its replay check never runs. Wire it or delete it."
    ),
    "project_mai_tai.atr_simulation.today_display": (
        "10-31 claude-1 finding: ATRSIMLIVE1 TODAY display model; named only in tools/atrsimlive1_failure_literals.json and imported by nothing; the live ATR page reads tools/atrsim2_job.py dashboard_snapshots instead. Delete in a cleanup PR."
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
    # Existing reviewed SEC nightly launcher (ea60d9f7) constructs python -m
    # explicitly. Detect that source evidence, not a blanket module exemption.
    launcher = SRC.parents[1] / "ops/momlive1/sec_nightly_runner.py"
    tree = ast.parse(launcher.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "command" for target in node.targets
        ) and isinstance(node.value, ast.List):
            args = node.value.elts
            for index, arg in enumerate(args[:-1]):
                if isinstance(arg, ast.Constant) and arg.value == "-m":
                    module = args[index + 1]
                    if isinstance(module, ast.Constant) and isinstance(module.value, str):
                        out.add(module.value)
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
        package = _module_name(path).split(".")
        if path.name != "__init__.py":
            package = package[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or node.level):
                module = node.module or ""
                if node.level:
                    # `from .entry import x` imports pkg.entry: resolve against this file's package.
                    base = package[: len(package) - node.level + 1]
                    module = ".".join(base + ([module] if module else []))
                found.add(module)
                for alias in node.names:
                    found.add(f"{module}.{alias.name}")
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
    # One dated exception explicitly approved for removal in the 10-15 close PR.
    assert set(KNOWN_INERT) == {
        "project_mai_tai.trade_reasons", "project_mai_tai.momentum_live_routing",
        # 10-31: four findings surfaced once relative imports were resolved (reported to operator).
        "project_mai_tai.oms.bot_control_cleanup",
        "project_mai_tai.momentum_paper.case_collision_append",
        "project_mai_tai.atr_simulation.firstbar_study_publication",
        "project_mai_tai.atr_simulation.today_display",
        "project_mai_tai.oms.webull_identity_copy_audit",
        "project_mai_tai.services.momentum_live_replay",
    }
    assert all(len(reason) > 120 for reason in KNOWN_INERT.values()), (
        "a KNOWN_INERT entry must explain what the module was for and what is lost while it is dead"
    )


def test_every_launched_entry_says_how_it_is_launched():
    assert all(len(reason) > 15 for reason in LAUNCHED_NOT_IMPORTED.values())


def test_sec_nightly_source_launch_does_not_exempt_unresolved_callers():
    targets = _console_script_targets()
    assert "project_mai_tai.services.momentum_live_sec_prepare" in targets
    for unresolved in (
        "project_mai_tai.momentum_paper.case_collision_append",
        "project_mai_tai.oms.webull_identity_copy_audit",
        "project_mai_tai.services.momentum_live_replay",
    ):
        assert unresolved not in targets
        assert unresolved not in LAUNCHED_NOT_IMPORTED
        # 10-31: recorded as open findings (KNOWN_INERT), never as a launch exemption.
        assert unresolved in KNOWN_INERT


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
