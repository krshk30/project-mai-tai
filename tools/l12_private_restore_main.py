"""Explicit Main-only private construct/copy caller; never a service boot.

Requires an independently reviewed input packet and actual nonce capability.
No Settings, environment DSN, subprocess, provider, cluster lifecycle, adoption,
retry, source approval generation or historical Alembic stamp is available.
"""

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import time

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tools.l12_main_nonce_target import load_target_file, validate_execution_binding
from tools.l12_nonce_copy_lease import NonceCopyLease
from tools.l12_private_evidence_journal import PrivateJournal
from tools.l12_private_full23_lifecycle import CheckedFactory, PrivateAuthority, run_full23
from tools.l12_private_onechain import SCHEMA_PINS, construct_onechain
from tools.l12_scoped_chunks_v2 import Limits, canonical, validate_v2
from tools import l12_v10_branch_source_guard as v10
from tools.l12_private_schema_evidence import (
    C1_BASELINE, C1_UPGRADED, C1_GRAPH_VERSION, GRAPH_SOURCE_PINS,
    COMBINED_APP_PINS, COMBINED_APP_COMPONENT,
    is_c1_review, validate_c1_review, accepted_native_reference,
)

VERSION = "L12_MAIN_NONCE_CONSTRUCT_OR_FULL23_COPY_V1"
NATIVE_VERSION = "L12_MAIN_C1_NATIVE_ROW_RESTORE_V1"
BRIDGE_OK = "REVIEWED_BRIDGE_V2_COPY_INPUTS_BOUND_NOT_RESTORED_OR_NATIVE"
ONLINE_SCHEMA_PINS = {
    "sql/migrations/versions/20261023_atr_simulation_store.py": "185e4ee38f868b84fe031fc77232061e081b02dacf429bd4a0048d26aaaeea0b",
    "sql/migrations/versions/20261023_atr_aggregate_entry.py": "ece27e94d265b85811880c30fb639b488bb8013a815e68153e39f7b8982cb3b4",
    "sql/migrations/versions/20261022_l12_dispatch_retirement.py": "f028854fcda1a520c0228683ef71e7fbab0b254fbc1a33b904dc34559c5b4d54",
    "src/project_mai_tai/atr_simulation/store_schema_20261023.py": "c6e98ec1865b36f48d810d813dc36c6a335d579860d77549319e418d462bb2bc",
    "src/project_mai_tai/atr_simulation/store_schema_20261023_aggregate.py": "7a072363a7337a65c72f753ae9428907024f940c4b34c1bef3313a6f92aef828",
    "src/project_mai_tai/db/base.py": "923c290e8726dc2fbd10bd39e9cb3a4bf5a3e8e295294dad6a927da8a098133b",
    "sql/migrations/env.py": "acbc86a24bb31fad0824048d0f05b01bb2cd3f2494749e57df70585b2687ed08",
    "alembic.ini": "1b0ee9641106926ac1d6f9329e0af5809f6767272c3f019ca3df7699a870774f",
}
OFFLINE_CHILD_SHA = "8d37ab249ef5d6d395216c3dcc27dc7b9cf2648a628f72ed97be5aa0240109b7"
# Explicit proposed consumer component, observed in Main's current root. This
# does not amend historical V10 or approve that root: the exact clean inventory
# and separate consumer review still have to bind it before any connection.
CURRENT_COPY_SETTINGS_SHA = "b083f38863f91c7e30bf07e8df369b2472aa0d3a6ccb79b39d2f08664434dbff"

# Independently observed Galileo source checkpoint, not a qualified consumer.
# Keep this separate from every executable profile and historical component.
L11_REVIEW_CANDIDATE = {
    "head": "04052bc73c0d61adc1329e0bb28eb9ee5dae50aa",
    "path": "src/project_mai_tai/services/schwab_1m_v2_bot.py",
    "before_sha256": "6e7cb1d5dfeba487974c7710fce3ff58bf9715d41ced92636571e73504a0d8b0",
    "after_sha256": "ddc930f1bcf1f6253e54394b4fe5902e8b1fa21515a2641e6c3fa9e9e60b2a0c",
}


