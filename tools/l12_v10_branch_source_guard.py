"""Explicit V10 read-only source guard. Never discovers/approves a moving HEAD.

Main supplies independent raw clean/baseline observations and per-change review.
This module reads reviewed files only: no Git, imports of APP, writes, DB or GO.
Historical V9 and its original122 guard remain unchanged.
"""

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys

from tools.l12_l14_class_binding import BASELINE_PINS as HISTORICAL_V9


VERSION = "L12_L14_AGGREGATE_FULL_APP_V10"
OLD_HEAD = "2e79a1282eaed6ec8ae6b8ca4770ebdf08e0e8e9"
APP_COMPONENT = "de75abd2139e2e337de37ae311fad9f8870273f5"
SCHEMA_COMPONENT = "8c445c1181c31a130484721754b1e60688a461d0"
SOURCE_DIRS = ("src/project_mai_tai", "sql", "ops", "tools")
MAX_FILE = 4 * 1024 * 1024
MAX_RECEIPT = 8 * 1024 * 1024
MODULE = "project_mai_tai.strategy_core.schwab_1m_v2"
CORE = "src/project_mai_tai/strategy_core/schwab_1m_v2.py"


def canonical_name(name):
    return name if name.startswith(("tools/", "sql/", "ops/")) else "src/project_mai_tai/" + name


