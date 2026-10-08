from __future__ import annotations

import subprocess
import stat
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BUILD_ENV = ROOT / "ops/systemd/build_orb_paper_env.sh"
ORB_UNIT = ROOT / "ops/systemd/project-mai-tai-orb.service"


def test_orb_unit_has_no_shared_fleet_environment_or_broker_credential_path() -> None:
    unit = ORB_UNIT.read_text()

    assert "EnvironmentFile=/etc/project-mai-tai/orb-paper.env" in unit
    assert "EnvironmentFile=/etc/project-mai-tai/project-mai-tai.env" not in unit
    assert "PassEnvironment" not in unit


def test_orb_environment_builder_excludes_broker_credentials_and_enables_observation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fleet.env"
    target = tmp_path / "orb.env"
    source.write_text(
        "\n".join(
            (
                "MAI_TAI_ENVIRONMENT=production",
                "MAI_TAI_DATABASE_URL=postgresql://paper-writer",
                "MAI_TAI_REDIS_URL=redis://localhost/0",
                "MAI_TAI_REDIS_STREAM_PREFIX=mai_tai",
                "MAI_TAI_ORB_ENABLED=false",
                "MAI_TAI_ORB_RUNNING_HIGH_ENABLED=false",
                "MAI_TAI_ORB_RESTING_ENTRY_ENABLED=false",
                "MAI_TAI_ORB_PAPER_LIFECYCLE_ENABLED=false",
                "MAI_TAI_ORB_PAPER_TARGET_PCT=99",
                "MAI_TAI_ORB_PAPER_STOP_PCT=99",
                "MAI_TAI_ORB_PAPER_MIN_BREAK_BODY_PCT=99",
                "MAI_TAI_ORB_PAPER_ATR_EXIT_ENABLED=false",
                "MAI_TAI_ORB_PAPER_ATR_ENTRY_GATE_ENABLED=true",
                "MAI_TAI_ORB_PAPER_FOUR_RED_DELAY_ENABLED=true",
                "MAI_TAI_ORB_WINDOW_FLATTEN_ENABLED=true",
                "MAI_TAI_ORB_BROKER_ACCOUNT_NAME=live:orb",
                "MAI_TAI_ORB_BROKER_PROVIDER=webull",
                "MAI_TAI_SERVICE_DB_TIMEOUTS_ENABLED=true",
                "MAI_TAI_SCHWAB_CLIENT_SECRET=must-not-cross",
                "MAI_TAI_SCHWAB_ACCOUNT_HASH=must-not-cross",
                "MAI_TAI_WEBULL_APP_SECRET=must-not-cross",
                "MAI_TAI_ALPACA_MACD_30S_SECRET_KEY=must-not-cross",
                "MAI_TAI_MASSIVE_API_KEY=must-not-cross",
                "MAI_TAI_OMS_ADAPTER=schwab",
            )
        )
        + "\n"
    )

    subprocess.run((str(BUILD_ENV), str(source), str(target)), check=True)
    rendered = target.read_text()

    assert "MAI_TAI_DATABASE_URL=postgresql://paper-writer" in rendered
    assert "MAI_TAI_REDIS_URL=redis://localhost/0" in rendered
    assert "MAI_TAI_ORB_RUNNING_HIGH_ENABLED=true" in rendered
    assert "MAI_TAI_ORB_RESTING_ENTRY_ENABLED=true" in rendered
    assert "MAI_TAI_SERVICE_DB_TIMEOUTS_ENABLED=true" in rendered
    assert rendered.count("MAI_TAI_ORB_ENABLED=true") == 1
    assert rendered.count("MAI_TAI_ORB_RUNNING_HIGH_ENABLED=true") == 1
    assert rendered.count("MAI_TAI_ORB_RESTING_ENTRY_ENABLED=true") == 1
    assert rendered.count("MAI_TAI_ORB_PAPER_LIFECYCLE_ENABLED=true") == 1
    assert rendered.count("MAI_TAI_ORB_PAPER_TARGET_PCT=5.0") == 1
    assert rendered.count("MAI_TAI_ORB_PAPER_STOP_PCT=8.0") == 1
    assert rendered.count("MAI_TAI_ORB_PAPER_MIN_BREAK_BODY_PCT=45.0") == 1
    assert rendered.count("MAI_TAI_ORB_PAPER_ATR_EXIT_ENABLED=true") == 1
    assert rendered.count("MAI_TAI_ORB_PAPER_ATR_ENTRY_GATE_ENABLED=true") == 1
    assert rendered.count("MAI_TAI_ORB_PAPER_FOUR_RED_DELAY_ENABLED=true") == 1
    assert rendered.count("MAI_TAI_ORB_WINDOW_FLATTEN_ENABLED=false") == 1
    assert "must-not-cross" not in rendered
    assert "MAI_TAI_OMS_ADAPTER" not in rendered
    assert "MAI_TAI_ORB_BROKER_ACCOUNT_NAME" not in rendered
    assert "MAI_TAI_ORB_BROKER_PROVIDER" not in rendered
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_runtime_installer_never_regenerates_or_enables_retired_paper_process() -> None:
    installer = (ROOT / "ops/bootstrap/08_install_runtime.sh").read_text()

    assert "build_orb_paper_env.sh" not in installer
    assert "project-mai-tai-orb.service" not in installer
    assert "project-mai-tai-orb.service" not in (ROOT / "ops/systemd/project-mai-tai.target").read_text()
    assert "project-mai-tai-orb.service" not in (ROOT / "ops/systemd/install_units.sh").read_text()


def test_only_live_orb_has_a_post_close_deploy_path_with_its_own_health_identity() -> None:
    deploy = (ROOT / "ops/systemd/deploy_service.sh").read_text()

    assert 'PRIMARY_UNIT="project-mai-tai-orb-schwab.service"' in deploy
    assert 'project-mai-tai-orb-schwab.service) echo "orb-schwab"' in deploy
    assert "project-mai-tai-orb.service" not in deploy
