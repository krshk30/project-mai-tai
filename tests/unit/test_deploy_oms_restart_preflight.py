from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "ops/systemd/deploy_service.sh"


def _preflight_function() -> str:
    source = DEPLOY.read_text(encoding="utf-8")
    match = re.search(
        r"^run_oms_restart_preflight\(\) \{\n.*?^\}",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "the deploy's real OMS preflight helper must remain extractable"
    return match.group(0)


def _run_extracted_helper(tmp_path: Path, fence_rc: int | None) -> subprocess.CompletedProcess[str]:
    fence = tmp_path / "ops/preflight/preflight_oms_restart.sh"
    if fence_rc is not None:
        fence.parent.mkdir(parents=True)
        fence.write_text(f"#!/usr/bin/env bash\nexit {fence_rc}\n", encoding="utf-8")
        fence.chmod(0o755)
    script = f"""
set -euo pipefail
REPO_DIR={tmp_path}
{_preflight_function()}
run_oms_restart_preflight
echo RESTART_REACHED
"""
    return subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )


def test_refusing_preflight_stops_before_restart(tmp_path: Path) -> None:
    result = _run_extracted_helper(tmp_path, fence_rc=1)
    assert result.returncode == 1
    assert "RESTART_REACHED" not in result.stdout


def test_missing_preflight_fails_closed(tmp_path: Path) -> None:
    result = _run_extracted_helper(tmp_path, fence_rc=None)
    assert result.returncode == 2
    assert "missing or not executable" in result.stderr
    assert "RESTART_REACHED" not in result.stdout


def test_passing_preflight_reaches_restart(tmp_path: Path) -> None:
    result = _run_extracted_helper(tmp_path, fence_rc=0)
    assert result.returncode == 0
    assert "RESTART_REACHED" in result.stdout


def test_oms_deploy_stops_strategy_then_fences_then_restarts() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    dispatch = source.rsplit('case "$SERVICE_TARGET" in', maxsplit=1)[1]
    oms_branch = dispatch.split("  oms)", maxsplit=1)[1].split("    ;;", maxsplit=1)[0]

    stop = oms_branch.index('stop_unit "project-mai-tai-strategy.service"')
    preflight = oms_branch.index("run_oms_restart_preflight")
    restart = oms_branch.index('restart_unit "$PRIMARY_UNIT"')
    assert stop < preflight < restart


def test_market_data_deploy_does_not_gain_the_oms_fence() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    dispatch = source.rsplit('case "$SERVICE_TARGET" in', maxsplit=1)[1]
    market_data_branch = dispatch.split("  market-data)", maxsplit=1)[1].split(
        "    ;;", maxsplit=1
    )[0]
    assert "run_oms_restart_preflight" not in market_data_branch
