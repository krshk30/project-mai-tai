"""One Main-owned private child: REAL env.py stamp + upgrade, exact nonce only.

No work on import. No custom MigrationContext/Operations or metadata.create_all.
The source review, capability and private workdir must already be Main-owned.
"""

import argparse
from dataclasses import dataclass
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

SOURCE_PREFIXES = ("src", "sql", "alembic.ini")
TOOL_FILES = (
    "tools/l12_real_alembic_child.py", "tools/l12_schema_rehearsal_inputs.py",
    "tools/l12_production_schema_rehearsal.py", "tools/l12_main_nonce_target.py",
    "tools/l12_nonce_copy_lease.py", "tools/l12_private_evidence_journal.py",
    "tools/l12_private_full23_lifecycle.py",
    "tools/l12_create_private55521.py", "tools/l12_close_asof_contract.py",
    "tools/l12_scoped_chunks_v2.py", "tools/l12_scoped_restore_v2.py",
    "tools/l12_service_boot_replay.py", "tools/l12_close_export_restore.py",
    "tools/l12_required_copy_scope.py", "tools/l12_today_close_replay.py",
    "tools/l12_private_schema_evidence.py",
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate_real_env_input_STOP")
        result[name] = value
    return result


def read_artifact(item, *, limit=16 * 1024**2):
    if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
        raise ValueError("exact_Main_artifact_descriptor_required")
    path = Path(item["path"])
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise ValueError("canonical_Main_artifact_required")
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= limit):
        raise ValueError("owned_regular0600_Main_artifact_required")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != item["sha256"]:
        raise ValueError("actual_Main_artifact_hash_changed")
    return raw


@dataclass(frozen=True)
class SourceReview:
    raw: bytes
    sha256: str

    def __post_init__(self):
        if type(self.raw) is not bytes or not 0 < len(self.raw) <= 16 * 1024**2 or sha256(self.raw).hexdigest() != self.sha256:
            raise ValueError("immutable_actual_composed_source_review_required")
        value = json.loads(self.raw, object_pairs_hook=unique)
        if (set(value) != {"purpose", "source_root", "source_head", "source_pins", "authority_sha256", "review_id"}
                or value["purpose"] != "MAIN_REVIEWED_REAL_ENV_WHOLE_PRODUCTION_SCHEMA_REHEARSAL"
                or not isinstance(value["source_head"], str) or not re.fullmatch(r"[0-9a-f]{40}", value["source_head"])
                or not isinstance(value["source_pins"], dict) or not 10 <= len(value["source_pins"]) <= 5000
                or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) or item == "0" * 64
                    for item in [value["authority_sha256"], *value["source_pins"].values()])
                or not isinstance(value["review_id"], str) or not value["review_id"].strip()):
            raise ValueError("exact_reviewed_real_env_source_membership_required")
        root = Path(value["source_root"])
        if not root.is_absolute() or root.resolve() != root or root.is_symlink():
            raise ValueError("canonical_composed_Main_root_required")

    @property
    def values(self):
        return json.loads(self.raw, object_pairs_hook=unique)

    def argv(self, *args):
        return ["git", "-C", self.values["source_root"], *args]

    def verify(self):
        value, total = self.values, 0
        root = Path(value["source_root"])
        def git(*args):
            return subprocess.check_output(self.argv(*args), timeout=10, stderr=subprocess.DEVNULL)
        if git("rev-parse", "HEAD").decode().strip() != value["source_head"] or git("status", "--porcelain").strip():
            raise ValueError("actual_reviewed_clean_source_head_changed")
        files = git("ls-files", "-z", "--", *SOURCE_PREFIXES, *TOOL_FILES).decode().split("\x00")
        expected = sorted(name for name in files if name)
        if sorted(value["source_pins"]) != expected or not set(TOOL_FILES) <= set(expected):
            raise ValueError("full_APP_sql_real_env_and_helper_closure_required")
        for name in expected:
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("source_inventory_path_escape_STOP")
            path = root
            for part in Path(name).parts:
                path = path / part
                if path.is_symlink():
                    raise ValueError("source_inventory_symlink_STOP")
            size = path.stat().st_size
            total += size
            if size > 32 * 1024**2 or total > 512 * 1024**2 or sha256(path.read_bytes()).hexdigest() != value["source_pins"][name]:
                raise ValueError("bounded_full_source_bytes_changed_STOP")
        return True

    def verify_loaded_app(self):
        root = Path(self.values["source_root"]) / "src"
        for name, module in list(sys.modules.items()):
            if name == "project_mai_tai" or name.startswith("project_mai_tai."):
                origin = getattr(module, "__file__", None)
                if origin is None or not Path(origin).resolve().is_relative_to(root):
                    raise ValueError("loaded_APP_origin_not_reviewed_current_copy_root")
        return True


