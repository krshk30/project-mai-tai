"""Historical9c fallback closure plus the explicit installed L14 successor.

The existing fixture is immutable. Historical APP code is read as pinned Git
blobs only, never imported/checked out. Current metadata/GET assertions still use
the current application. No copy/load/install or L11/L12 acceptance is claimed.
"""

import ast
import asyncio
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import subprocess

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from project_mai_tai.atr_simulation import models
from project_mai_tai.atr_simulation.ui_reader import SimulationUIReader
from project_mai_tai.db.base import Base
from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings


ROOT = Path(__file__).resolve().parents[2]
FALLBACK = "9c10a7de1cd2fc7a7df2b0e587fe132d46faa4a7"
COMBINED = "724d36c5010f3e544e0d29d509060528662f5f82"
REVIEWED_TREES = {
    FALLBACK: "900fa2ed892c4800afa2f38d04ddc00d8541348e",
    COMBINED: "a3e33f457913363cb918eb250dcd08b10684efab",
}
CONTRACT_PATH = "tests/fixtures/atr_simulation/l14_only_a2_projection.json"
CONTRACT_SHA256 = "3eaba723e024d9ac3d675ac661a5981a45673fa0d72784c8160482c39cd091ad"
CONTRACT_BLOB = "682fedb05faf50ba40ed742c6da1cb7752fc8204"
CONTRACT_RAW = (ROOT / CONTRACT_PATH).read_bytes()
CONTRACT = json.loads(CONTRACT_RAW)
SETTINGS_ADDITION = (
    "    # Explicit immutable baseline artifact; None keeps aggregate reads unconfigured.\n"
    "    atr_simulation_aggregate_receipt_path: str | None = None\n"
    "    # Main-reviewed actual-copy allocator output; both absent means UNKNOWN.\n"
    "    atr_simulation_realized_receipt_path: str | None = None\n"
    "    atr_simulation_realized_receipt_sha256: str | None = None\n"
)