V9_BEFORE = {canonical_name(name): value for name, value in HISTORICAL_V9.items()}
ADDITIONS = (
    "sql/migrations/versions/20261023_atr_aggregate_entry.py",
    "src/project_mai_tai/atr_simulation/store_schema_20261023_aggregate.py",
    "src/project_mai_tai/db/base.py", "src/project_mai_tai/events.py",
    "src/project_mai_tai/fanout_identity.py", "src/project_mai_tai/fanout_segment_store.py",
    "src/project_mai_tai/atr_country_route.py", "tools/l12_retained_owner_packet.py",
    "tools/l12_original122_embedded_identity.py", "tools/l12_original122_owner_crosswalk.py",
    "tools/l12_original122_branch_adapter.py", "tools/l12_branch_source_guard.py",
    "tools/l12_capture_manifest_bridge.py", "tools/l12_capture_resource_measurement.py",
    "tools/l12_single_snapshot_capture.py", "tools/l12_measure_existing_database.py",
    "tools/l12_measure_existing_database.sh", "tools/l12_v10_branch_source_guard.py",
    "tools/l12_v10_test_projection.py", "ops/health/expected_flags.json",
)
MEMBERS = frozenset(V9_BEFORE) | frozenset(ADDITIONS)
APP_PINS = {canonical_name(name): value for name, value in {
    "broker_adapters/routing.py": "c97b3ceaba53c5a5b8a6f0fbfa3ee2dc0de06c864e6f25f7f25a3fddaa3d46b8",
    "broker_adapters/schwab.py": "4d52addfbfd48c7bd891979ca4e3cac4562940fbd79eda4e788a15e219008988",
    "broker_adapters/webull.py": "da3be1841a5358271285bff7f5c956e870e0997d02732da5951b415371a59c56",
    "brokerdecides1.py": "0f3132bbd9d84154107024a05e1327678777695cfdc5fcca7093c412172eaf98",
    "brokerdecides1_accounts.py": "49b38edab2705d348baf05baa674022dc1294106603909a3400851cb02ad28dc",
    "brokerdecides1_alert_transport.py": "f488f3a427400a7fbd47f9dfe0f9c025abfb3dc9f19877b49ace77162ef49e6b",
    "brokerdecides1_closed_owner.py": "073f4d72abdd6682229e0d33a20d8cd40c32924fbcacef5af03164316937d281",
    "brokerdecides1_collect.py": "74895e5c6d747b021232c0e11a43a3f60b0d259d108458ab8cda967eda73ac9b",
    "brokerdecides1_entry.py": "a7ad8b0373ca10c4d772dcf863eb5ed699d13b3f2e09491fa7332554596eadb8",
    "brokerdecides1_install_chunks.py": "de1fad979b35af74bfae77616a31334bc7d00107685bda61e91765464746c815",
    "brokerdecides1_native.py": "389202ffe0cf7e27339e09f2720aa25afea3ac89ced83a77ac5069b82ec9a6c1",
    "brokerdecides1_owner_phone.py": "f87bdaac58e7908f4ee9587e34c4ed33bd1897c420b5855a16546b62ca77bdcd",
    "brokerdecides1_owner_watchdog.py": "bca726e8f1ea4ce13030ac453cbfa2b08a86533cfbd28feedfcfd419e4561e6e",
    "brokerdecides1_prewire.py": "a5ea4a222bb8aa58fe769a58c517e60d6f628e79a1dccb3222f93d63d5041979",
    "brokerdecides1_prewire_authority.py": "f4b7dd4459629ac2135935f8e060d358867b0cd7ab73b1751bf23ef739b5cbb9",
    "brokerdecides1_prewire_proof.py": "2909dac23dc9010b8719bb2b040d8d4dfd29c673eed99bb6f74fa31d99558613",
    "brokerdecides1_publication_callbacks.py": "46dc439a48de154991191ae2f0c93c007f5588c27fd2184aa66906ef785344fd",
    "brokerdecides1_retirement.py": "d4a8b1b718c72b12f0ce2973488e3e0a925930aeb915560b36dee6afbea4ad2d",
    "brokerdecides1_set_proof.py": "47a26c6859952685f36309a02d975d4b088040ccbd2df8806f2c85b16a70c82c",
    "brokerdecides1_sync.py": "6b2c8e5a3f138be74f9f886ed8e760212ca6392746b63dc9a6537b00aeafdd6c",
    "db/models.py": "eb0593e46163bdeeedcd72bcf469cdbacdb50499b90e8e39db3393dfddfb9cd4",
    "oms/service.py": "21a2fd65e42c233c54f0f315e25bf7f628961499fce9e404f179cb1b393b9608",
    "services/schwab_1m_v2_bot.py": "c9fd1fe1df867ba9b9860eb04ffb16cb93ab7084060bb83fc9739f4217a53aa3",
    "settings.py": "62c5e70cca51bd4fc248995b5c52b333dd6f7875e24921142f6f8924d8a10550",
    "strategy_core/schwab_1m_v2.py": "407469c40b8d5f50282752e226a728ff0847cc8402e7f025d003ef15278b7026",
    "v2_flip_entry_ownership.py": "de5f9be92690a333d19ad15f0cf3248ee1419575731199a38c0dd69371c882b0",
    "v2_removed_wait.py": "db93c7fb8918a23202caf1bc9ea0dd9afe0cee27c80cac51f61861b4faab8198",
}.items()}
SCHEMA_PINS = {
    "sql/migrations/versions/20261023_atr_simulation_store.py": "185e4ee38f868b84fe031fc77232061e081b02dacf429bd4a0048d26aaaeea0b",
    "src/project_mai_tai/atr_simulation/store_schema_20261023.py": "c6e98ec1865b36f48d810d813dc36c6a335d579860d77549319e418d462bb2bc",
    "sql/migrations/versions/20261023_atr_aggregate_entry.py": "61beb90a5b16a4ffd6a25a66ba1b363a13648efa988d13281777f85dd02cca9a",
    "src/project_mai_tai/atr_simulation/store_schema_20261023_aggregate.py": "7a072363a7337a65c72f753ae9428907024f940c4b34c1bef3313a6f92aef828",
    "src/project_mai_tai/atr_simulation/models.py": "b0e7ce2b2f947ae343a78dfc992eea31658345047bb1561ee59fdfeda0c10c6a",
    "sql/migrations/versions/20261022_l12_dispatch_retirement.py": "f028854fcda1a520c0228683ef71e7fbab0b254fbc1a33b904dc34559c5b4d54",
}
SCHEMA_TABLES = frozenset(("atr_simulation_inputs", "atr_simulation_consumers", "atr_simulation_runs",
    "atr_simulation_rows", "brokerdecides_dispatch_evidence", "brokerdecides_dispatch_retirements"))
FORBIDDEN = (
    "src/project_mai_tai/strategy_core/keepwatch.py",
    "src/project_mai_tai/services/keepwatch_bar_owner.py",
    "src/project_mai_tai/services/keepwatch_halt_proof.py",
    "src/project_mai_tai/services/keepwatch_startup_veto.py",
    "src/project_mai_tai/market_data/keepwatch_history.py",
    "src/project_mai_tai/oms/broker_sync_generation.py",
    "src/project_mai_tai/oms/broker_sync_collection.py",
    "src/project_mai_tai/oms/broker_sync_end_marker.py",
    "src/project_mai_tai/brokerdecides1_startup_alert.py",
    "sql/migrations/versions/20261023_broker_sync_generation.py",
)


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _head(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("v10_duplicate_receipt_key")
        result[name] = value
    return result


def _receipt(raw, expected):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RECEIPT or not _sha(expected) or sha256(raw).hexdigest() != expected:
        raise ValueError("v10_independent_raw_receipt_hash_required")
    def reject(value):
        raise ValueError("v10_nonfinite_receipt")
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=reject)


