from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "ops/systemd/deploy_service.sh"
PREFLIGHT = ROOT / "ops/preflight/preflight_oms_restart.sh"


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


def test_strict_flatness_mode_includes_protected_manual_positions(tmp_path: Path) -> None:
    env_file = tmp_path / "project-mai-tai.env"
    env_file.write_text(
        "MAI_TAI_DATABASE_URL=postgresql://user:password@localhost/project_mai_tai\n"
        "MAI_TAI_PROTECTED_SYMBOLS=MANUAL\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    protected_read = tmp_path / "protected-symbols-read"
    sudo = bin_dir / "sudo"
    sudo.write_text(
        "#!/usr/bin/env bash\n"
        f"case \"$*\" in *MAI_TAI_PROTECTED_SYMBOLS*) touch {protected_read} ;; esac\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    sudo.chmod(0o755)
    psql = bin_dir / "psql"
    psql.write_text(
        """#!/usr/bin/env bash
query="$*"
case "$query" in
  *"SELECT 1"*) echo 1 ;;
  *"count(*) FROM oms_managed_positions"*) echo 0 ;;
  *"now()-max(ap.updated_at)"*) echo 1 ;;
  *"ap.quantity <> 0"*)
    case "$query" in
      *"NOT IN ('')"*) echo MANUAL=1 ;;
      *) echo '' ;;
    esac
    ;;
esac
""",
        encoding="utf-8",
    )
    psql.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "MAI_TAI_ENV_FILE": str(env_file),
    }

    normal = subprocess.run(
        ["bash", str(PREFLIGHT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert protected_read.exists(), "the default deploy path must still read its exclusions"
    protected_read.unlink()
    strict = subprocess.run(
        ["bash", str(PREFLIGHT), "--require-all-account-positions-flat"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert normal.returncode == 0
    assert "operator manuals excluded" in normal.stdout
    assert strict.returncode == 1
    assert "strict all-account-position flatness enabled" in strict.stdout
    assert "live:schwab_1m_v2 NOT FLAT" in strict.stdout
    assert "MANUAL=1" in strict.stdout
    assert "===> NO-GO. A position is open" in strict.stdout
    assert not protected_read.exists(), "strict study mode must not read or apply exclusions"

    # C1 C11916: force EH only in the existing controlled command fixture.
    date = bin_dir / "date"
    date.write_text(
        '#!/usr/bin/env bash\ncase "$1" in\n'
        '  +%H) echo 18 ;;\n  +%M) echo 00 ;;\n'
        '  *) echo "2026-10-27 18:00:00 EDT" ;;\nesac\n',
        encoding="utf-8",
    )
    date.chmod(0o755)
    extended = subprocess.run(
        ["bash", str(PREFLIGHT), "--require-all-account-positions-flat"],
        check=False, capture_output=True, text=True, env=env,
    )
    assert extended.returncode == 1
    assert "===> NO-GO. A position is open" in extended.stdout
    assert "EXTENDED HOURS" in extended.stdout
    mismatch = subprocess.run(
        ["bash", str(PREFLIGHT), "--require-all-account-positions-flat",
         "--operator-override", "OTHER", "--i-accept-naked-position"],
        check=False, capture_output=True, text=True, env=env,
    )
    assert mismatch.returncode == 1
    assert "===> NO-GO. A position is open" in mismatch.stdout
    assert "named set does not match" in mismatch.stdout


@pytest.mark.parametrize("query_fails", [
    pytest.param(True, id="query-fail-STOP"),
    pytest.param(False, id="fresh-flat-pass"),
])
def test_actual_preflight_never_reads_failed_position_query_as_flat(tmp_path: Path, query_fails: bool) -> None:
    # C11853 A3: use the same controlled PATH ports as the existing strict-flat test.
    # No PG/provider: only Main executes these two cases against the actual script.
    env_file = tmp_path / "preflight.env"
    env_file.write_text(
        "MAI_TAI_DATABASE_URL=postgresql://user:password@localhost/project_mai_tai\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/usr/bin/env bash\nexec "$@"\n', encoding="utf-8")
    sudo.chmod(0o755)
    psql = bin_dir / "psql"
    psql.write_text(
        """#!/usr/bin/env bash
query="$*"
case "$query" in
  *"SELECT 1"*) echo 1 ;;
  *"count(*) FROM oms_managed_positions"*) echo 0 ;;
  *"now()-max(ap.updated_at)"*) echo 1 ;;
  *"ap.quantity <> 0"*)
    if [[ "$query" == *"live:orb"* && "$CONTROLLED_POSITION_QUERY_FAIL" == 1 ]]; then
      echo "CONTROLLED query error; not account flatness" >&2
      exit 2
    fi
    printf '\\n'
    ;;
  *) exit 97 ;;
esac
""",
        encoding="utf-8",
    )
    psql.chmod(0o755)
    result = subprocess.run(
        ["bash", str(PREFLIGHT), "--require-all-account-positions-flat"],
        check=False, capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "MAI_TAI_ENV_FILE": str(env_file),
             "CONTROLLED_POSITION_QUERY_FAIL": "1" if query_fails else "0"},
    )
    if query_fails:
        assert result.returncode == 2
        assert "[BLIND] live:orb: position query failed; flatness UNKNOWN" in result.stdout
        assert "NO-GO (CANNOT SEE)" in result.stdout
        assert "live:orb flat" not in result.stdout
        assert "Safe to restart" not in result.stdout and "===> GO" not in result.stdout
        assert "CONTROLLED query error" not in result.stdout + result.stderr
    else:
        assert result.returncode == 0
        assert "live:schwab_1m_v2 flat [1s old]" in result.stdout
        assert "live:orb flat [1s old]" in result.stdout
        assert "===> GO. Flat on every real-money account" in result.stdout
        assert "[BLIND]" not in result.stdout
