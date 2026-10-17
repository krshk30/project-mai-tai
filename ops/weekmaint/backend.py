"""Durable Linux/PG maintenance backend. Importing this module performs no actions.

Execution requires a root-private, reviewed exact-source manifest. Unknown
outcomes retain the inventory/claim; they never trigger an automatic rerun.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import sys
import time
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text

from ops.weekmaint.archive import archive_transaction, fsync_directory, private_directory, stage_file
from ops.weekmaint.collector import private_json
from ops.weekmaint.copy_plan import inventory
from ops.weekmaint.policy import BOOT_REQUIRED, FULL_TABLES, Refused, aware, boot_report, calendar_gate, encoded, run_job, sec_gate

UNIT = re.compile(r"project-mai-tai-[A-Za-z0-9@_.:-]+\.(?:service|timer)")
HEX = re.compile(r"[0-9a-f]{64}")
TIMER_BODIES = frozenset({
    "project-mai-tai-option-a-daily-guard.service", "project-mai-tai-momentum-paper.service",
    "project-mai-tai-gather-universe.service", "project-mai-tai-momlive-sec.service",
    "project-mai-tai-prune-capture.service", "project-mai-tai-prune-ticks.service",
    "project-mai-tai-prune-retention-weekly.service",
})


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def current_boot_id():
    return str(UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))


def boot_wall_time():
    lines = [line.split() for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime ")]
    if len(lines) != 1 or len(lines[0]) != 2 or not lines[0][1].isdigit():
        raise Refused("OS_boot_wall_time_UNKNOWN")
    return datetime.fromtimestamp(int(lines[0][1]), UTC)


def timer_wall(value):
    match = re.fullmatch(r"[A-Z][a-z]{2} (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (UTC|EST|EDT)", value)
    if match is None:
        raise Refused("timer_next_wall_clock_UNKNOWN")
    zone = UTC if match[2] == "UTC" else ZoneInfo("America/New_York")
    parsed = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=zone)
    if parsed.strftime("%Z") != match[2]:
        raise Refused("timer_zone_or_DST_UNKNOWN")
    return parsed.astimezone(UTC)


class SystemBackend:
    def __init__(self, config, engine, collector, *, run=None, clock=None, popen=None, preview=False):
        self.config, self.engine, self.collector = config, engine, collector
        self.run = run or subprocess.run
        self.popen = popen or subprocess.Popen
        self.clock = clock or (lambda: datetime.now(UTC))
        self.preview = preview
        self.state = Path(config["state_directory"])
        info = self.state.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise Refused("existing_private_installer_state_required_no_creation")
        self.approved_head = config["approved_head"]
        self.expected_lookup_day = config["lookup_day"]
        self.live_contract_approved = config.get("reviewed_bot_DAY_collector") is True
        self.security_window_approved = config.get("reviewed_scoped_security_window") is True
        self._locks = ExitStack()
        self._lock_held = False
        self.operation = None
        self._inventory = None
        self._broker_attempted = False
        self._broker_evidence = None
        self._ownership_signature = None
        self._performance_diagnostics = {}

    def command(self, args, *, timeout=30, env=None):
        if not isinstance(args, list) or any(not isinstance(value, str) for value in args):
            raise Refused("literal_argv_required")
        result = self.run(args, text=True, capture_output=True, timeout=timeout, env=env)
        if result.returncode or len(result.stdout.encode()) > 2_000_000:
            raise Refused(f"command_failed_or_unbounded:{Path(args[0]).name}")
        return result.stdout

    def validate_source(self):
        repo = Path(self.config["repository"]).resolve(strict=True)
        head = self.command(["/usr/bin/git", "-C", str(repo), "rev-parse", "HEAD"]).strip()
        dirty = self.command(["/usr/bin/git", "-C", str(repo), "status", "--porcelain=v1"])
        if head != self.approved_head or dirty:
            raise Refused("exact_clean_approved_source_required")
        sources = self.config.get("source_sha256")
        mandatory = {"ops/weekmaint/backend.py", "ops/weekmaint/policy.py", "ops/weekmaint/collector.py",
                     "ops/weekmaint/archive.py", "ops/weekmaint/copy_plan.py", "ops/weekmaint/security-only.conf",
                     "ops/bootstrap/maintenance-window/project-mai-tai-maintenance"}
        if not isinstance(sources, dict) or not mandatory.issubset(sources):
            raise Refused("source_hash_manifest_required")
        for name, expected in sources.items():
            path = (repo / name).resolve(strict=True)
            if repo not in path.parents or not HEX.fullmatch(expected) or digest(path) != expected:
                raise Refused("approved_source_hash_mismatch")

    def acquire_exclusion(self):
        paths = self.config.get("installer_lock_paths")
        if not isinstance(paths, list) or not paths or self.config.get("reviewed_all_installer_locks") is not True:
            raise Refused("installer_exclusion_inventory_unreviewed")
        try:
            for path in [str(self.state / "execution.lock"), *paths]:
                flags = os.O_RDONLY if self.preview else os.O_RDWR
                descriptor = os.open(path, flags | os.O_NOFOLLOW, 0o600)
                self._locks.callback(os.close, descriptor)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_held = True
        except BaseException:
            self._locks.close()
            raise

    def close(self):
        self._locks.close()
        self._lock_held = False

    def unit_properties(self, unit):
        if not UNIT.fullmatch(unit):
            raise Refused("unit_not_exact_project_unit")
        raw = self.command(["/bin/systemctl", "show", unit, "--no-pager",
            "--property=Id,LoadState,ActiveState,SubState,FragmentPath,DropInPaths,InvocationID,ExecMainStartTimestamp,NextElapseUSecRealtime,TriggeredBy"])
        properties = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        if properties.get("Id") != unit or properties.get("LoadState") != "loaded":
            raise Refused("systemd_unit_unknown")
        if properties.get("ActiveState") not in {"active", "inactive"} and not self.is_current_nonwriter_oneshot(unit, properties):
            raise Refused("systemd_unit_transitional_or_failed")
        return properties

    def is_current_nonwriter_oneshot(self, unit, properties):
        if (unit not in {"project-mai-tai-weekmaint.service", "project-mai-tai-weekmaint-boot.service"}
                or properties.get("ActiveState") != "activating" or properties.get("SubState") != "start"
                or self.config.get("units", {}).get(unit, {}).get("writer") is not False):
            return False
        invocation = os.environ.get("INVOCATION_ID", "")
        if not re.fullmatch(r"[0-9a-f]{32}", invocation) or properties.get("InvocationID") != invocation:
            return False
        try:
            return any(line.endswith(f"/{unit}") for line in Path("/proc/self/cgroup").read_text().splitlines())
        except OSError:
            return False

    def read_writer_inventory(self):
        configured = self.config.get("units")
        if not isinstance(configured, dict) or not configured:
            raise Refused("reviewed_exact_unit_inventory_required")
        raw = self.command(["/bin/systemctl", "list-unit-files", "project-mai-tai-*",
                            "--no-legend", "--no-pager", "--plain"])
        installed = {line.split()[0] for line in raw.splitlines() if line.strip()}
        if installed != set(configured):
            raise Refused("installed_unit_inventory_changed")
        result = {}
        for unit, expected in configured.items():
            properties = self.unit_properties(unit)
            fragment = properties.get("FragmentPath", "")
            if (not fragment.startswith(("/etc/systemd/system/", "/lib/systemd/system/", "/usr/lib/systemd/system/"))
                    or digest(fragment) != expected.get("fragment_sha256")
                    or type(expected.get("writer")) is not bool):
                raise Refused("unit_source_or_writer_role_unknown")
            dropins = expected.get("dropin_sha256", {})
            actual_dropins = properties.get("DropInPaths", "").split()
            if (set(actual_dropins) != set(dropins)
                    or any(digest(path) != dropins[path] for path in actual_dropins)):
                raise Refused("unit_dropin_inventory_changed")
            result[unit] = {**properties, "writer": expected["writer"]}
        # Cron, drop-ins and external installers must be explicitly inventoried;
        # list-unit-files alone cannot prove absence of another writer.
        manifests = self.config.get("writer_manifest_files")
        if not isinstance(manifests, dict) or not manifests:
            raise Refused("cron_and_dropin_inventory_unknown")
        for path, expected in manifests.items():
            if digest(path) != expected:
                raise Refused("writer_or_cron_manifest_changed")
        if self.config.get("reviewed_all_writer_surfaces") is not True:
            raise Refused("writer_coverage_unreviewed")
        directories = self.config.get("writer_directory_inventory")
        if not isinstance(directories, dict) or not directories:
            raise Refused("writer_directory_inventory_missing")
        for name, expected in directories.items():
            directory = Path(name)
            if directory.is_symlink() or not directory.is_dir():
                raise Refused("writer_directory_unknown")
            actual = {path.name: digest(path) for path in directory.iterdir() if path.is_file()}
            if actual != expected:
                raise Refused("writer_directory_contents_changed")
        return result

    def database_inventory(self):
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL statement_timeout='5s'"))
                connection.execute(text("SET LOCAL lock_timeout='1s'"))
                rows, facts = inventory(connection, aware(self.clock()))
        return rows, facts

    def read_snapshot(self):
        rows, facts = self.database_inventory()
        units = self.read_writer_inventory()
        signature = hashlib.sha256(encoded({"accounts": facts["live_accounts"], "ownership": sorted(
            (item["table"], str(item["row"]["id"]), item["account"], item["row"]["symbol"],
             str(item["row"].get("quantity") if item["table"] == "virtual_positions"
                 else item["row"].get("current_quantity")))
            for item in rows if item["table"] in {"virtual_positions", "oms_managed_positions"})})).hexdigest()
        if not self._broker_attempted:
            self._broker_attempted = True
            broker = asyncio.run(self.collector.collect(rows, facts))
            self._broker_evidence = deepcopy(broker)
            self._ownership_signature = signature
        elif self._broker_evidence is None:
            raise Refused("start_positions_read_failed_no_retry")
        elif signature != self._ownership_signature:
            raise Refused("ownership_changed_after_single_start_read_no_refresh")
        broker = deepcopy(self._broker_evidence)
        local = aware(self.clock()).astimezone(ZoneInfo("America/New_York"))
        outside = local.weekday() == 5 or (local.hour < 3 or local.hour >= 19)
        return {**facts, "broker_evidence": broker, "writer_units": units,
                "guard_paper_outside_session": outside,
                "install_exclusion_held": self._lock_held, "writer_inventory_complete": True}

    def read_retirement_rows(self):
        return self.database_inventory()[0]

    def read_sec_receipt(self):
        return private_json(self.config["sec_receipt_path"])

    def validate_runtime_inputs(self):
        self.timer_started_body("project-mai-tai-oms.service", {})
        # Read-only preflight before the first claim/stop; do not discover an
        # absent package policy only after taking all writers down.
        if (not self.live_contract_approved or not self.security_window_approved
                or self.state != Path("/var/lib/project-mai-tai-weekmaint")):
            raise Refused("collector_or_security_manifest_unreviewed")
        with self.engine.connect() as connection:
            connection.execute(text("SET statement_timeout='3s'"))
            database, address, data = connection.execute(text("SELECT current_database(),inet_server_addr()::text,"
                "current_setting('data_directory')")).one()
            system_id = str(connection.execute(text("SELECT system_identifier FROM pg_control_system()")).scalar_one())
            self.observe_jit_setting(connection)
        if (database != self.config.get("expected_database") or address not in {None, "127.0.0.1", "::1"}
                or system_id != self.config.get("expected_postgres_system_identifier")
                or Path(data).resolve(strict=True) != Path(self.config["expected_PGDATA"]).resolve(strict=True)):
            raise Refused("local_PG_identity_or_filesystem_binding_unknown")
        for path_key, hash_key in (("security_window_helper", "security_window_helper_sha256"),
                                   ("security_apt_config", "security_apt_config_sha256")):
            if digest(self.config[path_key]) != self.config.get(hash_key):
                raise Refused("security_preflight_source_changed")
        blacklists = Path("/var/lib/project-mai-tai-weekmaint/preserved-blacklists.conf")
        if (self.config.get("reviewed_all_existing_operator_blacklists") is not True
                or digest(blacklists) != self.config.get("preserved_blacklists_sha256")):
            raise Refused("blacklist_preflight_unknown")
        invocation = os.environ.get("INVOCATION_ID", "")
        if (not re.fullmatch(r"[0-9a-f]{32}", invocation)
                or not any(line.endswith("/project-mai-tai-weekmaint.service")
                           for line in Path("/proc/self/cgroup").read_text().splitlines())):
            raise Refused("reviewed_maintenance_service_invocation_required")

    def observe_jit_setting(self, connection):
        value, failure = "UNKNOWN", None
        try:
            with connection.begin_nested():
                observed = connection.execute(text("SELECT current_setting('jit')")).scalar_one()
            if observed in {"on", "off"}:
                value = observed
        except Exception as exc:
            failure = type(exc).__name__
        critical = value != "off"
        self._performance_diagnostics = {"jit": {
            "effective": value, "failure_type": failure, "severity": "CRITICAL" if critical else "INFO",
            "source": "existing_preclaim_validation_connection",
            "report_line": f"{'CRITICAL' if critical else 'INFO'} [WEEKMAINT-JIT] "
                           f"effective={value} required=off action=REPORT_ONLY_CONTINUE",
        }}

    def operation_path(self, operation):
        return self.state / f"operation-{UUID(str(operation))}.json"

    def save(self, path, value):
        temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
        stage_file(temporary, encoded(value))
        os.replace(temporary, path)
        fsync_directory(path.parent)

    def claim_once_per_SEC_success(self, token):
        if self.preview or not self._lock_held or not HEX.fullmatch(token):
            raise Refused("exclusive_SEC_claim_required")
        active = self.state / "active-operation.json"
        try:
            (self.state / "reboot.json").lstat()
        except FileNotFoundError:
            pass
        else:
            raise Refused("unresolved_reboot_marker_requires_review")
        try:
            active.lstat()
        except FileNotFoundError:
            pass
        else:
            raise Refused("unresolved_operation_requires_review_even_new_SEC_token")
        prior = self.state / f"sec-{token}.claimed"
        try:
            prior.lstat()
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("SEC_success_already_claimed_no_retry")
        operation = str(uuid4())
        stage_file(active, encoded({"version": 1, "operation": operation, "SEC_token": token,
            "head": self.approved_head, "status": "CLAIMED_UNRESOLVED", "at": self.clock()}))
        # Exclusive, never overwritten, and before any stop. Even a crash before
        # the operation record is written requires manual review, not a rerun.
        stage_file(prior, encoded({"operation": operation, "at": self.clock()}))
        self.operation = operation
        return operation

    def persist_inventory_before_stop(self, operation, snapshot):
        self._inventory = {"version": 1, "operation": operation, "head": self.approved_head,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "started_at": self.clock().isoformat(), "units": snapshot["writer_units"], "steps": [],
            "performance_diagnostics": self.__dict__.get("_performance_diagnostics", {})}
        self.save(self.operation_path(operation), self._inventory)

    def record(self, operation, step, data):
        state = private_json(self.operation_path(operation))
        state["steps"].append({"at": self.clock().isoformat(), "step": step, "data": data})
        self.save(self.operation_path(operation), state)

    def stop_exact_recorded_writers(self, operation):
        record = private_json(self.operation_path(operation))
        selected = [name for name, value in record["units"].items()
                    if value["writer"] and value["ActiveState"] == "active"]
        selected.sort(key=lambda value: (not value.endswith(".timer"), value))
        self.record(operation, "STOP_INTENT", selected)
        for unit in selected:
            self.command(["/bin/systemctl", "stop", unit], timeout=120)
        for unit, original in record["units"].items():
            if original["writer"] and self.unit_properties(unit)["ActiveState"] != "inactive":
                raise Refused("writer_still_active_after_stop")
        stopped = aware(self.clock())
        self.record(operation, "WRITERS_STOPPED", {"at": stopped.isoformat(), "units": selected})
        return stopped

    def record_skipped_rows(self, operation, rows):
        import csv
        import io
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=["table", "id", "reason"])
        writer.writeheader()
        writer.writerows(rows)
        stage_file(self.state / f"{operation}-left-unchanged.csv", stream.getvalue().encode())

    def archive_transaction_with_csv(self, operation, plan):
        outcome = archive_transaction(self.engine, operation, plan, self.state / "audits")
        self.record(operation, "ARCHIVE_COMMITTED", outcome)

    def vacuum_analyze_all_outside_transaction(self):
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text("SET lock_timeout='2s'"))
            connection.execute(text("SET statement_timeout='1h'"))
            connection.execute(text("VACUUM (ANALYZE)"))

    def measure_table_and_own_filesystem(self, table):
        if table not in FULL_TABLES:
            raise Refused("unapproved_rewrite_table")
        with self.engine.connect() as connection:
            size, location = connection.execute(text("""SELECT pg_total_relation_size(c.oid),
                COALESCE(NULLIF(pg_tablespace_location(COALESCE(NULLIF(c.reltablespace,0),d.dattablespace)),''),
                    current_setting('data_directory'))
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                JOIN pg_database d ON d.datname=current_database()
                WHERE n.nspname='public' AND c.relname=:table AND c.relkind='r'"""), {"table": table}).one()
        if not location or not Path(location).is_dir():
            raise Refused("actual_PG_filesystem_unavailable")
        return size, shutil.disk_usage(location).free, table in self.config.get("reviewed_bloated_tables", [])

    def vacuum_full_outside_transaction(self, table):
        if table not in FULL_TABLES:
            raise Refused("unapproved_rewrite_table")
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text("SET lock_timeout='2s'"))
            connection.execute(text("SET statement_timeout='1h'"))
            connection.execute(text(f'VACUUM (FULL, ANALYZE) public."{table}"'))

    def record_vacuum_decision(self, operation, decision):
        self.record(operation, "VACUUM_DECISION", decision)

    def vacuum_journal_30_days(self):
        self.command(["/usr/bin/journalctl", "--vacuum-time=30d"], timeout=120)

    @staticmethod
    def wait_known_child(process):
        """Wait/read the SAME known child; never kill it or submit another action."""
        first_error = None
        while True:
            if type(process.returncode) is int:
                return process.returncode, first_error
            try:
                result = process.wait()
                if type(result) is int:
                    return result, first_error
            except BaseException as error:
                if first_error is None:
                    first_error = error
            try:
                result = process.poll()
                if type(result) is int:
                    return result, first_error
            except BaseException as error:
                if first_error is None:
                    first_error = error
            try:
                time.sleep(0.1)
            except BaseException as error:
                if first_error is None:
                    first_error = error

    def security_process(self, operation, output, env):
        process = None
        first_error = None
        overrun = False

        def note(action):
            nonlocal first_error
            try:
                action()
            except BaseException as error:
                if first_error is None:
                    first_error = error

        try:
            process = self.popen(["/usr/bin/unattended-upgrade"], stdout=output,
                                 stderr=subprocess.STDOUT, env=env)
            note(lambda: self.record(operation, "SECURITY_PROCESS_STARTED", {"pid": process.pid}))
            try:
                process.wait(timeout=7200)
            except subprocess.TimeoutExpired:
                overrun = True
                note(lambda: self.record(operation, "PACKAGE_OVERRUN_NOT_KILLED", {"pid": process.pid}))
                note(lambda: self.command(["/usr/bin/logger", "-p", "daemon.crit", "-t", "project-mai-tai-weekmaint",
                    "WEEKMAINT1 package deadline exceeded; do not kill dpkg; operator review required"]))
            except BaseException as error:
                if first_error is None:
                    first_error = error
        finally:
            # This guard exists before spawn. Record/logger/signal errors after
            # a known handle cannot return the unit while that child is alive.
            if process is not None:
                result, wait_error = self.wait_known_child(process)
                if first_error is None:
                    first_error = wait_error
        if first_error is not None:
            raise first_error
        if result != 0 or overrun:
            raise Refused("security_result_failed_or_overrun_review_required")

    def security_updates_only_preserving_existing_policy(self):
        # No attended override or silent hook bypass. The scoped package-window
        # integration must be separately reviewed before this backend can apply.
        if not self.security_window_approved:
            raise Refused("post_SEC_security_window_not_reviewed")
        helper = Path(self.config["security_window_helper"])
        if digest(helper) != self.config.get("security_window_helper_sha256"):
            raise Refused("security_window_helper_changed")
        # Origins and blacklist come from an independently reviewed apt config;
        # no dist-upgrade and no generic apt upgrade fallback are present.
        apt_config = self.config["security_apt_config"]
        if digest(apt_config) != self.config.get("security_apt_config_sha256"):
            raise Refused("security_only_apt_config_changed")
        blacklists = Path("/var/lib/project-mai-tai-weekmaint/preserved-blacklists.conf")
        if (not HEX.fullmatch(str(self.config.get("preserved_blacklists_sha256", "")))
                or digest(blacklists) != self.config["preserved_blacklists_sha256"]
                or self.config.get("reviewed_all_existing_operator_blacklists") is not True):
            raise Refused("existing_operator_blacklist_preservation_unreviewed")
        parts = private_directory(self.state / "empty-apt-parts")
        if any(parts.iterdir()):
            raise Refused("unexpected_extra_APT_configuration")
        env = {**os.environ, "APT_CONFIG": apt_config, "NEEDRESTART_MODE": "l"}
        env.pop("MAI_TAI_MAINTENANCE_ATTENDED", None)
        invocation = env.get("INVOCATION_ID", "")
        if (not re.fullmatch(r"[0-9a-f]{32}", invocation)
                or not any(line.endswith("/project-mai-tai-weekmaint.service")
                           for line in Path("/proc/self/cgroup").read_text().splitlines())
                or self.state != Path("/var/lib/project-mai-tai-weekmaint")):
            raise Refused("scoped_security_systemd_invocation_required")
        started = aware(self.clock())
        expires = started + timedelta(seconds=7200)
        if started.astimezone(ZoneInfo("America/New_York")).date() != expires.astimezone(ZoneInfo("America/New_York")).date():
            raise Refused("security_window_must_finish_Saturday")
        token = sec_gate(self.read_sec_receipt(), started, self.approved_head,
                         expected_lookup_day=self.expected_lookup_day)
        permit = self.state / "package-window.json"
        stage_file(permit, encoded({"version": 1, "invocation_id": invocation,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "started_at": started.isoformat(), "expires_at": expires.isoformat(), "SEC_success_sha256": token}))
        try:
            self.command([str(helper), "install"], timeout=10, env=env)
            output_path = self.state / f"packages-{UUID(str(self.operation))}.log"
            descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                self.security_process(self.operation, output, env)
                output.flush()
                os.fsync(output.fileno())
        finally:
            permit.unlink()
            fsync_directory(self.state)

    def reboot_required(self):
        return Path("/run/reboot-required").exists()

    def persist_reboot_marker(self, operation):
        self.save(self.state / "reboot.json", {"operation": operation,
            "source_boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "head": self.approved_head, "requested_at": self.clock().isoformat()})

    def reboot_once(self, operation):
        self.record(operation, "REBOOT_REQUESTED_NO_RETRY", {})
        self.command(["/bin/systemctl", "reboot"], timeout=30)

    def restore_recorded_active_inventory(self, operation):
        record = private_json(self.operation_path(operation))
        if record.get("boot_id") != current_boot_id():
            raise Refused("no_restore_actions_after_reboot_verification_only")
        original = record["units"]
        requested = aware(self.clock())
        self.record(operation, "RESTORE_INTENT", {"at": requested.isoformat(), "boot_id": current_boot_id()})
        for unit, value in sorted(original.items(), key=lambda pair: pair[0].endswith(".timer")):
            if value["writer"] and value["ActiveState"] == "active" and not self.timer_started_body(unit, value):
                self.command(["/bin/systemctl", "start", unit], timeout=120)
        oms = self.unit_properties("project-mai-tai-oms.service")
        if not re.fullmatch(r"[0-9a-f]{32}", oms.get("InvocationID", "")):
            raise Refused("restored_OMS_invocation_UNKNOWN")
        self.record(operation, "RESTORE_BARRIER", {"at": requested.isoformat(), "boot_id": current_boot_id(),
            "oms_invocation_id": oms["InvocationID"], "source": "recorded_restore_request"})
        # Inactive oneshots/weekend services stay inactive. Never start a blanket
        # wildcard and never modify enablement, timer clocks or drop-ins.

    def timer_started_body(self, unit, properties):
        # The reviewed list covers externally scheduled bodies (e.g. preopen);
        # TriggeredBy additionally prevents starting any installed timer body.
        configured = self.config.get("timer_started_services")
        if (self.config.get("reviewed_timer_started_services") is not True
                or not isinstance(configured, list)
                or any(not isinstance(name, str) or not UNIT.fullmatch(name) or not name.endswith(".service")
                       or name not in self.config["units"] for name in configured)):
            raise Refused("reviewed_timer_body_inventory_required")
        return (unit in TIMER_BODIES or unit in configured
                or any(name.endswith(".timer") for name in properties.get("TriggeredBy", "").split()))

    def recorded_unit_states_match(self, original, units):
        for name, prior in original["units"].items():
            current = units[name]
            if self.is_current_nonwriter_oneshot(name, current):
                continue
            if (name == "project-mai-tai-weekmaint.service" and prior.get("writer") is False
                    and prior.get("ActiveState") == "activating" and prior.get("SubState") == "start"
                    and current["ActiveState"] == "inactive"):
                # The invoking nonwriter oneshot finished; it is not a daemon
                # that systemd should start again after reboot.
                continue
            if self.timer_started_body(name, prior):
                # Bodies are never restored; only their reviewed timers below
                # certify the next scheduled execution.
                if current["ActiveState"] != "inactive":
                    return False
            elif current["ActiveState"] != prior["ActiveState"]:
                return False
        return True

    def claim_single_report(self, operation):
        try:
            stage_file(self.state / f"report-{UUID(str(operation))}.claimed", encoded({"at": self.clock()}))
            return True
        except FileExistsError:
            return False

    def read_boot_health_and_monday_schedule(self, operation):
        original = private_json(self.operation_path(operation))
        units = {name: self.unit_properties(name) for name in original["units"]}
        restored = self.recorded_unit_states_match(original, units)
        with self.engine.connect() as connection:
            connection.execute(text("SET statement_timeout='3s'"))
            pg_ok = connection.execute(text("SELECT 1")).scalar_one() == 1
        redis_ok = self.command(["/usr/bin/redis-cli", "-h", "127.0.0.1", "-p", "6379", "PING"], timeout=5).strip() == "PONG"
        health = self.observe_current_oms_health(operation, original, units.get("project-mai-tai-oms.service", {}))
        # A state change during the observation cannot inherit the earlier census.
        restored = restored and self.recorded_unit_states_match(
            original, {name: self.unit_properties(name) for name in original["units"]})
        checks = {"recorded_services_restored": restored, **health,
                  "performance_diagnostics": original.get("performance_diagnostics", {}),
                  "redis_read_ok": redis_ok, "pg_read_ok": pg_ok}
        schedules = self.config.get("reviewed_monday_timers", {})
        for key in ("guard_0340_scheduled", "preopen_scheduled", "universe_scheduled", "SEC_scheduled", "prune_scheduled"):
            item = schedules.get(key, {})
            unit = item.get("unit", "")
            current = self.unit_properties(unit)
            next_wall = timer_wall(current.get("NextElapseUSecRealtime", ""))
            approved_next = datetime.fromisoformat(item["approved_next_wall"])
            checks[key] = (current["ActiveState"] == "active" and next_wall > aware(self.clock())
                           and next_wall == aware(approved_next)
                           and digest(current["FragmentPath"]) == item.get("fragment_sha256"))
        retain = self.config.get("RETAIN1_files", {})
        checks["RETAIN1_unchanged"] = bool(retain) and all(digest(path) == expected for path, expected in retain.items())
        return checks

    def completed_sync_current_invocation(self, operation, original, oms):
        return self.observe_current_oms_health(operation, original, oms)["oms_completed_sync_observed"]

    def journal_observation(self, argv, seconds):
        """One bounded journal reader, never a service retry or restart loop."""
        process = None
        try:
            process = self.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if process.stdout is None:
                raise Refused("journal_stdout_UNKNOWN")
            os.set_blocking(process.stdout.fileno(), False)
            end = time.monotonic() + seconds
            buffer, total = b"", 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while time.monotonic() < end:
                    ready = selector.select(max(0, end - time.monotonic()))
                    if not ready:
                        break
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        if buffer or process.wait(timeout=2) != 0:
                            raise Refused("journal_incomplete_or_failed")
                        break
                    total += len(chunk)
                    if total > 8_000_000:
                        raise Refused("journal_observation_budget_exceeded")
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if len(line) > 131072:
                            raise Refused("journal_line_unbounded")
                        yield line.decode("utf-8", errors="strict")
                    if len(buffer) > 131072:
                        raise Refused("journal_line_unbounded")
        finally:
            # Only this private readonly journal follower is stopped. Package
            # subprocesses use the separate never-kill lifetime guard.
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                if process.stdout is not None:
                    process.stdout.close()

    def observe_current_oms_health(self, operation, original, oms):
        failed = {"oms_completed_sync_observed": False, "oms_first_commits_observed": False}
        boot = current_boot_id()
        invocation = oms.get("InvocationID", "")
        if oms.get("ActiveState") != "active" or not re.fullmatch(r"[0-9a-f]{32}", invocation):
            return failed
        new_boot = original.get("boot_id") != boot
        barriers = [step["data"] for step in original.get("steps", [])
                    if step.get("step") in {"RESTORE_BARRIER", "BOOT_SYNC_BARRIER"}]
        if barriers:
            barrier = barriers[-1]
            if barrier.get("boot_id") != boot or barrier.get("oms_invocation_id") != invocation:
                return failed
            since = aware(datetime.fromisoformat(barrier["at"]))
        else:
            if not new_boot:
                return failed
            since = timer_wall(oms.get("ExecMainStartTimestamp", ""))
            if not aware(datetime.fromisoformat(original["started_at"])) < since <= aware(self.clock()):
                return failed
            self.record(operation, "BOOT_SYNC_BARRIER", {"at": since.isoformat(), "boot_id": boot,
                "oms_invocation_id": invocation, "source": "current_OMS_process_start_observed_no_start_action"})
        if since > aware(self.clock()):
            return failed
        boot_hex = UUID(boot).hex
        now = aware(self.clock())
        until = now
        remaining = 0
        if new_boot:
            boot_at = boot_wall_time()
            if not aware(datetime.fromisoformat(original["started_at"])) < boot_at <= since <= now:
                return failed
            until = boot_at + timedelta(minutes=5)
            # Both clocks bound the observation; a wall-clock step cannot
            # turn it into five minutes from handler start or an endless wait.
            remaining = max(0, min((until - now).total_seconds(),
                                   300 - time.clock_gettime(time.CLOCK_BOOTTIME)))
        if since > until:
            return failed
        accounts = self.config.get("live_accounts", {})
        if not isinstance(accounts, dict) or len(accounts) != 2:
            raise Refused("exact_both_live_accounts_required")
        argv = ["/usr/bin/journalctl", "--boot", boot_hex, "-u", "project-mai-tai-oms.service",
                "--since", since.isoformat(), "--no-pager", "-o", "json"]
        if remaining > 0:
            # --follow's default tail must not hide the earlier first commits.
            argv += ["--follow", "--no-tail"]
            lines = self.journal_observation(argv, remaining)
        else:
            argv += ["--until", until.isoformat(), "-n", "2000"]
            lines = iter(self.command(argv, timeout=10).splitlines())
        good, receipts, runtime_boot = False, set(), None
        try:
            for line in lines:
                event = self.oms_journal_event(line, boot_hex, invocation, since, until)
                if event is None:
                    continue
                message, event_at = event
                if re.search(r"\[OMS-BROKER-SYNC-PASS\] id=\d+ phase=end outcome=ok(?:\s|$)", message):
                    good = True
                match = re.search(r"\[RECEIPT-FIRST-COMMIT\] account=(\S+) boot_id=(\S+) elapsed_ms=(\d+(?:\.\d+)?) "
                                  r"wire_read_at=(\S+) committed_at=(\S+)(?:\s|$)", message)
                if match and match[1] in accounts:
                    stamp_boot = str(UUID(match[2]))
                    wire = aware(datetime.fromisoformat(match[4]))
                    committed = aware(datetime.fromisoformat(match[5]))
                    if not since <= wire <= committed <= event_at:
                        continue
                    if runtime_boot is not None and runtime_boot != stamp_boot:
                        return failed
                    runtime_boot = stamp_boot
                    receipts.add(match[1])
                if good and receipts == set(accounts):
                    break
        finally:
            if hasattr(lines, "close"):
                lines.close()
        current = self.unit_properties("project-mai-tai-oms.service")
        if (current_boot_id() != boot or current.get("ActiveState") != "active"
                or current.get("InvocationID") != invocation):
            return failed
        return {"oms_completed_sync_observed": good, "oms_first_commits_observed": receipts == set(accounts)}

    def oms_journal_event(self, line, boot_hex, invocation, since, until):
        try:
            event = json.loads(line)
            micros = event.get("__REALTIME_TIMESTAMP", "")
            if not isinstance(micros, str) or not re.fullmatch(r"\d{1,20}", micros):
                return None
            seconds, remainder = divmod(int(micros), 1_000_000)
            event_at = datetime.fromtimestamp(seconds, UTC) + timedelta(microseconds=remainder)
            if (event.get("_BOOT_ID") == boot_hex and event.get("_SYSTEMD_INVOCATION_ID") == invocation
                    and since <= event_at <= min(until, aware(self.clock()))):
                return str(event.get("MESSAGE", "")), event_at
        except (ValueError, TypeError, AttributeError, OverflowError, OSError):
            pass
        return None

    def write_boot_report_and_unique_health_line(self, operation, evidence, good):
        diagnostics = evidence.get("performance_diagnostics", {})
        if not isinstance(diagnostics, dict):
            diagnostics = {"jit": {"report_line": "CRITICAL [WEEKMAINT-JIT] effective=UNKNOWN "
                                                   "required=off action=REPORT_ONLY_CONTINUE"}}
        lines = [item["report_line"] for item in diagnostics.values()
                 if isinstance(item, dict) and isinstance(item.get("report_line"), str)]
        self.save(self.state / f"report-{operation}.json", {"operation": operation, "at": self.clock(),
            "healthy": good, "evidence": evidence, "report_lines": lines})
        descriptor = os.open("/var/log/project-mai-tai/weekmaint-health.log",
                             os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "ab") as output:
            output.write(encoded({"at": self.clock(), "operation": operation, "healthy": good}) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        self.command(["/usr/bin/logger", "-p", "daemon.info" if good else "daemon.crit", "-t", "project-mai-tai-weekmaint",
                      f"WEEKMAINT1 operation={operation} result={'HEALTHY' if good else 'FAILED_NO_RETRY'}"])

    def clear_marker(self, operation):
        report = private_json(self.state / f"report-{operation}.json")
        active_path = self.state / "active-operation.json"
        active = private_json(active_path)
        if (self.config.get("approved_WEEKMAINT1_manifest") is not True
                or report.get("operation") != operation or report.get("healthy") is not True
                or not all(report.get("evidence", {}).get(key) is True for key in BOOT_REQUIRED)
                or active.get("operation") != operation or active.get("head") != self.approved_head):
            raise Refused("only_actual_HEALTHY_reviewed_transition_can_release_fence")
        marker_path = self.state / "reboot.json"
        try:
            marker_path.lstat()
        except FileNotFoundError:
            pass
        else:
            marker = private_json(marker_path)
            if marker.get("operation") != operation:
                raise Refused("reboot_marker_operation_mismatch")
            marker_path.unlink()
        self.record(operation, "HEALTHY_TERMINAL_FENCE_RELEASE", {"report_sha256": digest(self.state / f"report-{operation}.json")})
        active_path.unlink()
        fsync_directory(self.state)

    def verify_and_report_once(self, operation):
        # Same single-report claim without claiming that a reboot happened.
        return boot_report(self, operation)

    def report_failure_keep_inventory(self, operation):
        # Never lose the original failure because a diagnostic sink also failed.
        # The fsynced unresolved global fence remains even if every update fails.
        try:
            self.record(operation, "FAILED_UNKNOWN_REVIEW_REQUIRED_NO_RETRY", {})
        except BaseException:
            pass
        try:
            active_path = self.state / "active-operation.json"
            active = private_json(active_path)
            if active.get("operation") == operation:
                self.save(active_path, {**active, "status": "FAILED_UNKNOWN", "failed_at": self.clock()})
        except BaseException:
            pass
        try:
            self.command(["/usr/bin/logger", "-p", "daemon.crit", "-t", "project-mai-tai-weekmaint",
                          f"WEEKMAINT1 FAILED operation={operation}; inventory retained; no retry"])
        except BaseException:
            pass


def build_backend(config, *, preview=False):
    """Use the existing adapter factory without constructing/starting an OMS actor."""
    from project_mai_tai.db.session import build_engine, build_session_factory
    from project_mai_tai.oms.service import OmsRiskService
    from project_mai_tai.settings import Settings
    from ops.weekmaint.collector import ExistingReadCollector

    settings = Settings()
    if settings.oms_adapter == "simulated":
        raise Refused("maintenance_never_certifies_live_flat_from_simulated_adapter")
    engine = build_engine(settings.database_url, connect_timeout_s=3, statement_timeout_ms=5000,
                          lock_timeout_ms=1000, pool_timeout_s=3)
    shell = object.__new__(OmsRiskService)
    shell.settings = settings
    shell.session_factory = build_session_factory(settings)
    collector = ExistingReadCollector(shell._build_broker_adapter(), config["live_accounts"])
    return SystemBackend(config, engine, collector, preview=preview)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--boot-verify", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default="/etc/project-mai-tai/weekmaint.json")
    parser.add_argument("--force-test", action="store_true")
    args = parser.parse_args(argv)
    backend = None
    try:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise Refused("reviewed_Linux_root_execution_only")
        config = private_json(args.config)
        if config.get("version") != 1 or config.get("approved_WEEKMAINT1_manifest") is not True:
            raise Refused("explicit_reviewed_weekmaint_manifest_required")
        if not args.boot_verify:
            calendar_gate(datetime.now(UTC), force_test=args.force_test)
        backend = build_backend(config, preview=args.dry_run)
        backend.validate_source()
        backend.acquire_exclusion()
        if args.boot_verify:
            marker = private_json(backend.state / "reboot.json")
            if (marker.get("head") != backend.approved_head
                    or marker.get("source_boot_id") == Path("/proc/sys/kernel/random/boot_id").read_text().strip()):
                raise Refused("actual_new_boot_and_same_reviewed_head_required")
            status = boot_report(backend, marker["operation"])
            print(encoded({"stage": "boot_verification", "status": status}).decode())
            return 0 if status == "HEALTHY" else 1
        if args.execute:
            backend.validate_runtime_inputs()
        report = run_job(backend, backend.clock(), dry_run=args.dry_run, force_test=args.force_test)
        if args.dry_run:
            print(encoded({"status": "READONLY_PLAN_NO_APPLY", "gates": report["gates"],
                "reason": report.get("reason"), "steps": report["steps"],
                "retirements": [{"table": item.table, "row_id": item.row_id, "account": item.account,
                                 "symbol": item.symbol, "source_sha256": item.source_hash}
                                for item in report["retirements"]],
                "unresolved_rows": report["unresolved_rows"], "snapshot_pruning": report["snapshot_pruning"]}).decode())
            return 0 if report["gates"] == "PASS_REQUIRES_RECHECK" else 2
        print(encoded({"status": report["status"], "operation": report["operation"],
            "preview_candidate_count": len(report["retirements"]),
            "unresolved_preview_count": len(report["unresolved_rows"]),
            "audit": "root-private operation/CSV; no raw rows on stdout"}).decode())
        return 0
    except Exception as error:
        # No secrets, account IDs, database URLs or raw broker payloads on stdout.
        print(encoded({"status": "WEEKMAINT1_REFUSED_OR_FAILED_NO_RETRY", "type": type(error).__name__}).decode())
        return 78
    finally:
        if backend is not None:
            backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
