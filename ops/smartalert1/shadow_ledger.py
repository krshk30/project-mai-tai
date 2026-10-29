"""Finite FILE-only L4 shadow ledger. No clock, acquisition or transport port.

Default OFF precedes all file operations. Main provisions directory + initial
state explicitly and binds actual files/hashes/as-of. No implicit state reset.
Immutable generations plus one atomic pointer are the commit boundary. Orphan
generations after failure remain retained and do not count as published records.
"""

import argparse
from contextlib import ExitStack
from copy import deepcopy
import fcntl
import hashlib
import json
import os
import stat
from uuid import uuid4

from .delivery import FRAME_BYTES, SCHEMA, STATE_BYTES, Refused, encoded, hexhash, need, plan_shadow


def _regular(fd, mode=0o600):
    info = os.fstat(fd)
    need(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == mode
         and info.st_uid == os.geteuid() and info.st_nlink == 1, "private_file_required")
    return info


def _directory(path):
    """Walk every component with NOFOLLOW; never resolve a hostile symlink."""
    need(type(path) is str and path.startswith("/") and path != "/", "absolute_owned_directory_required")
    parts = path.split("/")[1:]
    need(all(part and part not in {".", ".."} for part in parts), "directory_path_unknown")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        need(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, "owned_0700_directory_required")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _parse(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise Refused("nonfinite_json_number")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)
    except Refused:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise Refused("strict_json_required") from None