def inspect_l11_review_candidate(inventory, *, component_head):
    """File-only component comparison; deliberately returns no executable carry.

    Invert the ONE literal service delta, then use the unchanged whole-F guard.
    This cannot adopt Galileo's checkout, qualify its controls, or amend a copy
    capability. A future corrected head requires its own explicit source change.
    """
    candidate = L11_REVIEW_CANDIDATE
    if (type(inventory) is not dict or component_head != candidate["head"]
            or inventory.get(candidate["path"]) != candidate["after_sha256"]):
        raise ValueError("literal_L11_review_candidate_not_moving_HEAD_required")
    inverse = dict(inventory)
    inverse[candidate["path"]] = candidate["before_sha256"]
    require_current_f_app_inventory(inverse)
    return dict(status="SOURCE_CANDIDATE_ONLY_CORRECTED_APP_QUALIFICATION_PENDING",
        component_head=component_head, changed_paths=[candidate["path"]],
        preserved_component_head="2782fe6b1f8135703203682f4be633aa0c751768",
        executable_carry=None, permission_granted=False)


def json_value(raw):
    def reject(value):
        raise ValueError("nonfinite_review_packet")
    return json.loads(raw, object_pairs_hook=v10._pairs, parse_constant=reject)


def private_file(path, limit):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("canonical_absolute_private_file_required")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("private_input_symlink_refused")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or not 0 < info.st_size <= limit):
            raise ValueError("bounded_owned_0600_single_link_input_required")
        parts, remaining = [], limit + 1
        while remaining:
            raw = os.read(fd, min(remaining, 1024 * 1024))
            if not raw:
                break
            parts.append(raw)
            remaining -= len(raw)
        result = b"".join(parts)
        if len(result) != info.st_size:
            raise ValueError("private_input_changed_during_read")
        return result
    finally:
        os.close(fd)


def artifact(value, limit=8 * 1024 * 1024):
    if (not isinstance(value, dict) or set(value) != {"path", "sha256"}
            or not v10._sha(value["sha256"]) or value["sha256"] == "0" * 64):
        raise ValueError("explicit_actual_artifact_path_and_hash_required")
    raw = private_file(value["path"], limit)
    if sha256(raw).hexdigest() != value["sha256"]:
        raise ValueError("actual_artifact_hash_changed")
    return raw


def require_combined_app_inventory(inventory):
    app = {name: digest for name, digest in inventory.items() if name.startswith("src/project_mai_tai/")}
    if (len(app) != COMBINED_APP_COMPONENT["files"]
            or sha256(canonical(app)).hexdigest() != COMBINED_APP_COMPONENT["app_inventory_sha256"]
            or any(app.get(name) != digest for name, digest in COMBINED_APP_PINS.items())):
        raise ValueError("exact_06936_full_APP_inventory_and_thirteen_reviewed_deltas_required")


def require_current_359_app_inventory(inventory):
    """Compare only; do not rewrite source bytes, copy receipts or capabilities."""
    from tools.l12_private_schema_evidence import CURRENT_359_APP_COMPONENT, CURRENT_359_APP_DELTA
    app = {name: digest for name, digest in inventory.items() if name.startswith("src/project_mai_tai/")}
    if len(app) != CURRENT_359_APP_COMPONENT["files"]:
        raise ValueError("exact_359B_APP_membership_including_one_producer_addition_required")
    inverse = dict(inventory)
    for name, (before, after) in CURRENT_359_APP_DELTA.items():
        if app.get(name) != after:
            raise ValueError("literal_359B_changed_APP_component_bytes_required")
        if before == "ABSENT":
            del inverse[name]
        else:
            inverse[name] = before
    require_combined_app_inventory(inverse)


def verify_combined_tree(root, inventory):
    """Finite 06936 consumer only; historical V10 and its FORBIDDEN stay intact."""
    require_combined_app_inventory(inventory)
    return _verify_bound_combined_tree(root, inventory)


def require_current_f_app_inventory(inventory):
    from tools.l12_private_schema_evidence import CURRENT_F_APP_COMPONENT
    change = CURRENT_F_APP_COMPONENT
    if inventory.get(change["path"]) != change["after_sha256"]:
        raise ValueError("literal_F278_single_display_component_required")
    inverse = dict(inventory)
    inverse[change["path"]] = change["before_sha256"]
    require_current_359_app_inventory(inverse)


def verify_current_f_tree(root, inventory):
    require_current_f_app_inventory(inventory)
    return _verify_bound_combined_tree(root, inventory)


