#!/usr/bin/env python3
"""Main after close only: actual deployed policy/logrotate, synthetic copy targets."""
import argparse
from contextlib import ExitStack
import grp
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile

from chgrp_exact_four import NAMES, apply_four
from policy_contract import isolated_policy, normalize


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def metadata(path):
    row = path.stat()
    return (row.st_uid, row.st_gid, stat.S_IMODE(row.st_mode), row.st_ino)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-reviewed-afterclose", action="store_true", required=True)
    parser.add_argument("--deployed-config", type=Path, default=Path("/etc/logrotate.d/project-mai-tai"))
    parser.add_argument("--config-sha256", required=True)
    args = parser.parse_args()
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise RuntimeError("PENDING: main executes real Linux root copy proof after close; no mock substitute")
    binary = Path("/usr/sbin/logrotate")
    if binary.read_bytes()[:4] != b"\x7fELF":
        raise RuntimeError("actual installed ELF logrotate required")
    raw = args.deployed_config.read_bytes()
    if sha(raw) != args.config_sha256:
        raise RuntimeError("actual deployed config preimage hash mismatch")
    normalize(raw)
    runuser = shutil.which("runuser")
    if runuser is None:
        raise RuntimeError("actual trader read probes require runuser")
    group = grp.getgrnam("trader").gr_gid
    trader = pwd.getpwnam("trader")
    results = []
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="optiona-F2-APPROVED-COPY-")))
        os.chown(root, 0, group)
        os.chmod(root, 0o750)
        logs = root / "root-log-fixtures"
        logs.mkdir(mode=0o750)
        os.chown(logs, 0, group)
        trader_logs = root / "trader-writer-fixtures"
        trader_logs.mkdir(mode=0o750)
        os.chown(trader_logs, trader.pw_uid, trader.pw_gid)
        market = trader_logs / "market-data.log"
        private = logs / "unrelated-private.log"

        def pathname_append(path):
            code = "import os,sys; fd=os.open(sys.argv[1],os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o640); os.write(fd,b'synthetic pathname append\\n'); os.close(fd)"
            rc = subprocess.run([runuser, "-u", "trader", "--", sys.executable, "-c", code, str(path)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode
            if rc:
                raise RuntimeError("original trader pathname writer failed on copy")

        def inherited_append(fd):
            code = ("import os,sys; os.setgroups([]); os.setgid(int(sys.argv[2])); os.setuid(int(sys.argv[3])); "
                    "os.write(int(sys.argv[1]),b'synthetic service append\\n')")
            rc = subprocess.run([sys.executable, "-c", code, str(fd), str(trader.pw_gid), str(trader.pw_uid)],
                                pass_fds=(fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode
            if rc:
                raise RuntimeError("original User=trader held append descriptor failed on copy")

        def readable(path):
            return subprocess.run([runuser, "-u", "trader", "--", "head", "-c", "1", str(path)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0

        fds = {}
        for name in (*NAMES, private.name):
            path = logs / name
            path.write_text("synthetic no production data\n")
            os.chown(path, 0, 0)
            os.chmod(path, 0o640)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            stack.callback(os.close, fd)
            if name in NAMES:
                fds[name] = fd
        pathname_append(market)
        # Single approved group step BEFORE all rotations. No hook/reapply follows.
        apply_four(logs)
        expected = {name: metadata(logs / name) for name in NAMES}
        private_before = metadata(private)
        market_before = metadata(market)
        if market_before[:3] != (trader.pw_uid, trader.pw_gid, 0o640):
            raise RuntimeError("synthetic trader-owned creation fixture invalid")
        victim = NAMES[0]
        for attempt in range(4):
            if attempt == 1:
                market.unlink()
            elif attempt == 2:
                pathname_append(market)
                market_before = metadata(market)
            elif attempt == 3:
                # Explicit boundary control: deletion/recreation is NOT copytruncate.
                path = logs / victim
                path.unlink()
                path.write_text("synthetic new root-created inode\n")
                os.chown(path, 0, 0)
                os.chmod(path, 0o640)
                fd = os.open(path, os.O_WRONLY | os.O_APPEND)
                stack.callback(os.close, fd)
                fds[victim] = fd
                expected[victim] = metadata(path)
            for fd in fds.values():
                inherited_append(fd)
            with private.open("a") as writer:
                writer.write("synthetic original root writer\n")
            if market.exists():
                pathname_append(market)
            config = root / ("copy-" + str(attempt) + ".conf")
            text = isolated_policy(raw, [logs, trader_logs], attempt)
            if "/var/log/project-mai-tai" in text or "postrotate" in text or "setfacl" in text:
                raise RuntimeError("non-isolated path or unauthorized hook in copy policy")
            config.write_text(text)
            os.chmod(config, 0o600)
            rotated = subprocess.run([str(binary), "-f", "-v", "-s", str(root / "state"), str(config)],
                                     text=True, capture_output=True, timeout=30)
            if rotated.returncode:
                raise RuntimeError("real copy force failed; diagnostic SHA256=" + sha(rotated.stderr.encode()))
            rows = []
            for name, fd in fds.items():
                path = logs / name
                inherited_append(fd)
                with path.open("a") as writer:
                    writer.write("synthetic root opener append after force\n")
                row = metadata(path)
                expected_read = not (attempt == 3 and name == victim)
                new_write = subprocess.run([runuser, "-u", "trader", "--", "test", "-w", str(path)], timeout=10).returncode == 0
                if (row != expected[name] or readable(path) != expected_read or new_write
                        or not list(logs.glob(name + "-COPY" + str(attempt) + "*"))):
                    raise RuntimeError("copy changed group/inode/writer/reader invariant: " + name)
                rows.append({"name": name, "uid": row[0], "gid": row[1], "mode": "0640",
                             "same_inode_owner_group_mode": True, "held_service_writer_append": "PASS",
                             "root_opener_append": "PASS", "new_trader_path_write": "DENIED",
                             "trader_read": "PASS" if expected_read else "DENIED_EXPECTED_RECREATION_LIMIT"})
            if attempt == 1:
                if market.exists():
                    raise RuntimeError("inactive create directive unexpectedly root-created missing trader log")
                market_row = {"missing_after_force": True, "unexpected_root_creation": False}
            else:
                pathname_append(market)
                if metadata(market) != market_before or not list(trader_logs.glob("market-data.log-COPY" + str(attempt) + "*")):
                    raise RuntimeError("trader-owned original/new-file writer changed by copy rotation")
                market_row = {"owner_group_mode_inode_preserved": True, "original_trader_append": "PASS"}
            with private.open("a") as writer:
                writer.write("synthetic private original writer after force\n")
            if metadata(private) != private_before or readable(private):
                raise RuntimeError("unrelated private writer/access changed")
            results.append({"actual_logrotate_force_rc": rotated.returncode, "logs": rows,
                            "market_data": market_row, "unrelated_original_writer": "PASS",
                            "group_reapply_after_force": False})
        print(json.dumps({"status": "ACTUAL_COPY_PROOF_PASS_WITH_RECREATION_LIMIT", "scope": "COPY_ONLY",
                          "production_action": False, "config_changed": False, "acl_changed": False,
                          "actual_deployed_config_sha256": sha(raw), "logrotate_binary_sha256": sha(binary.read_bytes()),
                          "runs": results, "deletion_recreation_requires_reviewed_group_step": True,
                          "live_permission_and_F3_receipts": "NOT_EVALUATED"}, indent=2))


if __name__ == "__main__":
    main()
