from __future__ import annotations

import subprocess
from pathlib import Path


SCRIPT = Path("ops/systemd/install_v2_atr_massive_seed.sh")


def test_installer_is_after_close_flagged_v2_only_and_evidence_backed() -> None:
    text = SCRIPT.read_text()

    assert "refusing v2 ATR Massive seed installation before 16:00 ET" in text
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
