#!/usr/bin/env python3
"""Main's reviewed after-close permission leaf: chgrp only on four named files."""
import argparse
import grp
import json
import os
from pathlib import Path
import platform
import stat

NAMES = ("oms.log", "strategy.log", "schwab-1m-v2.log", "orb-schwab.log")
ROOT = Path("/var/log/project-mai-tai")


def apply_four(root=ROOT):
    if os.geteuid() != 0:
        raise PermissionError("root required for exact-four group change")
    gid = grp.getgrnam("trader").gr_gid
    if root.is_symlink() or not root.is_dir():
        raise ValueError("log directory absent or symlink")
    opened = []
    before = {}
    try:
        for name in NAMES:
            fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            opened.append((name, fd))
            row = os.fstat(fd)
            if (not stat.S_ISREG(row.st_mode) or row.st_nlink != 1 or row.st_uid != 0
                    or row.st_gid not in {0, gid} or stat.S_IMODE(row.st_mode) != 0o640):
                raise ValueError("unexpected existing owner/group/mode/type: " + name)
            before[name] = (row.st_dev, row.st_ino, row.st_uid, stat.S_IMODE(row.st_mode))
        for _, fd in opened:
            os.fchown(fd, -1, gid)
        rows = []
        for name, fd in opened:
            row = os.fstat(fd)
            current = os.stat(root / name, follow_symlinks=False)
            if ((row.st_dev, row.st_ino, row.st_uid, stat.S_IMODE(row.st_mode)) != before[name]
                    or row.st_gid != gid or (current.st_dev, current.st_ino) != before[name][:2]):
                raise ValueError("log identity/owner/mode changed during group step: " + name)
            rows.append({"name": name, "uid": row.st_uid, "gid": row.st_gid,
                         "mode": "0640", "owner_mode_inode_preserved": True})
        return {"status": "PASS", "scope": "EXACT_FOUR_CHGRP_ONLY",
                "actual_box": root == ROOT and platform.system() == "Linux", "logs": rows,
                "service_user_read": "NOT_EVALUATED", "rotation_proof": "NOT_RUN",
                "config_changed": False, "acl_changed": False}
    finally:
        for _, fd in opened:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-reviewed-afterclose", action="store_true", required=True)
    parser.parse_args()
    # Main enforces its existing fresh time/trading gates before calling this leaf.
    print(json.dumps(apply_four(), indent=2))


if __name__ == "__main__":
    main()
