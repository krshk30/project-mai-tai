"""Exact-head review barrier for the October 3 box one-shot."""
import hashlib
import json
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path("/home/trader/after-hours/2026-10-03/sizing-nfq1-job")
APPROVED_SHA = "608339894a1cfb33284e695196df55c18f312889"
ARTIFACTS = {"INSTALL_PLAN_2026-10-03.md", "run_install_2026-10-03.sh",
             "install_evidence_2026-10-03.py", "verify_install_approval.py",
             "project-mai-tai-sizing-nfq1-install-20261003.service",
             "project-mai-tai-sizing-nfq1-install-20261003.timer"}


def verify(root=ROOT, now=None):
    now = now or datetime.now(ZoneInfo("America/New_York"))
    if now.astimezone(ZoneInfo("America/New_York")).date().isoformat() != "2026-10-03":
        return False
    receipt = root / "approval.json"
    if not receipt.exists() or receipt.is_symlink():
        return False
    permissions = receipt.stat()
    if permissions.st_uid != 0 or stat.S_IMODE(permissions.st_mode) != 0o600:
        return False
    record = json.loads(receipt.read_text())
    expected = json.loads((root / "release.json").read_text())
    if record != {**expected, "reviewer": "claude-1", "decision": "APPROVED"}:
        return False
    if set(expected) != {"application_sha", "plan_commit", "artifacts"}:
        return False
    if expected["application_sha"] != APPROVED_SHA or not re.fullmatch(r"[0-9a-f]{40}", expected["plan_commit"]):
        return False
    if set(expected["artifacts"]) != ARTIFACTS:
        return False
    return all(hashlib.sha256((root / name).read_bytes()).hexdigest() == sha
               for name, sha in expected["artifacts"].items())


if __name__ == "__main__":
    try:
        allowed = verify()
    except Exception as exc:
        print(f"REVIEW_GATE UNKNOWN {type(exc).__name__}: {exc}")
        sys.exit(1)
    print("REVIEW_GATE APPROVED" if allowed else "REVIEW_GATE pending, expired, or mismatched")
    sys.exit(0 if allowed else 1)