def require_corrected_l11_app_inventory(inventory):
    from tools.l12_private_schema_evidence import CORRECTED_L11_APP_COMPONENT
    component = CORRECTED_L11_APP_COMPONENT
    if inventory.get(component["path"]) != component["after_sha256"]:
        raise ValueError("literal_ff41_shared_completion_service_required")
    inverse = dict(inventory)
    inverse[component["path"]] = component["before_sha256"]
    require_current_f_app_inventory(inverse)


def verify_corrected_l11_tree(root, inventory):
    require_corrected_l11_app_inventory(inventory)
    return _verify_bound_combined_tree(root, inventory)


def require_bounded_l11_app_inventory(inventory):
    from tools.l12_private_schema_evidence import BOUNDED_L11_APP_COMPONENT
    component = BOUNDED_L11_APP_COMPONENT
    if inventory.get(component["path"]) != component["after_sha256"]:
        raise ValueError("literal_4f0150_bounded_callback_service_required")
    inverse = dict(inventory)
    inverse[component["path"]] = component["before_sha256"]
    require_current_f_app_inventory(inverse)


def verify_bounded_l11_tree(root, inventory):
    require_bounded_l11_app_inventory(inventory)
    return _verify_bound_combined_tree(root, inventory)


def verify_current_359_tree(root, inventory):
    """New fixed consumer profile only, not permission to retag old copy input."""
    require_current_359_app_inventory(inventory)
    return _verify_bound_combined_tree(root, inventory)


def _verify_bound_combined_tree(root, inventory):
    if not v10._pins(inventory) or len(inventory) > 20000:
        raise ValueError("v10_bounded_complete_source_inventory_required")
    names = set()
    for directory in v10.SOURCE_DIRS:
        base = v10._path(root, directory)
        if not base.is_dir():
            raise ValueError("v10_source_directory_missing")
        for parent, directories, files in os.walk(base, followlinks=False):
            if any((Path(parent) / name).is_symlink() for name in directories):
                raise ValueError("v10_symlink_directory")
            directories[:] = [name for name in directories if name != "__pycache__"]
            for name in files:
                if name.endswith((".pyc", ".pyo")):
                    continue
                names.add((Path(parent) / name).relative_to(root).as_posix())
                if len(names) > 20000:
                    raise ValueError("v10_source_inventory_overflow")
    if names != set(inventory):
        raise ValueError("v10_source_inventory_membership_changed")
    for name, expected in inventory.items():
        path = v10._path(root, name)
        if not path.is_file() or not 0 <= path.stat().st_size <= v10.MAX_FILE:
            raise ValueError("v10_member_type_or_size")
        if sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("v10_source_bytes_changed")
    if any(v10._path(root, name).exists() for name in v10.FORBIDDEN if name not in COMBINED_APP_PINS):
        raise ValueError("unreviewed_forbidden_dependency_not_in_exact_06936_component")