def _path(root, name):
    if (not isinstance(name, str) or not name or name != PurePosixPath(name).as_posix()
            or PurePosixPath(name).is_absolute() or any(part in {".", ".."} for part in name.split("/"))):
        raise ValueError("v10_noncanonical_member")
    path = root
    for part in name.split("/"):
        path = path / part
        if path.is_symlink():
            raise ValueError("v10_symlink_member_or_parent")
    if not path.resolve().is_relative_to(root):
        raise ValueError("v10_member_escape")
    return path


def _pins(value):
    return isinstance(value, dict) and bool(value) and all(_sha(item) for item in value.values())


def _verify_tree(root, inventory):
    if not _pins(inventory) or len(inventory) > 20000:
        raise ValueError("v10_bounded_complete_source_inventory_required")
    names = set()
    for directory in SOURCE_DIRS:
        base = _path(root, directory)
        if not base.is_dir():
            raise ValueError("v10_source_directory_missing")
        for parent, directories, files in os.walk(base, followlinks=False):
            for name in directories:
                if (Path(parent) / name).is_symlink():
                    raise ValueError("v10_symlink_directory")
            directories[:] = [name for name in directories if name != "__pycache__"]
            for name in files:
                if name.endswith((".pyc", ".pyo")):
                    continue
                relative = (Path(parent) / name).relative_to(root).as_posix()
                names.add(relative)
                if len(names) > 20000:
                    raise ValueError("v10_source_inventory_overflow")
    if names != set(inventory):
        raise ValueError("v10_source_inventory_membership_changed")
    for name, expected in inventory.items():
        path = _path(root, name)
        if not path.is_file() or not 0 <= path.stat().st_size <= MAX_FILE:
            raise ValueError("v10_member_type_or_size")
        if sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("v10_source_bytes_changed")
    if any(_path(root, name).exists() for name in FORBIDDEN):
        raise ValueError("v10_forbidden_l11_dependency_present")


@dataclass(frozen=True)
class V10Guard:
    root: Path
    world_items: tuple
    inventory_items: tuple

    def __call__(self, world, method):
        if world != dict(self.world_items):
            return False
        function = getattr(method, "__func__", None)
        code = getattr(function, "__code__", None)
        if (code is None or getattr(function, "__module__", None) != MODULE
                or getattr(function, "__name__", None) != "_strict_first_rest_admitted"
                or Path(code.co_filename).resolve() != _path(self.root, CORE)):
            return False
        try:
            inventory = dict(self.inventory_items)
            _verify_tree(self.root, inventory)
            for name, module in tuple(sys.modules.items()):
                if not (name == "project_mai_tai" or name.startswith("project_mai_tai.")
                        or name == "tools" or name.startswith("tools.")):
                    continue
                prefix = self.root / ("tools" if name == "tools" or name.startswith("tools.") else "src/project_mai_tai")
                filename = getattr(module, "__file__", None)
                if filename is not None:
                    path = Path(filename)
                    if not path.is_absolute() or not path.resolve().is_relative_to(prefix):
                        return False
                    relative = path.relative_to(self.root).as_posix()
                    if relative not in inventory or _path(self.root, relative) != path:
                        return False
                for directory in getattr(module, "__path__", ()):
                    if not Path(directory).resolve().is_relative_to(prefix):
                        return False
        except (OSError, ValueError):
            return False
        return True


