"""Main-owned offline lease: real lock/file proof plus actual client census.

This is cooperative exclusion on a fresh private nonce, not a universal DB
write fence or broker permission. Main must separately own/exclude all writers.
No role grants, database alteration, process discovery or source approval here.
"""

import fcntl
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import time

from sqlalchemy import text

from tools.l12_main_nonce_target import MainNonceTarget, validate_execution_binding
from tools.l12_private_evidence_journal import PrivateJournal


_WALL_NS = time.time_ns  # Keep lease expiry outside the boot's retimed clocks.


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


POSTCOPY_OPERATIONS = frozenset(("boot", "manual_stop", "mirror"))
POSTCOPY_HASHES = frozenset(("target_capability_sha256", "copy_result_sha256",
    "copy_lease_sha256", "copy_closure_sha256", "execution_review_sha256",
    "consumer_binding_sha256", "source_inventory_sha256"))


def validate_postcopy_context(context, *, operation, target_sha256, execution_binding,
                              observed_ns):
    """Pure shape/binding validation, never artifact acceptance or a lease."""
    fields = POSTCOPY_HASHES | {"version", "operation", "claim_id", "expires_ns", "maximum_sessions"}
    if (operation not in POSTCOPY_OPERATIONS or type(context) is not dict or set(context) != fields
            or context["version"] != "L12_POSTCOPY_PRIVATE_STAGE_V1"
            or context["operation"] != operation or context["target_capability_sha256"] != target_sha256
            or type(context["claim_id"]) is not str or not 1 <= len(context["claim_id"]) <= 128
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for char in context["claim_id"])
            or type(context["expires_ns"]) is not int or type(observed_ns) is not int
            or context["expires_ns"] <= observed_ns
            or type(context["maximum_sessions"]) is not int or context["maximum_sessions"] <= 0
            or any(type(context[key]) is not str or len(context[key]) != 64 or context[key] == "0" * 64
                or any(char not in "0123456789abcdef" for char in context[key]) for key in POSTCOPY_HASHES)
            or type(execution_binding) is not dict
            or context["consumer_binding_sha256"] != sha256(canonical(execution_binding)).hexdigest()
            or context["source_inventory_sha256"] != execution_binding.get("consumer_inventory_sha256")):
        raise ValueError("exact_unexpired_postcopy_claim_copy_closure_consumer_and_budget_required")
    return deepcopy(context)


