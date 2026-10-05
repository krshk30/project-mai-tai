"""Diagnose the test connection rollback and repeat the full R-T5 fill path.

Run from the RPG checkout with --source pointing to a read-only composition.
Only test modules are replaced in memory; production modules are never edited.
"""
import argparse
import importlib
from collections import deque
from pathlib import Path
import sys
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread, get_ident

parser = argparse.ArgumentParser()
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--mode", choices=("static", "file"), required=True)
parser.add_argument("--repeat", type=int, default=50)
parser.add_argument("--child", action="store_true")
args = parser.parse_args()
sys.path.insert(0, str(Path.cwd()))
sys.path.insert(0, str(args.source / "src"))

import pytest  # noqa: E402
from sqlalchemy import create_engine, event, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402
from project_mai_tai.db.models import Base  # noqa: E402

runtime = importlib.import_module("tests.unit.test_rpg1_runtime")
original_factory = importlib.import_module("tests.unit.test_oms_webull_mirror_deferred_resubmit")._session_factory
directories = []


def file_factory():
    directory = TemporaryDirectory(prefix="rpg-runtime-isolation-")
    directories.append(directory)
    engine = create_engine("sqlite+pysqlite:///" + str(Path(directory.name) / "runtime.sqlite"),
        connect_args={"check_same_thread": False}, poolclass=NullPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


factory = original_factory() if args.mode == "static" else file_factory()
engine = factory.kw["bind"]
with engine.begin() as connection:
    connection.execute(text("CREATE TABLE isolation_probe (value INTEGER NOT NULL)"))
    connection.execute(text("INSERT INTO isolation_probe VALUES (0)"))

with factory() as writer:
    writer.execute(text("UPDATE isolation_probe SET value=1"))
    writer_connection = writer.connection().connection.driver_connection
    observed = {}

    def read_and_close():
        with factory() as reader:
            observed["same_connection"] = reader.connection().connection.driver_connection is writer_connection
            observed["value"] = reader.scalar(text("SELECT value FROM isolation_probe"))

    reader = Thread(target=read_and_close)
    reader.start()
    reader.join()
    writer.commit()
with factory() as reader:
    persisted = reader.scalar(text("SELECT value FROM isolation_probe"))
print(f"mode={args.mode} same_connection={observed['same_connection']} "
      f"reader_value={observed['value']} writer_committed_value={persisted}", flush=True)
assert persisted == (0 if args.mode == "static" else 1)

if args.child:
    trace = deque(maxlen=500)
    chosen_factory = original_factory if args.mode == "static" else file_factory

    def traced_factory():
        result = chosen_factory()
        engine = result.kw["bind"]

        @event.listens_for(engine, "before_cursor_execute")
        def statement(connection, cursor, sql, parameters, context, many):
            trace.append((get_ident(), id(connection.connection.driver_connection), sql, str(parameters)[:600]))

        for operation in ("commit", "rollback"):
            def transaction(connection, operation=operation):
                trace.append((get_ident(), id(connection.connection.driver_connection), operation))
            event.listen(engine, operation, transaction)
        return result

    runtime._session_factory = traced_factory
    print(f"production_source={importlib.import_module('project_mai_tai.oms.service').__file__}", flush=True)
    result = pytest.main(["-q", "-p", "no:cacheprovider",
        "tests/unit/test_rpgstuck1.py::test_r_t5_uncertain_webull_dispatch_reconciles_exact_client_without_resubmit[fills]",
        "--tb=short"])
    if result:
        print("SQL transaction trace (thread, physical connection, operation):", flush=True)
        for entry in trace:
            print(entry, flush=True)
    raise SystemExit(result)
failed = 0
for attempt in range(args.repeat):
    run = subprocess.run([sys.executable, "-B", __file__, "--source", str(args.source),
        "--mode", args.mode, "--child"], capture_output=True, text=True)
    print(f"attempt={attempt + 1} rc={run.returncode}", flush=True)
    if run.returncode:
        failed += 1
        print(run.stdout, run.stderr, flush=True)
print(f"mode={args.mode} passed={args.repeat - failed} failed={failed}", flush=True)
raise SystemExit(bool(failed))
