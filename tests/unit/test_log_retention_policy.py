from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_policy_keeps_complete_application_logs_for_30_days() -> None:
    policy = (ROOT / "ops/logrotate/project-mai-tai").read_text(encoding="utf-8")

    assert "/var/log/project-mai-tai/*.log" in policy
    assert "rotate 30" in policy
    assert "daily" in policy
    assert "copytruncate" in policy
    assert "compress" in policy
    assert "maxsize 200M" in policy
    assert "grep" not in policy


def test_application_runtime_install_does_not_manage_retention_policy() -> None:
    runtime_installer = (ROOT / "ops/bootstrap/08_install_runtime.sh").read_text(encoding="utf-8")

    assert "ops/logrotate" not in runtime_installer


def test_retention_reconciles_automatically_without_touching_application_runtime() -> None:
    workflow = (ROOT / ".github/workflows/log-retention-reconcile.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "schedule:" in workflow
    assert 'cron: "30 22 * * *"' in workflow
    assert "TZ=America/New_York" in workflow
    assert "environment: production" in workflow
    assert "actions/checkout@v4" in workflow
    assert "ops/logrotate/install.sh" in workflow
    assert "ops/logrotate/project-mai-tai" in workflow
    assert "/tmp/project-mai-tai-logrotate." in workflow
    assert "/home/trader/project-mai-tai" not in workflow
    assert "deploy_service.sh" not in workflow
    assert "systemctl restart" not in workflow
    assert "systemctl stop" not in workflow
    assert "pip install" not in workflow
    assert "alembic" not in workflow
    assert "git fetch" not in workflow
    assert "git checkout" not in workflow
    assert "git pull" not in workflow
    assert "git merge" not in workflow


def test_installer_enables_and_verifies_daily_timer() -> None:
    installer = (ROOT / "ops/logrotate/install.sh").read_text(encoding="utf-8")

    assert "enable --now logrotate.timer" in installer
    assert "is-enabled --quiet logrotate.timer" in installer
    assert "is-active --quiet logrotate.timer" in installer
    assert 'cmp -s "$SOURCE" "$TARGET"' in installer
    assert installer.index('--debug "$SOURCE"') < installer.index('mv -f "$tmp" "$TARGET"')
    assert 'target_parent=$(dirname "$target_dir")' in installer
    assert 'mktemp "$target_parent/' in installer
    assert 'mktemp "$target_dir/' not in installer
    assert "logrotate did not parse the project-mai-tai log pattern" in installer
    assert "no replacement needed" in installer