class NonceCopyLease:
    def __init__(self, target, *, manifest_sha256, metadata_sha256, schema_review_sha256,
                 writer_exclusion_receipt_sha256, source_guard, operation="restore", execution_binding=None,
                 postcopy_context=None, postcopy_guard=None):
        hashes = (manifest_sha256, metadata_sha256, schema_review_sha256, writer_exclusion_receipt_sha256)
        if (operation not in {"construct", "restore"} | POSTCOPY_OPERATIONS
                or type(target) is not MainNonceTarget or not callable(source_guard)
                or any(not isinstance(value, str) or len(value) != 64 or value == "0" * 64
                    or any(char not in "0123456789abcdef" for char in value) for value in hashes)
                or source_guard(target) is not True):
            raise ValueError("actual_nonce_lease_source_and_exclusion_artifacts_required")
        self.postcopy_context, self.postcopy_guard = None, None
        self.postcopy_started, self.postcopy_finished, self.postcopy_sessions = False, False, 0
        self.boot_arms, self.boot_arm_active, self.boot_arm_failed = [], None, False
        if operation in POSTCOPY_OPERATIONS:
            self.postcopy_context = validate_postcopy_context(postcopy_context, operation=operation,
                target_sha256=target.reviewed_sha256, execution_binding=execution_binding,
                observed_ns=_WALL_NS())
            if not callable(postcopy_guard) or postcopy_guard(target, deepcopy(self.postcopy_context)) is not True:
                raise ValueError("independent_Main_copy_closure_current_source_and_execution_acceptance_required")
            self.postcopy_guard = postcopy_guard
        elif postcopy_context is not None or postcopy_guard is not None:
            raise ValueError("postcopy_proof_cannot_relabel_construct_or_restore")
        self.target, self.source_guard = target, source_guard
        self.context = dict(operation=operation, target_capability_sha256=target.reviewed_sha256,
            source_head=target.values["source_head"], manifest_sha256=manifest_sha256,
            metadata_sha256=metadata_sha256, schema_review_sha256=schema_review_sha256,
            writer_exclusion_receipt_sha256=writer_exclusion_receipt_sha256)
        if execution_binding is not None:
            if operation != "restore" and operation not in POSTCOPY_OPERATIONS:
                raise ValueError("consumer_carry_is_copy_only_no_creation_or_DDL")
            self.context["execution_head"] = validate_execution_binding(execution_binding, target)
            self.context["execution_binding_sha256"] = sha256(canonical(execution_binding)).hexdigest()
        if self.postcopy_context is not None:
            self.context["postcopy"] = deepcopy(self.postcopy_context)
        self.path = Path(target.values["root"]) / (operation + "-offline-writer.lock")
        self.closed, self.bound_binding, self.ddl_consumed = False, None, False
        # Both stages share this cooperative lock. Distinct O_EXCL stage files
        # below retain one-attempt evidence, without allowing stages to overlap.
        gate_path = Path(target.values["root"]) / "exclusive-writer.lock"
        gate_raw = canonical(dict(target_capability_sha256=target.reviewed_sha256))
        created = False
        try:
            self.gate_fd = os.open(gate_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
        except FileExistsError:
            self.gate_fd = os.open(gate_path, os.O_RDWR | os.O_NOFOLLOW)
        self.fd = None
        try:
            fcntl.flock(self.gate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            gate_info = os.fstat(self.gate_fd)
            if (not stat.S_ISREG(gate_info.st_mode) or gate_info.st_uid != os.getuid()
                    or stat.S_IMODE(gate_info.st_mode) != 0o600 or gate_info.st_nlink != 1):
                raise ValueError("exact_nonce_exclusion_lock_identity_required")
            if created:
                if os.write(self.gate_fd, gate_raw) != len(gate_raw):
                    raise OSError("gate_short_write")
                os.fsync(self.gate_fd)
            if os.pread(self.gate_fd, len(gate_raw) + 1, 0) != gate_raw:
                raise ValueError("nonce_exclusion_lock_context_changed")
            self.gate_path, self.gate_raw, self.gate_identity = gate_path, gate_raw, gate_info
            if self.postcopy_context is not None:
                self._original_copy_marker()
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.raw = canonical(dict(version="L12_NONCE_OFFLINE_WRITER_LEASE_V1", context=self.context,
                owner_pid=os.getpid(), namespace="EXACT_PRIVATE_NONCE_ONLY_NOT_NATIVE_OR_PRODUCTION"))
            remaining = memoryview(self.raw)
            while remaining:
                written = os.write(self.fd, remaining)
                if written <= 0:
                    raise OSError("lease_short_write")
                remaining = remaining[written:]
            os.fsync(self.fd)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.identity = os.fstat(self.fd)
            self.receipt_sha256 = sha256(self.raw).hexdigest()
            self._held_file()
            records = Path(target.values["root"]) / (operation + "-lease-journal")
            records.mkdir(mode=0o700)
            self.journal = PrivateJournal(records)
            self.journal(self.raw)
            self.file_receipt = self.journal.last_receipt
        except BaseException:
            if self.fd is not None:
                os.close(self.fd)
            os.close(self.gate_fd)
            self.closed = True
            raise

    def _held_file(self):
        if self.closed or self.source_guard(self.target) is not True:
            raise ValueError("nonce_lease_closed_or_source_changed")
        if self.postcopy_context is not None:
            if (_WALL_NS() >= self.postcopy_context["expires_ns"]
                    or self.postcopy_guard(self.target, deepcopy(self.postcopy_context)) is not True):
                raise ValueError("postcopy_claim_expired_or_independent_evidence_changed")
            self._original_copy_marker()
        gate, named_gate = os.fstat(self.gate_fd), self.gate_path.lstat()
        if (not stat.S_ISREG(gate.st_mode) or gate.st_uid != os.getuid()
                or stat.S_IMODE(gate.st_mode) != 0o600 or gate.st_nlink != 1
                or (gate.st_dev, gate.st_ino) != (named_gate.st_dev, named_gate.st_ino)
                or (gate.st_dev, gate.st_ino) != (self.gate_identity.st_dev, self.gate_identity.st_ino)
                or os.pread(self.gate_fd, len(self.gate_raw) + 1, 0) != self.gate_raw):
            raise ValueError("nonce_shared_exclusion_lock_changed")
        info, named = os.fstat(self.fd), self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
                or (info.st_dev, info.st_ino) != (self.identity.st_dev, self.identity.st_ino)
                or os.pread(self.fd, len(self.raw) + 1, 0) != self.raw):
            raise ValueError("actual_nonce_lease_identity_or_bytes_changed")
        return True

    def _original_copy_marker(self):
        """Observe, never delete/adopt the consumed original restore tombstone.

        The shared gate excludes other cooperating stages; the original stage
        lock must additionally be acquirable. Its closed journal/result still
        require the independent Main postcopy_guard, not PID/row absence.
        """
        path = Path(self.target.values["root"]) / "restore-offline-writer.lock"
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        primary = None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info, named = os.fstat(fd), path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or not 0 < info.st_size <= 16384
                    or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
                raise ValueError("original_consumed_copy_marker_identity_required")
            raw = os.pread(fd, 16385, 0)
            if sha256(raw).hexdigest() != self.postcopy_context["copy_lease_sha256"]:
                raise ValueError("original_consumed_copy_marker_bytes_changed")
            value = json.loads(raw)
            if (value.get("version") != "L12_NONCE_OFFLINE_WRITER_LEASE_V1"
                    or value.get("context", {}).get("operation") != "restore"
                    or value["context"].get("target_capability_sha256") != self.target.reviewed_sha256):
                raise ValueError("postcopy_requires_original_same_target_restore_marker")
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                os.close(fd)
            except BaseException as cleanup:
                if primary is None:
                    raise
                primary.add_note("original_copy_marker_cleanup_type=" + type(cleanup).__name__)

    def begin_postcopy(self):
        self._held_file()
        if self.postcopy_context is None or self.postcopy_started:
            raise ValueError("one_postcopy_stage_attempt_only_no_retry")
        self.journal(canonical(dict(kind="BEGIN_ONE_POSTCOPY_STAGE", context=self.context,
            copy_acceptance="INDEPENDENT_MAIN_GUARD_REQUIRED_NOT_INFERRED_FROM_MARKER")))
        self.postcopy_started = True

    def require_postcopy(self, operation):
        self._held_file()
        if (operation not in POSTCOPY_OPERATIONS or self.context["operation"] != operation
                or not self.postcopy_started or self.postcopy_finished):
            raise ValueError("exact_started_postcopy_operation_required")
        return True

    def finish_postcopy(self, *, outcome, result_sha256):
        self.require_postcopy(self.context["operation"])
        if (outcome == "OBSERVED_NOT_QUALIFICATION" and (self.boot_arm_active is not None
                or self.boot_arm_failed or self.boot_arms and len(self.boot_arms) != 2)):
            raise ValueError("sequential_boot_incomplete_or_failed_cannot_finish_observed")
        if (outcome not in {"OBSERVED_NOT_QUALIFICATION", "FAILED", "UNKNOWN"}
                or type(result_sha256) is not str or len(result_sha256) != 64
                or result_sha256 == "0" * 64
                or any(char not in "0123456789abcdef" for char in result_sha256)):
            raise ValueError("exact_postcopy_actual_result_and_non_GO_outcome_required")
        self.journal(canonical(dict(kind="END_ONE_POSTCOPY_STAGE", context=self.context,
            outcome=outcome, result_sha256=result_sha256,
            permission="NOT_GRANTED_BY_LEASE_OR_RESULT_HASH")))
        self.postcopy_finished = True

    @staticmethod
    def _arm_hash(value):
        return (type(value) is str and len(value) == 64 and value != "0" * 64
            and all(char in "0123456789abcdef" for char in value))

    def begin_boot_arm(self, variant, *, preimage_sha256, readback_guard):
        """Main supplies actual readback acceptance, never a rewound copy."""
        self.require_postcopy("boot")
        expected = ("EMPTY_FLAT", "ONE_WORKING_BUY")
        try:
            index = len(self.boot_arms)
            if (self.boot_arm_failed or self.boot_arm_active is not None or index >= 2
                    or variant != expected[index] or not self._arm_hash(preimage_sha256)
                    or index == 1 and preimage_sha256 != self.boot_arms[0]["postimage_sha256"]
                    or not callable(readback_guard)
                    or readback_guard(self.target, variant, preimage_sha256) is not True):
                raise ValueError("exact_sequential_boot_preimage_order_and_Main_readback_required")
            self.boot_arm_active = dict(variant=variant, preimage_sha256=preimage_sha256)
            self.journal(canonical(dict(kind="BEGIN_SEQUENTIAL_BOOT_ARM", **self.boot_arm_active,
                context=self.context, scope="ARM2_FROM_ARM1_ACTUAL_POSTSTATE_NO_RESET")))
        except BaseException:
            self.boot_arm_failed = True
            raise

    def require_boot_arm(self, variant):
        self.require_postcopy("boot")
        if (self.boot_arm_failed or self.boot_arm_active is None
                or self.boot_arm_active["variant"] != variant):
            self.boot_arm_failed = True
            raise ValueError("exact_active_sequential_boot_arm_required")

    def finish_boot_arm(self, variant, *, postimage_sha256, result_sha256,
                        allowed_mutations_review_sha256, readback_guard):
        """The guard must prove real row/owner conservation and arm2 no-resend.

        Hash shape or successful lifecycle return is not such acceptance. Main's
        independent guard binds the actual pre/post readbacks and fixed mutation
        review (04 roll, tick flush, stores), not a target-derived permission.
        """
        try:
            self.require_boot_arm(variant)
            if (not all(self._arm_hash(value) for value in (postimage_sha256,
                    result_sha256, allowed_mutations_review_sha256)) or not callable(readback_guard)
                    or readback_guard(self.target, dict(self.boot_arm_active), postimage_sha256,
                        result_sha256, allowed_mutations_review_sha256) is not True):
                raise ValueError("actual_boot_poststate_mutations_owner_and_no_resend_proof_required")
            row = dict(self.boot_arm_active, postimage_sha256=postimage_sha256,
                result_sha256=result_sha256, allowed_mutations_review_sha256=allowed_mutations_review_sha256)
            self.journal(canonical(dict(kind="END_SEQUENTIAL_BOOT_ARM", **row,
                scope="OBSERVED_NOT_NATIVE_OR_INSTALL_PERMISSION")))
            self.boot_arms.append(row)
            self.boot_arm_active = None
        except BaseException:
            self.boot_arm_failed = True
            raise

    def fail_boot_arm(self):
        self.boot_arm_failed = True  # Preserve active arm/journal; no retry or reset.

    def bind_inputs(self, *, plan, schema_review, authority, binding):
        self._held_file()
        if (self.context["operation"] != "restore" or self.bound_binding is not None
                or authority.execution_head != self.context.get("execution_head", self.context["source_head"])
                or authority.lease_receipt_sha256 != self.receipt_sha256
                or plan["manifest_sha256"] != self.context["manifest_sha256"]
                or plan["metadata_sha256"] != self.context["metadata_sha256"]
                or sha256(canonical(schema_review)).hexdigest() != self.context["schema_review_sha256"]
                or not isinstance(binding, str) or len(binding) != 64):
            raise ValueError("nonce_lease_input_binding_changed_or_already_consumed")
        self.journal(canonical(dict(kind="BIND_REVIEWED_INPUTS", binding=binding,
            context=self.context, lease_receipt_sha256=self.receipt_sha256)))
        self.bound_binding = binding

    def begin_ddl(self):
        self._held_file()
        if self.context["operation"] != "construct" or self.ddl_consumed:
            raise ValueError("onechain_attempt_already_consumed_no_retry")
        self.ddl_consumed = True
        self.journal(canonical(dict(kind="BEGIN_ONE_REVIEWED_DDL_ATTEMPT", context=self.context)))

    def __call__(self, binding, authority):
        self._held_file()
        if self.postcopy_context is not None:
            raise ValueError("postcopy_stage_is_not_restore_lifecycle_authority")
        if (binding != self.bound_binding or authority.lease_receipt_sha256 != self.receipt_sha256
                or authority.execution_head != self.context.get("execution_head", self.context["source_head"])):
            raise ValueError("nonce_lease_exact_lifecycle_binding_required")
        return True

    def verify_session(self, session):
        self._held_file()
        if self.postcopy_context is not None:
            self.require_postcopy(self.context["operation"])
            if self.postcopy_sessions >= self.postcopy_context["maximum_sessions"]:
                raise ValueError("postcopy_predeclared_session_budget_exhausted_no_growth")
            # Failed connection/identity/census attempts also consume budget.
            self.postcopy_sessions += 1
        self.target.verify_session(session)
        others = session.scalar(text("SELECT count(*) FROM pg_stat_activity "
            "WHERE backend_type='client backend' AND pid <> pg_backend_pid()"))
        if type(others) is not int or others != 0:
            raise ValueError("other_nonce_client_backend_present_STOP")
        self.journal(canonical(dict(kind="ACTUAL_PRIVATE_CLIENT_CENSUS_ZERO_AT_SESSION_OPEN",
            target_capability_sha256=self.target.reviewed_sha256, other_clients=others,
            universal_future_writer_exclusion="NOT_ESTABLISHED_BY_CENSUS")))

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.journal.close()
            finally:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                finally:
                    try:
                        os.close(self.fd)
                    finally:
                        try:
                            fcntl.flock(self.gate_fd, fcntl.LOCK_UN)
                        finally:
                            os.close(self.gate_fd)
        # Preserve lock/journal evidence. No unlink, retry or adoption.