def private_environment(target):
    """Ephemeral child only. No .env, installed config, service or provider IO."""
    value = target.values
    from sqlalchemy.engine import URL
    url = URL.create("postgresql+psycopg", username=value["database_user"], database=value["database"],
        query={"host": value["socket"], "port": value["port"]})
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
    env.update(MAI_TAI_DATABASE_URL=url.render_as_string(hide_password=False), PYTHONDONTWRITEBYTECODE="1")
    return env


class ChildFence:
    def __init__(self, target, source):
        from sqlalchemy.engine import URL
        self.target, self.source, self.connections = target, source, 0
        value = target.values
        self.expected = URL.create("postgresql+psycopg", username=value["database_user"], database=value["database"],
            query={"host": value["socket"], "port": value["port"]})

    def audit(self, event, args):
        if event == "socket.connect":
            if args[1] != self.target.values["socket"] + "/.s.PGSQL.55521":
                raise PermissionError("real_env_only_exact_private_Unix_socket_allowed")
        elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
                "os.system", "os.posix_spawn", "os.posix_spawnp", "os.fork", "os.forkpty"}:
            raise PermissionError("real_env_no_provider_DNS_shell_or_child")
        elif event == "subprocess.Popen":
            permitted = [self.source.argv("rev-parse", "HEAD"), self.source.argv("status", "--porcelain"),
                self.source.argv("ls-files", "-z", "--", *SOURCE_PREFIXES, *TOOL_FILES)]
            if not isinstance(args[1], (list, tuple)) or list(args[1]) not in permitted:
                raise PermissionError("real_env_only_fixed_source_guard_git_allowed")

    def do_connect(self, dialect, record, cargs, cparams):
        expected_args, expected_params = dialect.create_connect_args(self.expected)
        if dialect.name != "postgresql" or dialect.driver != "psycopg" or list(cargs) != list(expected_args) or cparams != expected_params:
            raise PermissionError("real_Alembic_cannot_connect_foreign_or_default_database")
        if self.connections >= 2:
            raise PermissionError("real_Alembic_stamp_plus_upgrade_two_connections_only_no_retry")
        self.connections += 1

    def engine_connect(self, connection):
        from sqlalchemy import text
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import NullPool
        if connection.engine.url != self.expected or not isinstance(connection.engine.pool, NullPool) or connection.engine.echo:
            raise PermissionError("actual_env_engine_URL_NullPool_noecho_required")
        session = Session(bind=connection)
        primary = None
        try:
            self.target.verify_session(session)
            others = session.scalar(text("SELECT count(*) FROM pg_catalog.pg_stat_activity "
                "WHERE backend_type='client backend' AND pid<>pg_backend_pid()"))
            if others != 0 or type(others) is not int:
                raise PermissionError("real_env_private_other_client_present_STOP")
            session.rollback()
            # SET SESSION survives each real env transaction and is set on EACH
            # separately opened stamp/upgrade connection, not borrowed from parent.
            for setting in ("statement_timeout='750ms'", "lock_timeout='250ms'", "idle_in_transaction_session_timeout='15s'"):
                connection.exec_driver_sql("SET SESSION " + setting)
            connection.commit()
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                session.close()
            except BaseException as cleanup:
                if primary is None:
                    raise
                primary.add_note("real_env_identity_session_cleanup_type=" + type(cleanup).__name__)