@dataclass(frozen=True)
class CopySourceGuard:
    root: Path
    head: str
    inventory_items: tuple
    expires_ms: int
    creation_head: str | None = None
    combined_app: bool = False
    current_359_app: bool = False
    current_f_app: bool = False
    corrected_l11_app: bool = False
    bounded_l11_app: bool = False

    def __call__(self, subject):
        modes = (self.current_359_app, self.current_f_app, self.corrected_l11_app, self.bounded_l11_app)
        if (any(type(mode) is not bool for mode in modes) or sum(modes) > 1
                or any(modes) and (not self.combined_app or self.creation_head is None)):
            return False
        actual_head = (subject.values["source_head"] if hasattr(subject, "values")
                       else getattr(subject, "execution_head", None))
        expected_head = (self.creation_head if hasattr(subject, "values") and self.creation_head is not None else self.head)
        if actual_head != expected_head or time.time_ns() // 1_000_000 >= self.expires_ms:
            return False
        try:
            inventory = dict(self.inventory_items)
            tree_inventory = inventory
            if self.creation_head is not None:
                # V10's historical directory inventory deliberately excludes
                # root-level ini files. Verify this new explicit member too.
                tree_inventory = dict(inventory)
                expected = tree_inventory.pop("alembic.ini", None)
                path = v10._path(self.root, "alembic.ini")
                if (not v10._sha(expected) or not path.is_file()
                        or path.stat().st_size > v10.MAX_FILE
                        or sha256(path.read_bytes()).hexdigest() != expected):
                    return False
            if self.bounded_l11_app:
                verify_bounded_l11_tree(self.root, tree_inventory)
            elif self.corrected_l11_app:
                verify_corrected_l11_tree(self.root, tree_inventory)
            elif self.current_f_app:
                verify_current_f_tree(self.root, tree_inventory)
            elif self.current_359_app:
                verify_current_359_tree(self.root, tree_inventory)
            elif self.combined_app:
                verify_combined_tree(self.root, tree_inventory)
            else:
                v10._verify_tree(self.root, tree_inventory)
            for name, module in tuple(sys.modules.items()):
                if not (name == "tools" or name.startswith("tools.")
                        or name == "project_mai_tai" or name.startswith("project_mai_tai.")):
                    continue
                filename = getattr(module, "__file__", None)
                prefix = self.root / ("tools" if name.startswith("tools") else "src/project_mai_tai")
                if filename is not None:
                    path = Path(filename)
                    if (not path.is_absolute() or not path.is_relative_to(prefix)
                            or path.resolve() != path
                            or path.relative_to(self.root).as_posix() not in inventory):
                        return False
                if any(not Path(item).resolve().is_relative_to(prefix)
                       for item in getattr(module, "__path__", ())):
                    return False
        except (OSError, ValueError):
            return False
        return True


def source_guard(value, target, *, native_review=None):
    native = is_c1_review(native_review)
    if native:
        validate_c1_review(native_review)
        consumer_head = validate_execution_binding(native_review["execution_binding"], target,
            inventory=value.get("inventory_pins") if isinstance(value, dict) else {})
        if (value.get("root") != native_review["execution_binding"]["consumer_root"]):
            raise ValueError("copy_consumer_root_differs_from_explicit_binding")
    else:
        consumer_head = target.values["source_head"]
    keys = {"root", "head", "inventory_pins", "clean_receipt", "scope_review_sha256", "expires_ms"}
    if (not isinstance(value, dict) or set(value) != keys or not v10._head(value["head"])
            or value["head"] != consumer_head
            or not v10._sha(value["scope_review_sha256"])
            or type(value["expires_ms"]) is not int or value["expires_ms"] <= 0
            or not isinstance(value["root"], str) or not Path(value["root"]).is_absolute()
            or str(Path(value["root"]).resolve()) != value["root"]
            or not v10._pins(value["inventory_pins"])):
        raise ValueError("reviewed_actual_current_copy_source_required")
    inventory = value["inventory_pins"]
    required = {**v10.APP_PINS, **SCHEMA_PINS}
    if native:
        schema_pins = dict(ONLINE_SCHEMA_PINS)
        child = "sql/migrations/versions/20261023_atr_aggregate_entry.py"
        if inventory.get(child) == OFFLINE_CHILD_SHA:
            schema_pins[child] = OFFLINE_CHILD_SHA
        required = {**v10.APP_PINS, **schema_pins}
        if native_review["version"] == C1_GRAPH_VERSION:
            required.update(GRAPH_SOURCE_PINS)
            required.update(COMBINED_APP_PINS)
            require_combined_app_inventory(inventory)
        else:
            required["src/project_mai_tai/settings.py"] = CURRENT_COPY_SETTINGS_SHA
    required_tools = {"tools/l12_private_restore_main.py", "tools/l12_private_onechain.py",
        "tools/l12_nonce_copy_lease.py", "tools/l12_main_nonce_target.py",
        "tools/l12_private_full23_lifecycle.py", "tools/l12_private_evidence_journal.py",
        "tools/l12_scoped_restore_v2.py", "tools/l12_private_schema_evidence.py"}
    if (any(inventory.get(name) != digest for name, digest in required.items())
            or not required_tools <= set(inventory)):
        raise ValueError("copy_APP_schema_or_loadbearing_tool_membership_changed")
    clean = json_value(artifact(value["clean_receipt"]))
    if clean != dict(root=value["root"], head=value["head"], clean=True, inventory_pins=inventory):
        raise ValueError("actual_independent_clean_root_receipt_disagrees")
    if clean.get("clean") is not True:
        raise ValueError("actual_clean_source_required")
    guard = CopySourceGuard(Path(value["root"]), value["head"], tuple(sorted(inventory.items())), value["expires_ms"],
        target.values["source_head"] if native else None,
        native and native_review["version"] == C1_GRAPH_VERSION)
    if not guard(target):
        raise ValueError("current_copy_source_or_import_origin_refused")
    return guard


