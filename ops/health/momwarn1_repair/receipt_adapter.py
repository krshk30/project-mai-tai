"""Validate actual service-user evidence without claiming live health or acting."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def f3_receipt(raw: bytes, *, expected_head: str, expected_instance: str,
               expected_uid: int, expected_gid: int, expected_paths: set[str],
               expected_sources: dict[str, str], expected_caller_sha256: str,
               now: datetime, max_age_seconds: int = 30) -> dict:
    """Caller expectations come from main's separately verified box preflight."""
    require(len(raw) <= 131072, "F3 evidence exceeds bound")
    require(now.tzinfo is not None, "actual current clock required")
    require(type(expected_uid) is int and expected_uid > 0 and type(expected_gid) is int
            and expected_gid > 0, "nonroot actual service identity required")
    require(re.fullmatch(r"[0-9a-f]{40}", expected_head) is not None
            and re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", expected_instance) is not None,
            "source and process identity expectations required")
    row = json.loads(raw)
    require(isinstance(row, dict) and row.get("status") == "READ_LIVE_2_OK"
            and not row.get("identity_changed"), "actual two-read proof missing")
    identity = row.get("identity")
    require(isinstance(identity, dict) and identity.get("source_head") == expected_head
            and identity.get("gateway_instance") == expected_instance
            and identity.get("gateway_pid") == int(expected_instance.split(":")[0])
            and type(identity.get("uid")) is int and identity["uid"] == expected_uid
            and type(identity.get("gid")) is int and identity["gid"] == expected_gid
            and identity.get("unit") == "project-mai-tai-market-data.service"
            and identity.get("environment_source") ==
            f"/proc/{identity.get('gateway_pid')}/environ (values not exported)",
            "service user, environment, source or identity mismatch")
    attempts = row.get("attempts")
    require(isinstance(attempts, list) and len(attempts) == 2
            and [a.get("attempt") for a in attempts if isinstance(a, dict)] == [1, 2]
            and all(a.get("read_status") == "OK" and "exception_class" not in a
                    and isinstance(a.get("health"), dict) for a in attempts),
            "two actual native reader calls required")
    permissions = row.get("permissions")
    require(isinstance(permissions, list) and len(permissions) == len(expected_paths)
            and all(isinstance(p, dict) and p.get("readable") is True for p in permissions)
            and {p.get("path") for p in permissions} == expected_paths,
            "complete actual opened-path list required")
    sources = row.get("native_sources")
    require(isinstance(sources, list) and len(sources) == len(expected_sources)
            and all(isinstance(s, dict) for s in sources)
            and {s.get("path"): s.get("sha256") for s in sources} == expected_sources
            and row.get("caller_sha256") == expected_caller_sha256,
            "actual native reader or caller source drift")
    observed = row.get("observed_ns")
    require(type(observed) is int and observed > 0, "actual observation clock missing")
    age = now.astimezone(UTC).timestamp() - observed / 1_000_000_000
    require(0 <= age <= max_age_seconds, "actual trader read receipt stale or future")
    return {
        "status": "PASS", "actual_box": True, "service_user": "trader",
        "read_live_calls": 2, "exceptions": [], "own_service_environment": True,
        "gateway_instance": expected_instance, "source_head": expected_head,
        "observed_ns": observed, "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "opened_paths": sorted(expected_paths),
        "health_verdict": row.get("health_verdict", "UNKNOWN"),
        "scope": "Actual reader readability only; not a gateway health verdict or deployment authority",
    }


def read_f3(path: Path, **expectations) -> dict:
    require(not path.is_symlink(), "F3 receipt symlink refused")
    with path.open("rb") as stream:
        raw = stream.read(131073)
    return f3_receipt(raw, **expectations)