def run(plan):
    # Parent's typed target construction and child re-validation are independent.
    review_raw = read_artifact(plan["source_review"])
    review = SourceReview(review_raw, plan["source_review"]["sha256"])
    review.verify()
    root = Path(review.values["source_root"])
    for path in (root / "src", root):
        sys.path.insert(0, str(path))
    from tools.l12_main_nonce_target import MainNonceTarget
    from tools.l12_schema_rehearsal_inputs import START, END
    raw = read_artifact(plan["capability"], limit=16384)
    target = MainNonceTarget(raw, plan["capability"]["sha256"], plan["creation_review_sha256"])
    if Path(__file__).resolve() != root / "tools/l12_real_alembic_child.py":
        raise ValueError("actual_real_env_child_origin_must_match_reviewed_source_root")
    if target.values["source_head"] != review.values["source_head"]:
        raise ValueError("immutable_creation_head_must_match_real_upgrade_source")
    work = Path(target.values["root"]) / "rehearsal-work"
    info = work.stat()
    if (Path.cwd().resolve() != work or work.is_symlink() or work.resolve() != work
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700 or (work / ".env").exists()):
        raise ValueError("isolated_private_workdir_without_dotenv_required")
    fence = ChildFence(target, review)
    sys.addaudithook(fence.audit)
    from sqlalchemy import event
    from sqlalchemy.engine import Engine, make_url
    event.listen(Engine, "do_connect", fence.do_connect)
    event.listen(Engine, "engine_connect", fence.engine_connect)
    from project_mai_tai.settings import get_settings, Settings
    if Path(inspect.getfile(Settings)).resolve() != root / "src/project_mai_tai/settings.py" or make_url(get_settings().database_url) != fence.expected:
        raise ValueError("actual_real_env_Settings_override_must_resolve_exact_nonce")
    from alembic.config import Config
    from alembic import command
    from alembic.script import ScriptDirectory
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "sql/migrations"))
    script = ScriptDirectory.from_config(cfg)
    if script.get_heads() != [END]:
        raise ValueError("exact_one_real_Alembic_head_required")
    expected = [("20261022_l12_retirement", "20261023_atr_aggregate"),
        ("20261023_atr_aggregate", "20261023_atr_simulation"), ("20261023_atr_simulation", START)]
    if [(item.revision, item.down_revision) for item in script.iterate_revisions(END, START)] != expected:
        raise ValueError("literal_real_three_migration_chain_required")
    # This executes ACTUAL env.py twice with ACTUAL Base.metadata. It is not a
    # custom MigrationContext or direct invocation of module.upgrade bodies.
    command.stamp(cfg, START)
    print(canonical(dict(kind="REAL_ENV_STAMP_RETURNED_NOT_FINAL_OUTCOME", revision=START)).decode(), flush=True)
    command.upgrade(cfg, END)
    review.verify_loaded_app()
    review.verify()
    print(canonical(dict(kind="REAL_ENV_UPGRADE_RETURNED", revision=END, connections=fence.connections,
        target_capability_sha256=target.reviewed_sha256, source_review_sha256=review.sha256,
        permission_granted=False, canonical_approval=False)).decode(), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--plan-sha256", required=True)
    args = parser.parse_args()
    plan = json.loads(read_artifact(dict(path=args.plan, sha256=args.plan_sha256), limit=65536), object_pairs_hook=unique)
    if set(plan) != {"source_review", "capability", "creation_review_sha256"}:
        raise ValueError("exact_real_Alembic_child_plan_required")
    run(plan)


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        print(canonical(dict(kind="REAL_ENV_FAILED_OR_UNKNOWN_NO_RETRY", error_type=type(error).__name__)).decode(), file=sys.stderr)
        raise SystemExit(2) from None
