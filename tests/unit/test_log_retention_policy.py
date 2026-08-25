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


def test_normal_runtime_install_also_installs_retention_policy() -> None:
    runtime_installer = (ROOT / "ops/bootstrap/08_install_runtime.sh").read_text(encoding="utf-8")

    assert 'bash "$REPO_DIR/ops/logrotate/install.sh" "$REPO_DIR"' in runtime_installer


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
