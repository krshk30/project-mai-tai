"""Evaluate real Perl regexes; install only into a temporary filesystem."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "ops/bootstrap/unattended-upgrades"
POLICY = "99-project-mai-tai-no-autorestart.conf"
INSTALLER = ROOT / "ops/bootstrap/11_install_unattended_upgrade_policy.sh"


@pytest.mark.parametrize("unit,selected", [
    ("postgresql.service", 0), ("postgresql@16-main.service", 0),
    ("postgresql@17-other-cluster.service", 0),
    ("project-mai-tai-oms.service", 0), ("project-mai-tai-control.service", 0),
    ("project-mai-tai-orb-schwab.service", 0),
    ("postgresql-exporter.service", 1), ("not-postgresql.service", 1),
    ("postgresql.service.extra", 1), ("postgresql@16-main.service.extra", 1),
    ("postgresql@16-main/other.service", 1), ("postgresql@.service", 1),
    ("other-project-mai-tai-oms.service", 1), ("apt-daily-upgrade.service", 1),
    ("redis-server.service", 1), ("ssh.service", 0),
])
def test_needrestart_automatic_selection_defers_only_protected_units(unit, selected):
    # Same first-match semantics as needrestart's automatic restart branch.
    program = r'''
        use JSON::PP;
        our %nrconf = (restart => 'a', override_rc => {qr(^ssh\.service$) => 0});
        my $result = do $ARGV[0];
        die($@ || $! || "false policy") unless $result;
        my $selected = 1;
        for my $regex (sort keys %{$nrconf{override_rc}}) {
            if ($ARGV[1] =~ /$regex/) { $selected = $nrconf{override_rc}->{$regex}; last; }
        }
        print encode_json({selected => $selected, restart => $nrconf{restart},
                          overrides => scalar keys %{$nrconf{override_rc}},
                          blacklist => exists $nrconf{blacklist_rc} ? 1 : 0});
    '''
    result = subprocess.run(["perl", "-e", program, str(SOURCE / POLICY), unit],
                            check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == {
        "selected": selected, "restart": "a", "overrides": 3, "blacklist": 0,
    }


@pytest.fixture
def installer_environment(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "ops/bootstrap/unattended-upgrades"
    shutil.copytree(SOURCE, source)
    commands = tmp_path / "bin"
    commands.mkdir()
    tool = commands / "tool"
    tool.write_text(f"#!{sys.executable}\n" + '''
import os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
if name == "systemctl":
    with open(os.environ["COMMAND_LOG"], "a") as log:
        log.write(" ".join(sys.argv[1:]) + "\\n")
elif name == "apt-config":
    print(pathlib.Path(sys.argv[2]).read_text())
elif name == "stat":
    st = pathlib.Path(sys.argv[-1]).stat()
    print(f"{st.st_mode & 0o777:o}:{st.st_uid}:{st.st_gid}")
''')
    tool.chmod(0o755)
    for name in ("systemctl", "systemd-analyze", "apt-config", "stat"):
        (commands / name).symlink_to(tool)
    etc = tmp_path / "etc"
    libexec = tmp_path / "libexec"
    log = tmp_path / "commands.log"
    env = {**os.environ, "PATH": f"{commands}:{os.environ['PATH']}",
           "MAI_TAI_NO_SUDO": "1", "MAI_TAI_ETC_DIR": str(etc),
           "MAI_TAI_LIBEXEC_DIR": str(libexec), "MAI_TAI_SKIP_RUNTIME_VERIFY": "1",
           "MAI_TAI_SYSTEMCTL_BIN": str(commands / "systemctl"),
           "MAI_TAI_SYSTEMD_ANALYZE_BIN": str(commands / "systemd-analyze"),
           "MAI_TAI_APT_CONFIG_BIN": str(commands / "apt-config"), "COMMAND_LOG": str(log)}
    return repo, source, etc, libexec, log, env


def install(environment):
    repo, _, _, _, _, env = environment
    return subprocess.run(["bash", str(INSTALLER), str(repo)], env=env,
                          capture_output=True, text=True)


@pytest.mark.skipif(sys.platform == "darwin", reason="installer requires Linux Bash 4+ (CI)")
def test_installer_delivers_policy_atomically_and_idempotently_without_runtime_restart(
        installer_environment):
    _, source, etc, libexec, log, _ = installer_environment
    result = install(installer_environment)
    assert result.returncode == 0, result.stderr
    target = etc / "needrestart/conf.d" / POLICY
    assert target.read_bytes() == (source / POLICY).read_bytes()
    assert target.stat().st_mode & 0o777 == 0o644
    inode = target.stat().st_ino
    security_policy = etc / "apt/apt.conf.d/52-project-mai-tai-unattended-upgrades"
    assert security_policy.read_bytes() == (source / security_policy.name).read_bytes()
    assert (libexec / "project-mai-tai-unattended-upgrade-notify").exists()
    second = install(installer_environment)
    assert second.returncode == 0, second.stderr
    assert target.stat().st_ino == inode
    assert log.read_text().splitlines() == [
        "daemon-reload", "enable --now apt-daily.timer apt-daily-upgrade.timer",
    ] * 2


@pytest.mark.parametrize("damage", ["missing", "invalid-perl"])
def test_needrestart_preflight_rejects_missing_or_invalid_policy_before_install(
        installer_environment, damage):
    _, source, etc, libexec, log, _ = installer_environment
    policy = source / POLICY
    if damage == "missing":
        policy.unlink()
    else:
        policy.write_text("this is not valid Perl !\n")
    result = install(installer_environment)
    assert result.returncode != 0
    if damage == "missing":
        assert "missing unattended-upgrade policy artifact" in result.stderr
    else:
        assert "syntax error" in result.stderr
    assert not etc.exists() and not libexec.exists() and not log.exists()