def bind_v10_guard(*, root, world, approval, observed_clean_receipt, observed_baseline_receipt, observed_schema):
    """NEW-only callback compatible with the existing original122 adapter.

    OLD continues to use the historical exact2e79 guard, never NEW flag-OFF.
    Returns source guard only; restoration/native/boot permissions are separate.
    """
    if (not isinstance(root, str) or not Path(root).is_absolute() or str(Path(root).resolve()) != root
            or not isinstance(world, dict) or set(world) != {"label", "head", "source_review_sha256"}
            or world["label"] != "NEW" or not _head(world["head"]) or world["head"] == OLD_HEAD
            or not _sha(world["source_review_sha256"])):
        raise ValueError("v10_exact_reviewed_NEW_world_and_canonical_root_required")
    fields = {"version", "root", "world", "components", "scope_review_sha256", "review_sha256",
        "clean_receipt_sha256", "baseline_receipt_sha256", "fixed_pins", "inventory_pins", "differences", "schema"}
    if (not isinstance(approval, dict) or set(approval) != fields or approval["version"] != VERSION
            or approval["root"] != root or approval["world"] != world
            or approval["review_sha256"] != world["source_review_sha256"] or not _sha(approval["scope_review_sha256"])
            or approval["components"] != {"app": APP_COMPONENT, "schema": SCHEMA_COMPONENT}):
        raise ValueError("v10_independent_component_and_scope_review_required")
    clean = _receipt(observed_clean_receipt, approval["clean_receipt_sha256"])
    baseline = _receipt(observed_baseline_receipt, approval["baseline_receipt_sha256"])
    if (not isinstance(clean, dict) or set(clean) != {"head", "root", "clean", "fixed_pins", "inventory_pins"}
            or clean != dict(head=world["head"], root=root, clean=True,
                fixed_pins=approval["fixed_pins"], inventory_pins=approval["inventory_pins"])
            or clean["clean"] is not True):
        raise ValueError("v10_actual_execution_root_and_clean_inventory_receipt_required")
    pins, inventory = approval["fixed_pins"], approval["inventory_pins"]
    if not _pins(pins) or set(pins) != MEMBERS or not _pins(inventory) or any(inventory.get(name) != value for name, value in pins.items()):
        raise ValueError("v10_literal_fixed_and_full_source_membership_required")
    if (not isinstance(baseline, dict) or set(baseline) != {"head", "source_pins"} or baseline["head"] != OLD_HEAD
            or not isinstance(baseline["source_pins"], dict) or set(baseline["source_pins"]) != MEMBERS):
        raise ValueError("v10_actual_baseline_addition_observations_required")
    before = baseline["source_pins"]
    if (any(before[name] != value for name, value in V9_BEFORE.items())
            or any(value != "ABSENT_AT_2E79" and not _sha(value) for value in before.values())):
        raise ValueError("v10_historical_baseline_not_refreshable")
    changed = {name for name in MEMBERS if before[name] != pins[name]}
    differences = approval["differences"]
    if not isinstance(differences, dict) or set(differences) != changed:
        raise ValueError("v10_every_difference_review_required")
    for name in changed:
        item = differences[name]
        if (not isinstance(item, dict) or set(item) != {"before", "after", "reason", "review_sha256"}
                or item["before"] != before[name] or item["after"] != pins[name]
                or not _sha(item["review_sha256"]) or not isinstance(item["reason"], str) or not item["reason"].strip()):
            raise ValueError("v10_source_difference_review_unbound")
    if any(pins.get(name) != value for name, value in {**SCHEMA_PINS, **APP_PINS}.items()):
        raise ValueError("v10_exact_component_bytes_aggregate_child_catalog_and_parent_required")
    schema_fields = {"inventory_sha256", "construction_review_sha256", "canonical_schema_review_sha256",
        "schema_policy_sha256", "source_table_states", "generation_table"}
    if (not isinstance(observed_schema, dict) or set(observed_schema) != schema_fields
            or approval["schema"] != observed_schema
            or any(not _sha(observed_schema[name]) for name in schema_fields - {"source_table_states", "generation_table"})
            or observed_schema["schema_policy_sha256"] != pins["tools/l12_private_schema_evidence.py"]
            or observed_schema["generation_table"] != "ABSENT_NOT_REQUIRED_NO_FRESHNESS_INFERENCE"
            or not isinstance(observed_schema["source_table_states"], dict)
            or set(observed_schema["source_table_states"]) != SCHEMA_TABLES
            or any(value not in {"ABSENT_REQUIRES_PRIVATE_CONSTRUCTION", "PRESENT_REQUIRES_FULL_MEMBERSHIP"}
                   for value in observed_schema["source_table_states"].values())):
        raise ValueError("v10_all_six_source_states_and_canonical_construction_reviews_required")
    resolved = Path(root)
    _verify_tree(resolved, inventory)
    return V10Guard(resolved, tuple(sorted(world.items())), tuple(sorted(inventory.items())))
