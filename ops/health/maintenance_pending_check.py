#!/usr/bin/env python3
"""Read-only fullscan probe for deferred maintenance/restarts; no service calls."""
import json
from pathlib import Path

FLAG = Path("/var/lib/project-mai-tai-maintenance/pending-restart.json")


def check(flag=FLAG, reboot=Path("/run/reboot-required")):
    try:
        if flag.is_symlink():
            raise ValueError("flag symlink")
        if flag.exists():
            if flag.stat().st_size > 65_536:
                raise ValueError("flag exceeds bound")
            row = json.loads(flag.read_text())
            if (not isinstance(row, dict) or row.get("version") != 1
                    or not isinstance(row.get("reason"), str)):
                raise ValueError("flag malformed")
            return 1, "MAINTENANCE PENDING_RESTART_FLAG " + row["reason"]
        if reboot.exists():
            return 1, "MAINTENANCE PENDING_RESTART_FLAG reboot-required"
        return 0, "MAINTENANCE NO_PENDING_FLAG (absence is not runtime health proof)"
    except (OSError, ValueError, TypeError):
        return 2, "MAINTENANCE UNKNOWN pending-restart evidence unreadable"


if __name__ == "__main__":
    code, message = check()
    print(message)
    raise SystemExit(code)
