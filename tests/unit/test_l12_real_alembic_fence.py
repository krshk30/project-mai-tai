"""Source/connection/failure guards only, no database or child process."""

from hashlib import sha256
import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import URL

from tools import l12_real_alembic_child as child
from tools import l12_production_schema_rehearsal as parent


def target():
    return SimpleNamespace(values=dict(database_user="controlled", database="oct24_l12_" + "a" * 32,
        socket="/private/tmp/oct24-l12-pg-" + "a" * 32 + "/socket", port="55521"))


def source():
    return SimpleNamespace(argv=lambda *args: ["git", "-C", "/CONTROLLED", *args])


def test_child_database_settings_are_exact_private_URL_and_no_inherited_service_config(monkeypatch):
    monkeypatch.setenv("MAI_TAI_DATABASE_URL", "postgresql://FOREIGN")
    monkeypatch.setenv("DATABASE_URL", "postgresql://FOREIGN")
    monkeypatch.setenv("PGHOST", "FOREIGN")
    monkeypatch.setenv("PGOPTIONS", "FOREIGN")
    env = child.private_environment(target())
    assert set(env) <= {"PATH", "LANG", "LC_ALL", "MAI_TAI_DATABASE_URL", "PYTHONDONTWRITEBYTECODE"}
    assert env["MAI_TAI_DATABASE_URL"].startswith("postgresql+psycopg://controlled@/oct24_l12_")
    assert "55521" in env["MAI_TAI_DATABASE_URL"] and "FOREIGN" not in json.dumps(env)


@pytest.mark.parametrize("event,args", [
    ("socket.connect", (None, ("localhost", 5432))),
    ("socket.connect", (None, "/tmp/.s.PGSQL.55521")),
    ("socket.getaddrinfo", ("provider.example",)),
    ("os.system", ("forbidden",)),
    ("os.fork", ()),
    ("subprocess.Popen", ("sh", ["sh", "-c", "forbidden"], None, None)),
])
def test_child_fence_rejects_other_socket_provider_shell_or_child(event, args):
    fence = child.ChildFence(target(), source())
    with pytest.raises(PermissionError):
        fence.audit(event, args)


def test_child_fence_allows_only_exact_nonce_socket_and_fixed_source_git():
    value = target()
    fence = child.ChildFence(value, source())
    fence.audit("socket.connect", (None, value.values["socket"] + "/.s.PGSQL.55521"))
    fence.audit("subprocess.Popen", ("git", ["git", "-C", "/CONTROLLED", "rev-parse", "HEAD"], None, None))


@pytest.mark.parametrize("fault", ["foreign_args", "foreign_options", "driver", "third_connection"])
def test_real_env_do_connect_rejects_foreign_override_and_retry(fault):
    fence = child.ChildFence(target(), source())
    dialect = SimpleNamespace(name="postgresql", driver="psycopg",
        create_connect_args=lambda expected: ([], {"host": target().values["socket"], "port": "55521"}))
    args, params = dialect.create_connect_args(fence.expected)
    if fault == "foreign_args":
        args = ["FOREIGN"]
    if fault == "foreign_options":
        params = dict(params, options="FOREIGN")
    if fault == "driver":
        dialect.driver = "other"
    if fault == "third_connection":
        fence.connections = 2
    with pytest.raises(PermissionError):
        fence.do_connect(dialect, None, args, params)


def test_real_env_exact_stamp_upgrade_two_connections_only():
    fence = child.ChildFence(target(), source())
    dialect = SimpleNamespace(name="postgresql", driver="psycopg", create_connect_args=lambda expected: ([], {"host": "CONTROLLED"}))
    fence.do_connect(dialect, None, [], {"host": "CONTROLLED"})
    fence.do_connect(dialect, None, [], {"host": "CONTROLLED"})
    assert fence.connections == 2
    with pytest.raises(PermissionError):
        fence.do_connect(dialect, None, [], {"host": "CONTROLLED"})


@pytest.mark.parametrize("fault", ["foreign_url", "echo", "pool"])
def test_engine_guard_refuses_before_identity_query(fault):
    from sqlalchemy.pool import NullPool
    fence = child.ChildFence(target(), source())
    def forbidden():
        pytest.fail("DBAPI creator must never run")
    engine = SimpleNamespace(url=fence.expected, echo=False, pool=NullPool(creator=forbidden))
    if fault == "foreign_url":
        engine.url = URL.create("postgresql+psycopg", host="FOREIGN")
    if fault == "echo":
        engine.echo = True
    if fault == "pool":
        engine.pool = object()
    with pytest.raises(PermissionError):
        fence.engine_connect(SimpleNamespace(engine=engine))


def test_source_review_refuses_empty_or_noncanonical_approval(tmp_path):
    value = dict(purpose="MAIN_REVIEWED_REAL_ENV_WHOLE_PRODUCTION_SCHEMA_REHEARSAL",
        source_root=str(tmp_path), source_head="b" * 40, source_pins={}, authority_sha256="c" * 64, review_id="CONTROLLED")
    raw = child.canonical(value)
    with pytest.raises(ValueError):
        child.SourceReview(raw, sha256(raw).hexdigest())


def test_actual_artifact_hash_and_private_permissions_required(tmp_path):
    path = tmp_path / "actual.json"
    raw = b'{"CONTROLLED":true}'
    path.write_bytes(raw)
    path.chmod(0o600)
    assert child.read_artifact(dict(path=str(path), sha256=sha256(raw).hexdigest())) == raw
    path.chmod(0o644)
    with pytest.raises(ValueError):
        child.read_artifact(dict(path=str(path), sha256=sha256(raw).hexdigest()))


def test_parent_cannot_open_factory_before_actual_typed_inputs():
    with pytest.raises(ValueError, match="exact_actual_nonce"):
        parent.rehearse(lambda: pytest.fail("factory before proof forbidden"), target=None, lease=None, journal=None,
            schema=None, source=None, child_inputs={}, interpreter={}, source_guard=lambda target: True)


def test_artifact_is_create_once_and_readback_identical(tmp_path):
    path = tmp_path / "plan.json"
    raw = b'{"CONTROLLED":true}'
    result = parent.write_new(path, raw)
    assert result == dict(path=str(path), sha256=sha256(raw).hexdigest())
    assert path.read_bytes() == raw and (os.stat(path).st_mode & 0o777) == 0o600
    with pytest.raises(FileExistsError):
        parent.write_new(path, raw)