def _read(fd, name, cap, expected=None):
    need(type(name) is str and name not in {"", ".", ".."} and "/" not in name, "filename_unknown")
    named = os.stat(name, dir_fd=fd, follow_symlinks=False)
    need(stat.S_ISREG(named.st_mode), "private_file_required")
    handle = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        before = _regular(handle)
        need((named.st_dev, named.st_ino) == (before.st_dev, before.st_ino), "input_name_changed")
        need(before.st_size <= cap, "file_resource_overflow")
        data = bytearray()
        while len(data) <= cap:
            part = os.read(handle, min(65536, cap + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        after = _regular(handle)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        need(len(data) <= cap and len(data) == before.st_size
             and all(getattr(before, key) == getattr(after, key) for key in fields), "file_changed_or_overflow")
        raw = bytes(data)
        digest = hashlib.sha256(raw).hexdigest()
        if expected is not None:
            need(digest == hexhash(expected), "file_hash_mismatch")
        return raw, digest
    finally:
        os.close(handle)


def _input(path, cap, expected):
    need(type(path) is str and "/" in path, "input_path_unknown")
    parent, name = path.rsplit("/", 1)
    fd = _directory(parent)
    try:
        raw, digest = _read(fd, name, cap, expected)
        return _parse(raw), digest
    finally:
        os.close(fd)


def _new_file(fd, name, data):
    handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=fd)
    try:
        _regular(handle)
        view = memoryview(data)
        while view:
            count = os.write(handle, view)
            need(count > 0, "short_file_write")
            view = view[count:]
        os.fsync(handle)
    finally:
        os.close(handle)


def write_shadow(*, mode="OFF", ledger_dir=None, batch_path=None, policy_path=None,
                 batch_sha256=None, policy_sha256=None, as_of=None, expected_revision=None,
                 expected_generation_sha256=None, initialization_path=None, initialization_sha256=None,
                 supplied_batch=None, supplied_policy=None, producer_index_sha256=None,
                 supplied_policy_file_sha256=None, producer_request_sha256=None):
    """No sends, no acquisition. Failures after pointer replacement are UNPROVEN.

    Caller retry must retain the failed receipt and read actual pointer/CAS first.
    Never delete an orphan or reset a namespace to manufacture a clean retry.
    """
    if mode == "OFF":
        return {"status": "OFF", "delivery_action": "NONE", "paging_eligible": False}
    if mode != "SHADOW":
        return {"status": "REFUSED", "reason_code": "shadow_only", "delivery_action": "NONE", "paging_eligible": False}
    published = False
    try:
        need(type(expected_revision) is int and expected_revision >= 0, "expected_revision_required")
        produced = supplied_batch is not None or supplied_policy is not None
        if produced:
            need(batch_path is None and policy_path is None and batch_sha256 is None
                 and policy_sha256 is None, "mixed_file_and_produced_inputs")
            need(type(supplied_batch) is dict and type(supplied_policy) is dict,
                 "produced_inputs_unknown")
            hexhash(producer_index_sha256)
            if producer_request_sha256 is not None:
                hexhash(producer_request_sha256)
            policy_hash = hexhash(supplied_policy_file_sha256)
            # Preserve typed JSON values exactly; no path re-open or mutation
            # of the caller's already verified frame during CAS publication.
            encoded(supplied_batch, FRAME_BYTES)
            encoded(supplied_policy, FRAME_BYTES)
            batch, policy = deepcopy(supplied_batch), deepcopy(supplied_policy)
            batch_hash = hashlib.sha256(encoded(batch, FRAME_BYTES)).hexdigest()
        else:
            need(producer_index_sha256 is None and supplied_policy_file_sha256 is None,
                 "unbound_producer_receipt")
            need(producer_request_sha256 is None, "unbound_request_receipt")
            hexhash(batch_sha256)
            hexhash(policy_sha256)
            batch, batch_hash = _input(batch_path, FRAME_BYTES, batch_sha256)
            policy, policy_hash = _input(policy_path, FRAME_BYTES, policy_sha256)
        need(type(policy) is dict and policy.get("enabled") is True and policy.get("mode") == "SHADOW",
             "explicit_shadow_policy_required")
        with ExitStack() as stack:
            fd = _directory(ledger_dir)
            stack.callback(os.close, fd)
            lock = os.open(".writer.lock", os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                           0o600, dir_fd=fd)
            stack.callback(os.close, lock)
            _regular(lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused("writer_already_owned") from None
            try:
                raw_pointer, _ = _read(fd, "current.json", 4096)
            except FileNotFoundError:
                need(expected_generation_sha256 is None and expected_revision == 0
                     and initialization_path is not None, "missing_state_not_empty_boot")
                hexhash(initialization_sha256)
                state, _ = _input(initialization_path, STATE_BYTES, initialization_sha256)
                need(type(state) is dict and state.get("revision") == 0 and state.get("incidents") == {}
                     and state.get("batch_receipts") == {} and state.get("digests") == {}, "initialization_not_pristine")
                previous_hash = None
                previous_pointer_sha = None
            else:
                need(initialization_path is None and initialization_sha256 is None, "initialization_cannot_reset_existing_state")
                pointer = _parse(raw_pointer)
                need(type(pointer) is dict and pointer.get("schema_version") == SCHEMA, "pointer_unknown")
                need(type(pointer.get("revision")) is int, "pointer_revision_unknown")
                previous_pointer_sha = hashlib.sha256(raw_pointer).hexdigest()
                previous_hash = hexhash(pointer.get("generation_sha256"))
                need(previous_hash == hexhash(expected_generation_sha256)
                     and pointer.get("revision") == expected_revision, "pointer_CAS_conflict")
                name = "generation-" + previous_hash + ".json"
                need(pointer.get("file") == name, "pointer_filename_unknown")
                raw_generation, _ = _read(fd, name, STATE_BYTES, previous_hash)
                generation = _parse(raw_generation)
                need(type(generation) is dict and generation.get("schema_version") == SCHEMA, "generation_unknown")
                state = generation.get("state")
            need(type(state) is dict and state.get("revision") == expected_revision, "state_revision_conflict")
            need(type(batch) is dict, "batch_unknown")
            decision = plan_shadow(batch, state, as_of=as_of, policy=policy,
                resolution_links=batch.get("resolution_links", []), digest_window=batch.get("digest_window"))
            if decision["status"] != "SHADOW":
                return {"status": decision["status"], "reason_code": decision.get("reason_code"),
                        "delivery_action": "NONE", "paging_eligible": False}
            generation = {"schema_version": SCHEMA, "previous_generation_sha256": previous_hash,
                "batch_file_sha256": None if produced else batch_hash, "policy_file_sha256": policy_hash,
                "batch_input_kind": "PRODUCED_FROM_SEALED_FILES" if produced else "FILE",
                "produced_batch_sha256": batch_hash if produced else None,
                "producer_index_sha256": producer_index_sha256,
                "producer_request_sha256": producer_request_sha256,
                "state": decision["next_state"], "records": decision["records"], "digest": decision["digest"],
                "delivery_action": "NONE", "paging_eligible": False}
            raw_generation = encoded(generation, STATE_BYTES)
            digest = hashlib.sha256(raw_generation).hexdigest()
            filename = "generation-" + digest + ".json"
            try:
                _new_file(fd, filename, raw_generation)
            except FileExistsError:
                existing, _ = _read(fd, filename, STATE_BYTES, digest)
                need(existing == raw_generation, "immutable_generation_conflict")
            os.fsync(fd)  # Generation durable BEFORE publishing a pointer to it.
            pointer = {"schema_version": SCHEMA, "generation_sha256": digest, "file": filename,
                       "revision": decision["next_state"]["revision"]}
            raw_pointer = encoded(pointer, 4096)
            temporary = "current-" + uuid4().hex + ".tmp"
            _new_file(fd, temporary, raw_pointer)
            # Keep the original revision/lock identity through publication;
            # never silently replace a changed pointer or a substituted lock.
            observed_lock = os.open(".writer.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                held, current_lock = _regular(lock), _regular(observed_lock)
                need((held.st_dev, held.st_ino) == (current_lock.st_dev, current_lock.st_ino), "writer_lock_replaced")
            finally:
                os.close(observed_lock)
            try:
                _, actual_pointer_sha = _read(fd, "current.json", 4096)
            except FileNotFoundError:
                need(previous_pointer_sha is None, "pointer_disappeared_before_commit")
            else:
                need(previous_pointer_sha is not None and actual_pointer_sha == previous_pointer_sha,
                     "pointer_changed_before_commit")
            current_directory = _directory(ledger_dir)
            try:
                held_dir, named_dir = os.fstat(fd), os.fstat(current_directory)
                need((held_dir.st_dev, held_dir.st_ino) == (named_dir.st_dev, named_dir.st_ino),
                     "ledger_directory_replaced")
            finally:
                os.close(current_directory)
            os.replace(temporary, "current.json", src_dir_fd=fd, dst_dir_fd=fd)
            published = True
            os.fsync(fd)
            readback, _ = _read(fd, "current.json", 4096, hashlib.sha256(raw_pointer).hexdigest())
            need(readback == raw_pointer, "pointer_readback_mismatch")
            committed, _ = _read(fd, filename, STATE_BYTES, digest)
            need(committed == raw_generation, "generation_readback_mismatch")
            return {"status": "SHADOW_COMMITTED", "revision": pointer["revision"],
                    "generation_sha256": digest, "delivery_action": "NONE", "paging_eligible": False}
    except (Refused, OSError, TypeError, ValueError, RecursionError) as exc:
        return {"status": "COMMIT_UNPROVEN" if published else "REFUSED",
                "reason_code": exc.args[0] if type(exc) is Refused else "file_operation_or_shape_failed",
                "delivery_action": "NONE", "paging_eligible": False}


def main():
    parser = argparse.ArgumentParser(description="Finite supplied-file SMART shadow ledger; no phones")
    parser.add_argument("--mode", choices=("OFF", "SHADOW"), default="OFF")
    for name in ("ledger-dir", "batch-path", "policy-path", "batch-sha256", "policy-sha256", "as-of",
                 "expected-generation-sha256", "initialization-path", "initialization-sha256"):
        parser.add_argument("--" + name)
    parser.add_argument("--expected-revision", type=int)
    options = vars(parser.parse_args())
    # This module remains the persistence primitive, not an operational path
    # that can ship L4 alone while bypassing the finite L2+L3 producer.
    result = ({"status": "REFUSED", "reason_code": "bundled_producer_entrypoint_required",
               "delivery_action": "NONE", "paging_eligible": False}
              if options["mode"] == "SHADOW" else write_shadow(**options))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"OFF", "IDEMPOTENT", "SHADOW_COMMITTED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
