from __future__ import annotations

import subprocess
from pathlib import Path


SCRIPT = Path("ops/systemd/install_v2_atr_massive_seed.sh")


def test_installer_is_after_close_flagged_v2_only_and_evidence_backed() -> None:
    text = SCRIPT.read_text()

    assert "refusing v2 ATR Massive seed installation before 16:00 ET" in text
    assert "committed G5 report is not PASS" in text
    assert "grep -Fxq '**G5 verdict: PASS**'" in text
    assert 'FLAG="MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_MASSIVE_SEED_ENABLED"' in text
    assert "expected exactly one non-empty MAI_TAI_MASSIVE_API_KEY" in text
    assert 'deploy_service.sh" "$REPO_DIR" main schwab-1m-v2' in text
    assert "snapshot --output" in text
    assert "--restarted schwab-1m-v2" in text
    assert "--no-schema-change" in text
    assert "[V2-ATR-SEED-CENSUS]" in text
    assert "set ${FLAG}=false exactly once" in text
    for forbidden in (" main oms", " main strategy", " main market-data", "restart_all.sh"):
        assert forbidden not in text


def test_installer_has_valid_shell_syntax() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_installer_refuses_the_measured_g5_failure_before_host_actions(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    report = repo / "docs/review-artifacts/v2-atr-massive-seed/replay-30.md"
    report.parent.mkdir(parents=True)
    report.write_text("**G5 verdict: FAIL**\n")

    result = subprocess.run(
        ["bash", str(SCRIPT), str(repo), str(tmp_path / "unused.env")],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert "committed G5 report is not PASS" in result.stdout