class Parts:
    def __init__(self, root):
        self.root = Path(root)
        if (not self.root.is_absolute() or self.root.resolve() != self.root
                or not self.root.is_dir()):
            raise ValueError("exact_existing_bridge_quarantine_required")

    def __call__(self, name):
        original = name
        name = PurePosixPath(name)
        if (name.as_posix() != original or name.is_absolute() or not name.parts
                or name.parts[0] not in {"v2", "metadata", "source-v1"}
                or any(part in {"..", "."} for part in name.parts)):
            raise ValueError("canonical_bridge_part_reference_required")
        return private_file(self.root / name, 32 * 1024 * 1024)


def checked_packet(raw):
    value = json_value(raw)
    keys = {"version", "operation", "capability", "creation_review_sha256", "source",
        "manifest", "metadata", "bridge_receipt", "parts_root", "chunk_review", "schema_review",
        "writer_exclusion", "execution_review_sha256", "construction_receipt",
        "configured_accounts", "expanded_accounts"}
    if (not isinstance(value, dict) or set(value) != keys or value["version"] not in {VERSION, NATIVE_VERSION}
            or value["operation"] not in {"construct", "restore"}
            or not v10._sha(value["execution_review_sha256"])
            or not v10._sha(value["creation_review_sha256"])):
        raise ValueError("exact_independently_reviewed_operation_packet_required")
    if value["version"] == NATIVE_VERSION and value["operation"] != "restore":
        raise ValueError("C1_native_caller_is_existing_target_row_restore_only")
    if value["operation"] == "construct" and value["construction_receipt"] is not None:
        raise ValueError("fresh_construction_cannot_adopt_prior_receipt")
    if value["operation"] == "restore" and not isinstance(value["construction_receipt"], dict):
        raise ValueError("copy_requires_actual_committed_onechain_receipt")
    return value


def require_committed_construction(constructed, target):
    report = constructed.get("report") if isinstance(constructed, dict) else None
    if (not isinstance(report, dict)
            or constructed.get("kind") != "COMMITTED_PRIVATE_DDL_NOT_INDEPENDENT_SCHEMA_REVIEW"
            or report.get("status") != "PRIVATE_ONECHAIN_COMMITTED_CATALOG_REVIEW_PENDING_NOT_COPY_OR_BOOT"
            or "cleanup_failure_type" in report
            or report.get("target_capability_sha256") != target.reviewed_sha256
            or report.get("source_head") != target.values["source_head"]
            or report.get("source_pins") != SCHEMA_PINS):
        # Also refuse old-head journals whose early COMMITTED status survived
        # alongside cleanup uncertainty. Absence of an OMS row is never proof.
        raise ValueError("actual_onechain_commit_not_proven_or_cleanup_UNKNOWN")