def git_blob(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def contract_pin():
    assert (ROOT / CONTRACT_PATH).read_bytes() == CONTRACT_RAW
    assert hashlib.sha256(CONTRACT_RAW).hexdigest() == CONTRACT_SHA256
    assert git_blob(CONTRACT_RAW) == CONTRACT_BLOB


def git_read(*args, input_bytes=None):
    # Literal local object reads only. No HEAD, checkout, remote, shell, replace
    # refs or inherited Git/environment configuration selects the test context.
    result = subprocess.run(
        ["/usr/bin/git", "--no-replace-objects", "-C", str(ROOT), *args],
        input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, check=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_REPLACE_OBJECTS": "1"},
    )
    assert not result.stderr, "pinned_history_read_must_not_warn_or_fall_back"
    return result.stdout


@lru_cache(maxsize=2)
def tree_paths(revision):
    assert revision in REVIEWED_TREES
    try:
        raw_tree = git_read("rev-parse", revision + "^{tree}")
    except subprocess.CalledProcessError as exc:
        missing = (f"fatal: ambiguous argument '{revision}^{{tree}}': unknown revision "
                   "or path not in the working tree.").encode("ascii")
        if exc.returncode == 128 and missing in exc.stderr:
            pytest.skip(f"requires local rig artifact Git object {revision}^{{tree}}")
        raise
    actual_tree = raw_tree.decode("ascii").strip()
    assert actual_tree == REVIEWED_TREES[revision], "explicit_reviewed_tree_required"
    raw = git_read("ls-tree", "-r", "--name-only", "-z", revision, "--", "src", "sql", "ops")
    assert raw.endswith(b"\0"), "missing_or_empty_reviewed_runtime_tree"
    paths = raw[:-1].decode("utf-8").split("\0")
    assert len(paths) == len(set(paths))
    return frozenset(paths)


@lru_cache(maxsize=1)
def historical_blobs():
    contract_pin()
    tree_paths(FALLBACK)
    paths = sorted({item["path"] for group in ("protected_a2_files", "shared_files", "copied_files")
                    for item in CONTRACT[group]} | {CONTRACT_PATH})
    for path in paths:
        assert not Path(path).is_absolute() and ".." not in Path(path).parts
        assert not any(char in path for char in "\n\r\0")
    request = "".join(FALLBACK + ":" + path + "\n" for path in paths).encode("utf-8")
    raw = git_read("cat-file", "--batch", input_bytes=request)
    result, offset = {}, 0
    for path in paths:
        boundary = raw.index(b"\n", offset)
        header = raw[offset:boundary].split()
        assert len(header) == 3 and header[1] == b"blob", "missing_history_object_is_a_failure"
        size = int(header[2])
        assert size >= 0
        start, end = boundary + 1, boundary + 1 + size
        value = raw[start:end]
        assert len(value) == size and raw[end:end + 1] == b"\n"
        assert git_blob(value) == header[0].decode("ascii")
        result[path], offset = value, end + 1
    assert offset == len(raw), "unexpected_history_batch_output"
    assert result[CONTRACT_PATH] == CONTRACT_RAW, "original_fixture_cannot_be_reauthored"
    return result


def historical_settings_fields(raw):
    classes = [node for node in ast.parse(raw).body
               if isinstance(node, ast.ClassDef) and node.name == "Settings"]
    assert len(classes) == 1
    return {node.target.id for node in classes[0].body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}


def current_runtime_paths():
    paths = set()
    for prefix in ("src", "sql", "ops"):
        for path in (ROOT / prefix).rglob("*"):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            assert not path.is_symlink(), "runtime_source_must_not_redirect_to_foreign_code"
            if path.is_file():
                paths.add(path.relative_to(ROOT).as_posix())
    return paths


def test_projection_names_exact_a2_base_and_immutable_f_donor():
    contract_pin()
    assert CONTRACT["base_head"] == "a2d4156d06ab2bbe0b072a1d88a80141192470c6"
    assert CONTRACT["donor_head"] == "2782fe6b1f8135703203682f4be633aa0c751768"
    assert len(CONTRACT["protected_a2_files"]) == 513
    assert len(CONTRACT["shared_files"]) == 2
    assert len(CONTRACT["copied_files"]) == 107


def test_every_protected_a2_runtime_file_remains_byte_identical():
    blobs = historical_blobs()
    for item in CONTRACT["protected_a2_files"]:
        assert git_blob(blobs[item["path"]]) == item["git_blob"], item["path"]


def test_no_undeclared_runtime_or_migration_path_can_enter_fallback():
    contract_pin()
    baseline = {item["path"] for item in CONTRACT["protected_a2_files"]}
    shared = {item["path"] for item in CONTRACT["shared_files"]}
    additions = {item["path"] for item in CONTRACT["copied_files"]
                 if item["path"].startswith(("src/", "sql/", "ops/"))}
    actual = tree_paths(FALLBACK)
    expected = {path for path in baseline | shared
                if path.startswith(("src/", "sql/", "ops/"))} | additions
    assert actual == expected
    assert len([path for path in additions if path.startswith(
        "src/project_mai_tai/atr_simulation/")]) == 48
    assert not any("brokerdecides" in path or "keepwatch" in path for path in additions)


def test_all_copied_engine_io_ui_tests_and_evidence_keep_reviewed_donor_bytes():
    blobs = historical_blobs()
    for item in CONTRACT["copied_files"]:
        raw = blobs[item["path"]]
        assert git_blob(raw) == item["git_blob"], item["path"]
        assert hashlib.sha256(raw).hexdigest() == item["sha256"], item["path"]


def test_shared_control_plane_matches_only_reviewed_l14_postimage():
    item = next(item for item in CONTRACT["shared_files"] if item["path"].endswith("control_plane.py"))
    assert hashlib.sha256(historical_blobs()[item["path"]]).hexdigest() == item["sha256"]


def test_settings_are_a2_plus_only_three_opt_in_receipt_fields():
    item = next(item for item in CONTRACT["shared_files"] if item["path"].endswith("settings.py"))
    raw = historical_blobs()[item["path"]]
    assert hashlib.sha256(raw).hexdigest() == item["sha256"]
    addition = SETTINGS_ADDITION.encode("ascii")
    assert raw.count(addition) == 1
    assert git_blob(raw.replace(addition, b"", 1)) == item["a2_git_blob"]
    settings = Settings(_env_file=None)
    assert settings.atr_simulation_aggregate_receipt_path is None
    assert settings.atr_simulation_realized_receipt_path is None
    assert settings.atr_simulation_realized_receipt_sha256 is None
    fields = historical_settings_fields(raw)
    assert "strategy_schwab_1m_v2_keepwatch1_enabled" not in fields
    assert "strategy_schwab_1m_v2_brokerdecides1_enabled" not in fields


def test_installed_l14_successor_preserves_historical_preimages_and_reviewed_postimages():
    # Historical55 bytes stay literal9c; installed source has reviewed later changes.
    from tests.unit.test_l14_ui_current_lineage_successor import assert_current_protected_l14_successor
    assert_current_protected_l14_successor()


def test_current_runtime_path_closure_matches_reviewed_installed_successor():
    # Installed6ddb membership plus exact reviewed source-only changes, never HEAD.
    from tests.unit.test_l14_ui_current_lineage_successor import assert_current_runtime_membership_successor
    assert_current_runtime_membership_successor()


def test_sim_metadata_cannot_join_or_extend_live_base_metadata():
    assert models.metadata is not Base.metadata
    assert set(models.metadata.tables) == {
        "atr_simulation_inputs", "atr_simulation_consumers",
        "atr_simulation_runs", "atr_simulation_rows",
    }
    assert not set(models.metadata.tables) & set(Base.metadata.tables)


def test_normal_unconfigured_fallback_owns_only_lazy_sim_pool_and_get_routes(monkeypatch):
    attempted = []
    def forbidden(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("unselected SIM must not create a database engine")
    monkeypatch.setattr("project_mai_tai.atr_simulation.ui_reader._new_engine", forbidden)
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        app = cp.build_app(
            Settings(_env_file=None, trade_coach_enabled=False, bot_control_enabled=False),
            session_factory=sessionmaker(engine),
        )
        reader = app.state.firstbar_read_port
        assert isinstance(reader, SimulationUIReader) and reader._engine is None
        assert app.state.legacy_simulation_read_port._owner is reader
        assert app.state.aggregate_simulation_read_port is None
        async def requests():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://controlled"
            ) as client:
                firstbar = await client.get("/atr-firstbar/api")
                legacy = await client.get("/atr-simulation/api")
                aggregate = await client.get("/atr-aggregate/api")
                assert firstbar.status_code == 200
                assert legacy.status_code == 200
                assert aggregate.status_code == 200
                assert firstbar.json()["coverage_status"] == "UNKNOWN"
                assert legacy.json()["coverage_status"] == "UNKNOWN"
                for prefix in ("/atr-firstbar", "/atr-simulation", "/atr-aggregate"):
                    assert (await client.post(prefix)).status_code == 405
                    assert (await client.post(prefix + "/api")).status_code == 405
        asyncio.run(requests())
        assert attempted == [] and reader._engine is None
    finally:
        engine.dispose()
