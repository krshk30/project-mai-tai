from __future__ import annotations

import os
from pathlib import Path

from ops.health.service_source_freshness import evaluate, service_source_files


def _write(repo: Path, relative: str, body: str, mtime: float) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _fixture(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        "pyproject.toml",
        """
[project]
name = "freshness-fixture"
version = "0"
[project.scripts]
mai-tai-oms = "project_mai_tai.services.oms_risk:run"
mai-tai-schwab-1m-v2 = "project_mai_tai.services.schwab_1m_v2_bot:run"
""",
        10,
    )
    _write(tmp_path, "src/project_mai_tai/__init__.py", "", 10)
    _write(tmp_path, "src/project_mai_tai/services/__init__.py", "", 10)
    _write(
        tmp_path,
        "src/project_mai_tai/services/oms_risk.py",
        "from project_mai_tai.shared import store\n\ndef run(): pass\n",
        90,
    )
    _write(tmp_path, "src/project_mai_tai/shared/__init__.py", "", 10)
    _write(tmp_path, "src/project_mai_tai/shared/store.py", "VALUE = 1\n", 80)
    _write(
        tmp_path,
        "src/project_mai_tai/services/schwab_1m_v2_bot.py",
        "from project_mai_tai.strategy_core import schwab_1m_v2\n\ndef run(): pass\n",
        190,
    )
    _write(tmp_path, "src/project_mai_tai/strategy_core/__init__.py", "", 10)
    _write(tmp_path, "src/project_mai_tai/strategy_core/schwab_1m_v2.py", "VALUE = 2\n", 200)
    return tmp_path


def test_unrelated_v2_pull_does_not_call_oms_stale(tmp_path: Path) -> None:
    repo = _fixture(tmp_path)

    result = evaluate(repo, "oms", process_start_epoch=100)

    assert result.verdict == "FRESH"
    assert "schwab_1m_v2.py" not in {path.name for path in result.files}


def test_reachable_source_newer_than_process_is_stale_control(tmp_path: Path) -> None:
    repo = _fixture(tmp_path)
    shared = repo / "src/project_mai_tai/shared/store.py"
    os.utime(shared, (110, 110))

    result = evaluate(repo, "oms", process_start_epoch=100)

    assert result.verdict == "STALE"
    assert result.exit_code == 1
    assert result.files == (shared,)


def test_v2_service_sees_its_own_newer_source(tmp_path: Path) -> None:
    repo = _fixture(tmp_path)

    result = evaluate(repo, "schwab-1m-v2", process_start_epoch=100)

    assert result.verdict == "STALE"
    assert any(path.name == "schwab_1m_v2.py" for path in result.files)


def test_newer_lazy_import_is_could_not_tell_not_stale(tmp_path: Path) -> None:
    repo = _fixture(tmp_path)
    _write(
        repo,
        "src/project_mai_tai/services/oms_risk.py",
        "def later():\n    from project_mai_tai.strategy_core import schwab_1m_v2\n",
        90,
    )

    result = evaluate(repo, "oms", process_start_epoch=100)

    assert result.verdict == "COULD_NOT_TELL"
    assert "conditional/lazy" in result.detail


def test_missing_entry_module_is_could_not_tell(tmp_path: Path) -> None:
    repo = _fixture(tmp_path)
    (repo / "src/project_mai_tai/services/oms_risk.py").unlink()

    result = evaluate(repo, "oms", process_start_epoch=100)

    assert result.verdict == "COULD_NOT_TELL"
    assert result.exit_code == 3


def test_project_import_resolution_failure_is_could_not_tell(tmp_path: Path) -> None:
    repo = _fixture(tmp_path)
    _write(
        repo,
        "src/project_mai_tai/services/oms_risk.py",
        "import project_mai_tai.does_not_exist\n",
        90,
    )

    result = evaluate(repo, "oms", process_start_epoch=100)

    assert result.verdict == "COULD_NOT_TELL"
    assert "does not resolve" in result.detail


def test_real_repository_graph_resolves() -> None:
    repo = Path(__file__).resolve().parents[2]

    files = service_source_files(repo, "oms")

    assert repo / "src/project_mai_tai/services/oms_risk.py" in files
    assert all(path.is_file() for path in files)


def test_collector_uses_service_scope_and_labels_unknown() -> None:
    repo = Path(__file__).resolve().parents[2]
    collector = (repo / "ops/health/collect_deploy_evidence.sh").read_text(encoding="utf-8")

    assert "service_source_freshness.py" in collector
    assert '--service "$u"' in collector
    assert '*) v="⚠ $v"' in collector
    assert 'v="⛔ NOT_RUNNING — source freshness is not health"' in collector
    assert "find src -type f -name '*.py'" not in collector