def bind_native_restore_evidence(review, target):
    """Read only already-retained immutable proofs; no target observation/approval."""
    purpose = validate_c1_review(review)
    if review["version"] != C1_GRAPH_VERSION:
        raise ValueError("C1_row_DML_requires_graph_bound_V2_not_old_reflected_approval")
    raw = {key: artifact(value, 16 * 1024 * 1024) for key, value in review["evidence"].items()}
    ruling = json_value(raw["c1_ruling"])
    if (ruling.get("purpose") != "IMMUTABLE_C1_1409_RULING_CAPTURE_NOT_NEW_AUTHORITY"
            or ruling.get("heading") != "2026-10-24 14:09 ET"):
        raise ValueError("actual_C1_1409_ruling_capture_required")
    baseline = json_value(raw["baseline_graph"])
    comparison = json_value(raw["comparison"])
    graph = baseline.get("graph")
    if (not isinstance(graph, dict) or graph.get("role") != "PRIVATE_BASELINE"
            or comparison.get("status") != "INDEPENDENT_DELTA_HASH_MATCH_PENDING_C1_SOURCE_CARRY_AND_DELETE_GATE"
            or comparison.get("unexpected") != [] or comparison.get("missing") != []
            or comparison.get("excluded_keys_present_in_observed") != []
            or comparison.get("expected") != 266 or comparison.get("matched") != 266
            or comparison.get("ledger_sha256") != review["evidence"]["ledger"]["sha256"]):
        raise ValueError("accepted_independent266_and_exact_C1_baseline_proofs_required")
    selected = baseline if purpose == C1_BASELINE else json_value(raw["upgraded_graph"])
    selected_graph = selected.get("graph", {})
    expected_role = "PRIVATE_BASELINE" if purpose == C1_BASELINE else "PRIVATE_UPGRADED"
    if selected_graph.get("role") != expected_role:
        raise ValueError("accepted_native_graph_role_changed")
    identity = selected_graph.get("identity", {})
    values = target.values
    if any(identity.get(key) != values[field] for key, field in (
            ("database", "database"), ("role", "database_user"), ("system_identifier", "system_identifier"),
            ("postmaster_started", "postmaster_started"), ("server_version_num", "server_version_num"))):
        raise ValueError("approved_native_graph_epoch_does_not_match_original_nonce_target")
    proof = review["construction_review"]
    if (proof["target_capability_sha256"] != target.reviewed_sha256
            or proof["creation_source_head"] != values["source_head"]):
        raise ValueError("REAL_receipt_review_cannot_retag_capability_or_creation_source")
    validate_execution_binding(review["execution_binding"], target)
    return accepted_native_reference(review,
        raw["baseline_graph" if purpose == C1_BASELINE else "upgraded_graph"])


