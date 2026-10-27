"""Main-only fresh PG16 creation caller. No production config or adoption.

Default is plan-only. Explicit reviewed execution creates only the fixed private
root/db/socket. An existing root is a STOP, never deletion/restart/reuse. Failed
initialization is preserved for Main's identity-checked cleanup, never retried.
No migration, copy, service, native/provider or trade action is performed.
"""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import time

from tools.l12_private_evidence_journal import PrivateJournal
from tools.l12_main_nonce_target import MainNonceTarget, VERSION as TARGET_VERSION, layout

BINARIES = ("initdb", "pg_ctl", "postgres", "psql")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate_creation_review_key")
        result[key] = value
    return result


def close_preserving_primary(resource, primary_error, note):
    try:
        resource.close()
    except BaseException as cleanup:
        if primary_error is None:
            raise
        primary_error.add_note(note + "=" + type(cleanup).__name__)


def reviewed_plan(raw, *, expected_review_sha256):
    """No executable discovery, Settings/env loading or moving-head lookup."""
    if (type(raw) is not bytes or not 0 < len(raw) <= 32768
            or sha256(raw).hexdigest() != expected_review_sha256):
        raise ValueError("actual_reviewed_creation_plan_bytes_required")
    plan = json.loads(raw, object_pairs_hook=pairs)
    if (not isinstance(plan, dict) or set(plan) != {"version", "source_head", "source_review_sha256",
            "binary_review_sha256", "binaries", "nonce", "root", "data_directory", "socket",
            "database", "port", "username"}
            or plan["version"] != "L12_FRESH_PRIVATE55521_PG16_V1"
            or not re.fullmatch("[0-9a-f]{40}", plan["source_head"])
            or any(not isinstance(plan[key], str) or not re.fullmatch("[0-9a-f]{64}", plan[key])
                or plan[key] == "0" * 64 for key in ("source_review_sha256", "binary_review_sha256"))
            or plan["username"] != pwd.getpwuid(os.getuid()).pw_name
            or not re.fullmatch("[a-z_][a-z0-9_]{0,62}", plan["username"])
            or not isinstance(plan["binaries"], dict) or set(plan["binaries"]) != set(BINARIES)):
        raise ValueError("explicit_reviewed_private55521_plan_required")
    expected = layout(plan["nonce"])
    if any(plan[key] != value for key, value in expected.items()):
        raise ValueError("exact_Main_nonce_layout_required_no_legacy_adoption")
    for name, item in plan["binaries"].items():
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError("exact_binary_path_and_digest_required")
        path = Path(item["path"])
        if (not path.is_absolute() or not re.fullmatch("[A-Za-z0-9_./@+-]+", str(path))
                or path.name != name or path.resolve() != path
                or path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK)
                or sha256(path.read_bytes()).hexdigest() != item["sha256"]):
            raise ValueError("actual_reviewed_PG_binary_bytes_required")
    if len({str(Path(item["path"]).parent) for item in plan["binaries"].values()}) != 1:
        raise ValueError("same_reviewed_PG16_binary_directory_required")
    return plan


IDENTITY = """SELECT json_build_object(
'system_identifier', (pg_control_system()).system_identifier::text,
'postmaster_started', pg_postmaster_start_time()::text,
'database_user', current_user, 'database', current_database(),
'server_version_num', current_setting('server_version_num'),
'data_directory', current_setting('data_directory'), 'port', current_setting('port'),
'listen_addresses', current_setting('listen_addresses'), 'server_address', inet_server_addr())"""


def validate_identity(value, *, username, database, expected_layout, prior=None):
    fields = {"system_identifier", "postmaster_started", "database_user", "database", "server_version_num",
              "data_directory", "port", "listen_addresses", "server_address"}
    if (not isinstance(value, dict) or set(value) != fields
            or not isinstance(value["system_identifier"], str) or not value["system_identifier"].isdecimal()
            or not isinstance(value["postmaster_started"], str) or not value["postmaster_started"]
            or value["database_user"] != username or value["database"] != database
            or not isinstance(value["server_version_num"], str) or not value["server_version_num"].isdecimal()
            or not 160000 <= int(value["server_version_num"]) < 170000
            or value["data_directory"] != expected_layout["data_directory"]
            or value["port"] != expected_layout["port"]
            or value["listen_addresses"] != "" or value["server_address"] is not None):
        raise ValueError("actual_connected_private_PG16_identity_required")
    if prior is not None and any(value[key] != prior[key] for key in fields - {"database"}):
        raise ValueError("private_epoch_changed_during_creation")
    return value


