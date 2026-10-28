"""Fresh Unix-only PG16 SIM-store witness, not a baseline or real-close boot.

Runs the exact unapplied four-table migration body without stamping a fictional
installed baseline. Main owns lifecycle; successful stores remain for follow-up
publication/read checks. Never connects to an existing cluster or provider.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import psycopg
from psycopg import sql
import sqlalchemy as sa
from sqlalchemy.engine import URL


BIN = Path("/opt/homebrew/opt/postgresql@16/bin")
SOURCE_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = SOURCE_ROOT / "sql/migrations/versions/20261023_atr_simulation_store.py"
MIGRATION_SHA = "185e4ee38f868b84fe031fc77232061e081b02dacf429bd4a0048d26aaaeea0b"
TABLES = {"atr_simulation_inputs", "atr_simulation_consumers",
          "atr_simulation_runs", "atr_simulation_rows"}
PORT = 55537


def postgres_environment():
    # Homebrew PG on macOS must not discover the locale through threaded APIs.
    return {**os.environ, "LC_ALL": "C", "LANG": "C"}


def exclusive_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, sort_keys=True, default=str, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def finalize_owned_receipt(root, data, receipt, *, primary_error=None):
    try:
        exclusive_json(root / "creation-receipt.json", receipt)
    except BaseException as error:
        if (data / "postmaster.pid").exists():
            try:
                result = subprocess.run([str(BIN / "pg_ctl"), "-D", str(data), "-w", "-m", "fast", "stop"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=postgres_environment())
                if result.returncode != 0:
                    error.add_note("owned cleanup stop exit=" + str(result.returncode)
                        + "; root=" + str(root))
            except BaseException as cleanup_error:
                error.add_note("owned cleanup failed: " + type(cleanup_error).__name__)
        if primary_error is None:
            raise
        primary_error.add_note("creation receipt finalization failed: " + type(error).__name__)
        for note in getattr(error, "__notes__", ()):
            primary_error.add_note(note)


def create_private_store():
    if any(key.startswith("PG") for key in os.environ):
        raise ValueError("ambient_libpq_environment_refused")
    if hashlib.sha256(MIGRATION.read_bytes()).hexdigest() != MIGRATION_SHA:
        raise ValueError("reviewed_migration_source_changed")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE_ROOT, text=True).strip()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=SOURCE_ROOT, text=True).strip():
        raise ValueError("committed_clean_source_required")
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix="fbhpg-", dir="/private/tmp"))
    os.chmod(root, 0o700)
    data = root / "data"
    nonce = uuid.uuid4().hex[:16]
    admin, database = "fbh_admin_" + nonce, "fbh_store_" + nonce
    writer, reader = "fbh_writer_" + nonce, "fbh_reader_" + nonce
    receipt = {"status": "STARTED", "root": str(root), "nonce": nonce,
        "source_head": head, "migration_sha256": MIGRATION_SHA,
        "migration_scope": "EXACT_MIGRATION_BODY_ONLY_NO_BASELINE_STAMP_OR_BOOT_PROOF",
        "database": database, "admin": admin, "writer": writer, "reader": reader,
        "port": PORT, "provider_connections": 0, "production_actions": 0}
    receipt["postgres_environment"] = {"LC_ALL": "C", "LANG": "C"}
    print(json.dumps({"root": str(root), "nonce": nonce, "status": "OWNED_ROOT_RESERVED"}), flush=True)
    started = False
    primary_error = None
    try:
        with (root / "setup.log").open("xb") as log:
            subprocess.run([str(BIN / "initdb"), "-D", str(data), "-U", admin,
                "--auth=trust", "--no-locale", "--encoding=UTF8"], check=True,
                stdout=log, stderr=log, env=postgres_environment())
            subprocess.run([str(BIN / "pg_ctl"), "-D", str(data), "-l", str(root / "server.log"),
                "-w", "-o", f"-k {root} -p {PORT} -c listen_addresses='' "
                "-c unix_socket_permissions=0700 -c max_connections=12", "start"],
                check=True, stdout=log, stderr=log, env=postgres_environment())
            started = True
        with psycopg.connect(host=str(root), port=PORT, dbname="postgres", user=admin,
                connect_timeout=3, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
                sql.Identifier(database), sql.Identifier(admin)))
        url = URL.create("postgresql+psycopg", username=admin, database=database,
            query={"host": str(root), "port": str(PORT)})
        engine = sa.create_engine(url, poolclass=sa.pool.NullPool, connect_args={"connect_timeout": 3})
        try:
            with engine.begin() as connection:
                identity = connection.exec_driver_sql("SELECT current_database(),current_user,"
                    "current_setting('data_directory'),current_setting('listen_addresses'),inet_server_addr(),"
                    "(pg_control_system()).system_identifier::text,pg_postmaster_start_time(),pg_backend_pid()"
                ).one()
                if tuple(identity[:5]) != (database, admin, str(data), "", None):
                    raise ValueError("fresh_private_cluster_identity_mismatch")
                receipt.update(system_id=identity[5], postmaster_started=identity[6], setup_backend=identity[7],
                    postmaster_pid=int((data / "postmaster.pid").read_text().splitlines()[0]))
                connection.exec_driver_sql("CREATE TABLE public.non_sim_sentinel (key text PRIMARY KEY,value text NOT NULL)")
                connection.exec_driver_sql("INSERT INTO public.non_sim_sentinel VALUES ('unchanged','store-witness-only')")
                spec = importlib.util.spec_from_file_location("_firstbar_exact_migration", MIGRATION)
                migration = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(migration)
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                names = set(connection.exec_driver_sql("SELECT tablename FROM pg_tables WHERE schemaname='public'").scalars())
                if names != TABLES | {"non_sim_sentinel"}:
                    raise ValueError("unexpected_private_schema_membership")
                connection.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                quoted_db = connection.dialect.identifier_preparer.quote(database)
                connection.exec_driver_sql(f"REVOKE ALL ON DATABASE {quoted_db} FROM PUBLIC")
                for role in (writer, reader):
                    quoted = connection.dialect.identifier_preparer.quote(role)
                    connection.exec_driver_sql(f"CREATE ROLE {quoted} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS")
                    connection.exec_driver_sql(f"GRANT CONNECT ON DATABASE {quoted_db} TO {quoted}")
                    connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {quoted}")
                    connection.exec_driver_sql(f"ALTER ROLE {quoted} SET statement_timeout='750ms'")
                    connection.exec_driver_sql(f"ALTER ROLE {quoted} SET lock_timeout='250ms'")
                    for table in sorted(TABLES):
                        privileges = "SELECT, INSERT, UPDATE" if role == writer else "SELECT"
                        connection.exec_driver_sql(f"GRANT {privileges} ON TABLE public.{table} TO {quoted}")
                    if role == reader:
                        connection.exec_driver_sql(f"ALTER ROLE {quoted} SET default_transaction_read_only=on")
                receipt["tables"] = sorted(names)
        finally:
            engine.dispose()
        for role, kind in ((writer, "writer"), (reader, "reader")):
            role_url = URL.create("postgresql+psycopg", username=role, database=database,
                query={"host": str(root), "port": str(PORT)})
            path = root / (kind + ".dsn")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(role_url.render_as_string(hide_password=False) + "\n")
        receipt["status"] = "OWNED_STORE_READY_ROLE_ENFORCEMENT_AND_PUBLICATION_UNRUN"
        receipt["server_running"] = True
    except BaseException as error:
        primary_error = error
        receipt.update(status="SETUP_FAILED", error_type=type(error).__name__)
        if started or (data / "postmaster.pid").exists():
            result = subprocess.run([str(BIN / "pg_ctl"), "-D", str(data), "-w", "-m", "fast", "stop"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=postgres_environment())
            receipt["failure_cleanup_stop_exit"] = result.returncode
            receipt["postmaster_pidfile_after_cleanup"] = (data / "postmaster.pid").exists()
            if result.returncode != 0:
                error.add_note("owned cleanup stop exit=" + str(result.returncode) + "; root=" + str(root))
        raise
    finally:
        finalize_owned_receipt(root, data, receipt, primary_error=primary_error)
        print(json.dumps({"root": str(root), "status": receipt["status"]}))


if __name__ == "__main__":
    create_private_store()
