import os
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
POLICY_DIR = ROOT / "ops/bootstrap/unattended-upgrades"


@pytest.mark.skipif(os.name == "nt", reason="requires Linux apt and systemd tooling")
def test_installer_and_notifier_fault_controls() -> None:
    subprocess.run(
        ["bash", "ops/bootstrap/test_unattended_upgrade_policy.sh"],
        cwd=ROOT,
        check=True,
    )


def test_blacklist_covers_servers_and_loaded_client_libraries() -> None:
    policy = (POLICY_DIR / "52-project-mai-tai-unattended-upgrades").read_text()

    assert '"^postgresql$";' in policy
    assert '"^postgresql-.*$";' in policy
    assert '"^libpq[0-9]+$";' in policy
    assert '"^openssl$";' in policy
    assert '"^libssl[0-9].*$";' in policy

    patterns = re.findall(r'^\s+"([^"]+)";', policy, flags=re.MULTILINE)
    protected = ("postgresql", "postgresql-16", "libpq5", "openssl", "libssl3t64")
    allowed = ("curl", "libcurl3t64-gnutls", "python3.12")
    assert all(any(re.fullmatch(pattern, package) for pattern in patterns) for package in protected)
    assert all(not any(re.fullmatch(pattern, package) for pattern in patterns) for package in allowed)


def test_timers_are_dst_safe_explicit_and_never_catch_up_in_market_hours() -> None:
    download = (POLICY_DIR / "apt-daily.timer.override.conf").read_text()
    install = (POLICY_DIR / "apt-daily-upgrade.timer.override.conf").read_text()

    assert "01:30:00 America/New_York" in download
    assert "02:30:00 America/New_York" in install
    for timer in (download, install):
        assert "OnCalendar=" in timer
        assert "RandomizedDelaySec=0" in timer
        assert "Persistent=false" in timer


def test_notification_is_bound_to_package_change_or_failure() -> None:
    notifier = (
        POLICY_DIR / "project-mai-tai-unattended-upgrade-notify"
    ).read_text()
    drop_in = (
        POLICY_DIR / "apt-daily-upgrade.service.notify.conf"
    ).read_text()

    assert "ExecStartPre=" in drop_in
    assert "ExecStopPost=" in drop_in
    assert "SERVICE_RESULT" in drop_in
    assert "packages.before" in notifier
    assert "packages.after" in notifier
    assert "log_offset" in notifier
    assert " ERROR |Traceback|Exception:" in notifier
    assert "NO_CHANGE" in notifier
    assert "https://ntfy.sh/mai-tai-preopen-28806a5a97b7" in notifier
    assert "--fail --silent --show-error --retry 3 --max-time 20" in notifier