def create(plan_raw, *, expected_review_sha256, execute=False):
    plan = reviewed_plan(plan_raw, expected_review_sha256=expected_review_sha256)
    root, database, port = Path(plan["root"]), plan["database"], plan["port"]
    if root.exists() or root.is_symlink():
        raise ValueError("existing_private_root_STOP_no_adoption_or_deletion")
    result = dict(status="PLAN_ONLY_NOT_CREATED", permission_granted=False,
        source_head=plan["source_head"], review_sha256=expected_review_sha256,
        root=str(root), database=database, port=port, restore="NOT_RUN", migration="NOT_RUN",
        service_boot="NOT_RUN", native="UNKNOWN")
    if execute is not True:
        return result
    root.mkdir(mode=0o700)  # Atomic refusal if another writer won after the check.
    (root / "socket").mkdir(mode=0o700)
    (root / "journal").mkdir(mode=0o700)
    journal = PrivateJournal(root / "journal")
    nonce, previous, ordinal = plan["nonce"], None, 0
    started = time.monotonic()
    result.update(status="CREATING_PRIVATE_CLUSTER_NOT_BOOT", nonce=nonce)

    def record(kind, value):
        nonlocal previous, ordinal
        raw = canonical(dict(nonce=nonce, source_head=plan["source_head"],
            review_sha256=expected_review_sha256, ordinal=ordinal, previous=previous, kind=kind, value=value))
        journal(raw)
        previous, ordinal = sha256(raw).hexdigest(), ordinal + 1

    env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C", "LC_ALL": "C", "TZ": "UTC",
           "PGOPTIONS": "-c statement_timeout=750ms -c lock_timeout=250ms"}

    def run(argv, *, timeout):
        if time.monotonic() - started + timeout > 180:
            raise TimeoutError("private_creation_total_budget_exhausted_no_retry")
        try:
            completed = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=timeout, check=False, env=env, close_fds=True)
        except BaseException as exc:
            try:
                record("COMMAND_OUTCOME_UNKNOWN", dict(argv=argv, failure_type=type(exc).__name__))
            except BaseException as journal_error:
                exc.add_note("creation_command_journal_failure_type=" + type(journal_error).__name__)
            raise
        record("COMMAND_RETURNED", dict(argv=argv, returncode=completed.returncode,
            stdout_bytes=len(completed.stdout), stderr_bytes=len(completed.stderr),
            stdout_sha256=sha256(completed.stdout).hexdigest(), stderr_sha256=sha256(completed.stderr).hexdigest()))
        if completed.returncode != 0 or max(len(completed.stdout), len(completed.stderr)) > 1024 * 1024:
            raise RuntimeError("private_creation_command_failed_preserve_no_retry")
        return completed.stdout

    paths = {name: item["path"] for name, item in plan["binaries"].items()}
    def sql(database, statement):
        return run([paths["psql"], "-X", "-w", "-h", plan["socket"], "-p", port,
            "-U", plan["username"], "-d", database, "-A", "-t", "-v", "ON_ERROR_STOP=1",
            "-c", statement], timeout=8)

    primary_error = None
    try:
        record("BEGIN_REVIEWED_FRESH_CREATION", result)
        run([paths["initdb"], "-D", plan["data_directory"], "--encoding=UTF8", "--locale=C",
             "--auth-local=trust", "--auth-host=reject", "-U", plan["username"]], timeout=45)
        run([paths["pg_ctl"], "-D", plan["data_directory"], "-l", str(root / "postgresql.log"),
            "-p", paths["postgres"], "-w", "-t", "30", "-o",
            "-c listen_addresses='' -k " + plan["socket"] + " -p " + port, "start"], timeout=35)
        identity = validate_identity(json.loads(sql("postgres", IDENTITY), object_pairs_hook=pairs),
                                     username=plan["username"], database="postgres", expected_layout=plan)
        record("ACTUAL_PRIVATE_CLUSTER_IDENTITY", identity)
        count = sql("postgres", "SELECT count(*) FROM pg_database WHERE datname='" + database + "'").strip()
        if count != b"0":
            raise ValueError("fresh_private_database_already_exists_STOP")
        sql("postgres", "CREATE DATABASE " + database + " TEMPLATE template0 ENCODING 'UTF8'")
        created = validate_identity(json.loads(sql(database, IDENTITY), object_pairs_hook=pairs),
            username=plan["username"], database=database, expected_layout=plan, prior=identity)
        relations = sql(database, "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' "
            "AND c.relkind IN ('r','p','v','m','S','f')").strip()
        if relations != b"0":
            raise ValueError("new_nonce_database_not_empty_STOP")
        pid_path = root / "data/postmaster.pid"
        if pid_path.is_symlink() or not pid_path.is_file() or pid_path.stat().st_size > 8192:
            raise ValueError("actual_new_nonce_postmaster_PID_required")
        pid = int(pid_path.read_text().splitlines()[0])
        record("ACTUAL_DATABASE_CREATED_AND_ZERO_USER_RELATIONS", dict(identity=created, postmaster_pid=pid))
        capability = dict(version=TARGET_VERSION, nonce=nonce, **layout(nonce), uid=os.getuid(),
            database_user=created["database_user"], system_identifier=created["system_identifier"],
            postmaster_started=created["postmaster_started"], server_version_num=created["server_version_num"],
            postmaster_pid=pid, source_head=plan["source_head"], creation_review_sha256=expected_review_sha256,
            creation_journal_tail_sha256=previous,
            at_creation_schema_state="CATALOG_VERIFIED_ZERO_USER_RELATIONS_NO_COPY_OR_BOOT")
        cap_raw = canonical(capability)
        cap_sha = sha256(cap_raw).hexdigest()
        MainNonceTarget(cap_raw, cap_sha, expected_review_sha256)
        (root / "capability").mkdir(mode=0o700)
        cap_journal = PrivateJournal(root / "capability")
        capability_error = None
        try:
            cap_journal(cap_raw)
            cap_receipt = cap_journal.last_receipt
        except BaseException as exc:
            capability_error = exc
            raise
        finally:
            close_preserving_primary(cap_journal, capability_error, "creation_capability_cleanup_failure_type")
        result.update(status="FRESH_PRIVATE_PG16_CREATED_NO_SCHEMA_COPY_OR_BOOT", identity=created,
            postmaster_pid=pid, target_capability_sha256=cap_sha,
            target_capability_path=str(root / "capability/00000000.json"),
            target_capability_posix_readback_receipt=cap_receipt,
            shutdown_owner="MAIN_ONLY_EXACT_NONCE_AND_CONNECTED_IDENTITY_REQUIRED")
        record("CREATION_COMPLETE_NOT_BOOT", result)
        result["journal_tail_sha256"] = previous
        result["journal_posix_readback_receipt"] = journal.last_receipt
        return result
    except BaseException as exc:
        primary_error = exc
        result.update(status="CREATION_FAILED_PRESERVE_ROOT_AND_POSSIBLE_OWNED_SERVER",
            failure_type=type(exc).__name__, shutdown_owner="MAIN_IDENTITY_CHECK_REQUIRED_NO_AUTOMATIC_STOP")
        try:
            record("STOP_UNKNOWN_NO_RETRY", result)
        except BaseException as cleanup:
            exc.add_note("creation_journal_failure_type=" + type(cleanup).__name__)
        raise
    finally:
        close_preserving_primary(journal, primary_error, "creation_journal_cleanup_failure_type")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewed-plan", required=True)
    parser.add_argument("--reviewed-plan-sha256", required=True)
    parser.add_argument("--execute-reviewed-private-creation", action="store_true")
    args = parser.parse_args()
    path = Path(args.reviewed_plan)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 32768:
        raise ValueError("bounded_regular_actual_creation_review_required")
    result = create(path.read_bytes(), expected_review_sha256=args.reviewed_plan_sha256,
                    execute=args.execute_reviewed_private_creation)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