def execute(raw, *, reviewed_sha256):
    if not v10._sha(reviewed_sha256) or sha256(raw).hexdigest() != reviewed_sha256:
        raise ValueError("independent_exact_packet_review_required")
    packet = checked_packet(raw)
    cap = packet["capability"]
    artifact(cap, 16384)
    target = load_target_file(cap["path"], reviewed_sha256=cap["sha256"],
        expected_creation_review_sha256=packet["creation_review_sha256"])
    schema_review = json_value(artifact(packet["schema_review"]))
    native = packet["version"] == NATIVE_VERSION
    if native != is_c1_review(schema_review):
        raise ValueError("caller_version_and_schema_profile_must_agree_no_implicit_mode_change")
    native_reference = bind_native_restore_evidence(schema_review, target) if native else None
    guard = source_guard(packet["source"], target, native_review=schema_review if native else None)
    manifest = artifact(packet["manifest"], 16 * 1024 * 1024)
    metadata = artifact(packet["metadata"], 1024 * 1024)
    bridge = json_value(artifact(packet["bridge_receipt"]))
    review = json_value(artifact(packet["chunk_review"]))
    review["limits"] = Limits(**review["limits"])
    reader = Parts(packet["parts_root"])
    verified = validate_v2(manifest, metadata, reader, **review)
    if (bridge.get("status") != BRIDGE_OK or bridge.get("manifest_sha256") != verified["manifest_sha256"]
            or bridge.get("verified") != verified or bridge.get("database_operations") != 0
            or bridge.get("provider_connections") != 0):
        raise ValueError("actual_full_bridge_receipt_does_not_match_validated_copy")
    exclusion = json_value(artifact(packet["writer_exclusion"]))
    if exclusion != dict(target_capability_sha256=target.reviewed_sha256,
            source_head=target.values["source_head"], owner_uid=os.getuid(),
            statement="MAIN_OWNS_NONCE_AND_EXCLUDES_ALL_OTHER_WRITERS",
            expires_ms=packet["source"]["expires_ms"]):
        raise ValueError("actual_Main_writer_ownership_receipt_required")
    construction_sha = packet["execution_review_sha256"]
    if packet["operation"] == "construct" and schema_review != dict(
            purpose="REVIEWED_PRIVATE_ONECHAIN_CONSTRUCTION_NOT_CANONICAL_CATALOG_APPROVAL",
            source_pins=SCHEMA_PINS, review_sha256=packet["execution_review_sha256"]):
        raise ValueError("explicit_onechain_construction_review_required_not_catalog_self_approval")
    if packet["operation"] == "restore":
        if native:
            if packet["construction_receipt"] != schema_review["evidence"]["real_receipt"]:
                raise ValueError("packet_REAL_receipt_must_be_exact_native_profile_receipt")
        else:
            constructed = json_value(artifact(packet["construction_receipt"]))
            require_committed_construction(constructed, target)
        construction_sha = packet["construction_receipt"]["sha256"]
    engine, lease, journal = None, None, None
    failure = None
    try:
        lease = NonceCopyLease(target, manifest_sha256=verified["manifest_sha256"],
            metadata_sha256=verified["metadata_sha256"],
            schema_review_sha256=sha256(canonical(schema_review)).hexdigest(),
            writer_exclusion_receipt_sha256=packet["writer_exclusion"]["sha256"],
            source_guard=guard, operation=packet["operation"],
            execution_binding=schema_review["execution_binding"] if native else None)
        values = target.values
        authority = PrivateAuthority(execution_head=packet["source"]["head"] if native else values["source_head"],
            execution_review_sha256=packet["execution_review_sha256"],
            source_guard_receipt_sha256=packet["source"]["clean_receipt"]["sha256"],
            schema_construction_receipt_sha256=construction_sha,
            lease_receipt_sha256=lease.receipt_sha256, system_identifier=values["system_identifier"],
            postmaster_started=values["postmaster_started"], database_user=values["database_user"],
            server_version_num=values["server_version_num"],
            configured_accounts=tuple(packet["configured_accounts"]),
            expanded_accounts=tuple(packet["expanded_accounts"]))
        url = URL.create("postgresql+psycopg", username=values["database_user"], database=values["database"],
            query={"host": values["socket"], "port": values["port"]})
        engine = create_engine(url, echo=False, poolclass=NullPool,
            connect_args={"connect_timeout": 5})
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        directory = Path(values["root"]) / ("ddl-journal" if packet["operation"] == "construct" else "copy-journal")
        directory.mkdir(mode=0o700)
        journal = PrivateJournal(directory)
        if packet["operation"] == "construct":
            checked = CheckedFactory(factory, authority, lambda: guard(target) and lease._held_file(),
                target_capability=target, nonce_lease=lease)
            return construct_onechain(checked, root=guard.root, metadata_raw=metadata,
                metadata_sha256=verified["metadata_sha256"], target=target, lease=lease,
                source_guard=guard, append_readback=journal)
        return run_full23(factory, manifest, metadata, reader, review=review,
            schema_review=schema_review, authority=authority, source_guard=guard,
            require_lease=lease, append_readback=journal,
            observe_ms=lambda: time.time_ns() // 1_000_000, target_capability=target,
            execution_binding=schema_review["execution_binding"] if native else None,
            **({"native_reference": native_reference} if native else {}))
    except BaseException as exc:
        failure = exc
        raise
    finally:
        # Cleanup failures never mask the primary SQL/commit/journal outcome.
        for resource in (journal, lease, engine):
            if resource is None:
                continue
            try:
                resource.dispose() if resource is engine else resource.close()
            except BaseException as cleanup:
                if failure is not None:
                    failure.add_note("private_cleanup_failure_type=" + type(cleanup).__name__)
                else:
                    failure = cleanup
        if failure is not None and sys.exc_info()[0] is None:
            raise failure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", required=True)
    parser.add_argument("--reviewed-sha256", required=True)
    parser.add_argument("--execute-reviewed-private-operation", action="store_true")
    args = parser.parse_args()
    raw = private_file(args.packet, 8 * 1024 * 1024)
    if sha256(raw).hexdigest() != args.reviewed_sha256:
        raise ValueError("packet_review_hash_changed")
    packet = checked_packet(raw)
    if not args.execute_reviewed_private_operation:
        print(json.dumps(dict(status="PLAN_ONLY_NO_ENGINE_OR_TARGET_ACCESS", operation=packet["operation"])))
        return
    report = execute(raw, reviewed_sha256=args.reviewed_sha256)
    print(json.dumps(report, sort_keys=True, default=str))
    allowed = {"PRIVATE_ONECHAIN_COMMITTED_CATALOG_REVIEW_PENDING_NOT_COPY_OR_BOOT",
        "FULL23_READBACK_AND_INDEXED_COVERAGE_NOT_SERVICE_BOOT_OR_NATIVE_PROOF",
        "C1_BASELINE_SOURCE21_COPY_NOT_CONSTRUCTION_OR_BOOT"}
    if report.get("status") not in allowed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
