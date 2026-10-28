"""Exact Main nonce capability. No legacy-guard change or permissive URL prefix.

The reviewed raw creation artifact binds all target operands. URL checking is
not connected-target proof; every actual session must also verify its identity.
No engine, schema, process, service or permission is constructed here.
"""

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

VERSION = "L12_MAIN_NONCE_PG16_TARGET_V1"
EXECUTION_BINDING_VERSION = "L12_C1_1409_TOOLS_ONLY_COPY_CONSUMER_V1"
IDENTITY = text("SELECT current_database(), current_user, current_setting('data_directory'), "
    "current_setting('port'), inet_server_addr(), current_setting('listen_addresses'), "
    "(pg_control_system()).system_identifier::text, pg_postmaster_start_time()::text, "
    "current_setting('server_version_num')")


def validate_execution_binding(binding, target, *, inventory=None):
    """Separate consumer identity; never rewrite an original target capability."""
    fields = {"version", "target_capability_sha256", "creation_source_head", "consumer_source_head",
        "consumer_root", "consumer_inventory_sha256", "review_sha256"}
    if (type(target) is not MainNonceTarget or not isinstance(binding, dict) or set(binding) != fields
            or binding["version"] != EXECUTION_BINDING_VERSION
            or binding["target_capability_sha256"] != target.reviewed_sha256
            or binding["creation_source_head"] != target.values["source_head"]
            or not isinstance(binding["consumer_source_head"], str)
            or not re.fullmatch("[0-9a-f]{40}", binding["consumer_source_head"])
            or not isinstance(binding["consumer_root"], str) or not Path(binding["consumer_root"]).is_absolute()
            or str(Path(binding["consumer_root"]).resolve()) != binding["consumer_root"]
            or any(not isinstance(binding[key], str) or not re.fullmatch("[0-9a-f]{64}", binding[key])
                or binding[key] == "0" * 64 for key in ("consumer_inventory_sha256", "review_sha256"))):
        raise ValueError("exact_C1_tools_only_consumer_binding_no_target_retag_required")
    if inventory is not None:
        raw = json.dumps(inventory, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if sha256(raw).hexdigest() != binding["consumer_inventory_sha256"]:
            raise ValueError("actual_consumer_inventory_differs_from_reviewed_binding")
    return binding["consumer_source_head"]


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("nonce_target_duplicate_key")
        result[key] = value
    return result


def layout(nonce):
    if not isinstance(nonce, str) or not re.fullmatch("[0-9a-f]{32}", nonce):
        raise ValueError("explicit_Main_nonce_required")
    root = "/private/tmp/oct24-l12-pg-" + nonce
    return dict(root=root, data_directory=root + "/data", socket=root + "/socket",
                database="oct24_l12_" + nonce, port="55521")


@dataclass(frozen=True)
class MainNonceTarget:
    raw: bytes
    reviewed_sha256: str
    expected_creation_review_sha256: str
    values: object = field(init=False, repr=False)

    def __post_init__(self):
        if (type(self.raw) is not bytes or not 0 < len(self.raw) <= 16384
                or sha256(self.raw).hexdigest() != self.reviewed_sha256
                or not isinstance(self.expected_creation_review_sha256, str)
                or not re.fullmatch("[0-9a-f]{64}", self.expected_creation_review_sha256)
                or self.expected_creation_review_sha256 == "0" * 64):
            raise ValueError("actual_reviewed_nonce_creation_artifact_required")
        value = json.loads(self.raw, object_pairs_hook=unique)
        fields = {"version", "nonce", "root", "data_directory", "socket", "database", "port", "uid",
            "database_user", "system_identifier", "postmaster_pid", "postmaster_started",
            "server_version_num", "source_head", "creation_review_sha256", "creation_journal_tail_sha256",
            "at_creation_schema_state"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("exact_nonce_target_capability_membership_required")
        expected = layout(value["nonce"])
        if (value["version"] != VERSION or any(value[key] != item for key, item in expected.items())
                or type(value["uid"]) is not int or value["uid"] != os.getuid()
                or not isinstance(value["database_user"], str)
                or not re.fullmatch("[a-z_][a-z0-9_]{0,62}", value["database_user"])
                or not isinstance(value["system_identifier"], str) or not value["system_identifier"].isdecimal()
                or int(value["system_identifier"]) <= 0 or type(value["postmaster_pid"]) is not int
                or value["postmaster_pid"] <= 1 or not isinstance(value["postmaster_started"], str)
                or not value["postmaster_started"] or not isinstance(value["server_version_num"], str)
                or not value["server_version_num"].isdecimal()
                or not 160000 <= int(value["server_version_num"]) < 170000
                or not isinstance(value["source_head"], str) or not re.fullmatch("[0-9a-f]{40}", value["source_head"])
                or value["creation_review_sha256"] != self.expected_creation_review_sha256
                or not isinstance(value["creation_journal_tail_sha256"], str)
                or not re.fullmatch("[0-9a-f]{64}", value["creation_journal_tail_sha256"])
                or value["creation_journal_tail_sha256"] == "0" * 64
                or value["at_creation_schema_state"] != "CATALOG_VERIFIED_ZERO_USER_RELATIONS_NO_COPY_OR_BOOT"):
            raise ValueError("nonce_target_operand_or_creation_binding_mismatch")
        object.__setattr__(self, "values", MappingProxyType(value))

    def require_factory(self, factory):
        engine = getattr(factory, "kw", {}).get("bind")
        value = self.values
        expected = URL.create("postgresql+psycopg", username=value["database_user"],
            database=value["database"], query={"host": value["socket"], "port": value["port"]})
        if engine is None or engine.url != expected or engine.echo:
            raise ValueError("exact_nonce_capability_URL_no_options_password_TCP_or_echo_required")
        if not isinstance(engine.pool, NullPool):
            raise ValueError("nonce_offline_factory_requires_NullPool_no_hidden_idle_clients")

    def verify_session(self, session):
        value = self.values
        if session.get_bind().dialect.name != "postgresql":
            raise ValueError("nonce_capability_requires_actual_postgresql")
        session.execute(text("SET LOCAL statement_timeout = '750ms'"))
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        expected = (value["database"], value["database_user"], value["data_directory"], value["port"],
            None, "", value["system_identifier"], value["postmaster_started"], value["server_version_num"])
        if tuple(session.execute(IDENTITY).one()) != expected:
            raise ValueError("actual_nonce_target_epoch_role_or_noTCP_identity_changed")
        for name in ("root", "data_directory", "socket"):
            path = Path(value[name])
            if path.is_symlink() or path.resolve() != path:
                raise ValueError("nonce_target_directory_changed")
            info = path.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != value["uid"] or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("nonce_target_owned_private_directory_required")
        path = Path(value["data_directory"]) / "postmaster.pid"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != value["uid"]
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or not 0 < info.st_size <= 8192):
                raise ValueError("nonce_target_postmaster_PID_file_identity_required")
            fields = os.read(fd, 8193).decode().splitlines()
        finally:
            os.close(fd)
        if (len(fields) < 5 or fields[0] != str(value["postmaster_pid"])
                or fields[1] != value["data_directory"] or fields[3] != value["port"]
                or fields[4] != value["socket"]):
            raise ValueError("actual_nonce_postmaster_PID_or_socket_changed")


def load_target_file(path, *, reviewed_sha256, expected_creation_review_sha256):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise ValueError("canonical_nonce_capability_artifact_required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= 16384):
            raise ValueError("owned_0600_bounded_nonce_capability_artifact_required")
        raw = os.read(fd, 16385)
    finally:
        os.close(fd)
    return MainNonceTarget(raw, reviewed_sha256, expected_creation_review_sha256)
