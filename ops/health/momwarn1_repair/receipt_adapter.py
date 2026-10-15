"""Validate actual service-user evidence without claiming live health or acting."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import re

from project_mai_tai.momwarn_cohort_lateness import CohortObservation, NS


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def native_health(value, *, final_observed_ns, now):
    fields = {"observed_wall_ns", "oms_sync_seq", "oms_sync_seconds", "v2_bar_lateness_seconds",
              "load_1m", "ram_available_bytes", "new_live_error", "v2_not_applicable"}
    readiness = {"v2_startup_pending", "v2_startup_deadline_ns"}
    require(isinstance(value, dict) and set(value) in (
        fields, fields | readiness, fields | readiness | {"v2_cohort"}),
            "native health shape missing or changed")
    pending = value.get("v2_startup_pending", False)
    deadline = value.get("v2_startup_deadline_ns")
    require(type(pending) is bool and (
        pending and type(deadline) is int and deadline > 0
        and value["v2_not_applicable"] is False and value["v2_bar_lateness_seconds"] is None
        or not pending and deadline is None), "native startup readiness malformed")
    stamp = value["observed_wall_ns"]
    require(type(stamp) is int and 0 < stamp <= final_observed_ns
            and 0 <= now.timestamp() - stamp / 1_000_000_000 <= 30,
            "native observation timestamp stale, absent or future")
    require(type(value["oms_sync_seq"]) is int and value["oms_sync_seq"] > 0
            and type(value["ram_available_bytes"]) is int and value["ram_available_bytes"] >= 0
            and type(value["new_live_error"]) is bool and type(value["v2_not_applicable"]) is bool,
            "native integer or boolean evidence malformed")
    for key in ("oms_sync_seconds", "load_1m"):
        number = value[key]
        require(type(number) in (int, float) and math.isfinite(number) and number >= 0,
                "native numeric evidence malformed")
    late = value["v2_bar_lateness_seconds"]
    cohort = value.get("v2_cohort")
    if cohort is not None:
        observation = CohortObservation.read(cohort)
        require(observation.source == "strategy-state-isolated"
                and observation.observed_ns == stamp
                and value["v2_not_applicable"] is False and pending is False
                and deadline is None
                and late == (observation.lateness_ns / NS if observation.lateness_ns is not None else None),
                "native CAL source, clock or readiness incoherent")
    require((late is None and (value["v2_not_applicable"] or pending or cohort is not None))
            or (type(late) in (int, float) and math.isfinite(late) and late >= 0),
            "native bar observation malformed")
    # This verifies observations, not threshold health: real errors/slow reads remain visible.
    return stamp


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
    stamps = [native_health(a["health"], final_observed_ns=observed, now=now) for a in attempts]
    require(stamps[0] < stamps[1], "two distinct native observation clocks required")
    scope = "NOT_EVALUATED_NO_GATEWAY_PUBLISH_LAG"
    require(row.get("health_verdict") == scope, "reader receipt cannot assert gateway health")
    return {
        "status": "PASS", "actual_box": True, "service_user": "trader",
        "read_live_calls": 2, "exceptions": [], "own_service_environment": True,
        "gateway_instance": expected_instance, "source_head": expected_head,
        "observed_ns": observed, "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "opened_paths": sorted(expected_paths),
        "health_verdict": scope,
        "scope": "Actual reader readability only; not a gateway health verdict or deployment authority",
    }


def read_f3(path: Path, **expectations) -> dict:
    require(not path.is_symlink(), "F3 receipt symlink refused")
    with path.open("rb") as stream:
        raw = stream.read(131073)
    return f3_receipt(raw, **expectations)
