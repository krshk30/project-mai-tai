from __future__ import annotations

import asyncio
import logging
from unittest.mock import Mock

import pytest

from project_mai_tai.reconciliation.service import ReconciliationService
from project_mai_tai.services import reconciler
from project_mai_tai.settings import Settings

from tests.unit.test_reconciliation_service import FakeRedis, build_test_session_factory


def test_entrypoint_configures_logging_before_starting_service(monkeypatch) -> None:
    settings = Settings(log_level="DEBUG")
    calls = []

    class Service:
        def __init__(self, *, settings):
            calls.append(("construct", settings))

        async def run(self):
            calls.append(("run",))

    monkeypatch.setattr(reconciler, "get_settings", lambda: settings)
    monkeypatch.setattr(
        reconciler, "configure_logging", lambda *args: calls.append(("logging", *args))
    )
    monkeypatch.setattr(reconciler, "ReconciliationService", Service)
    asyncio.run(reconciler.main())
    assert calls == [("logging", "reconciler", "DEBUG"), ("construct", settings), ("run",)]


def test_cycle_summary_is_logged_after_commit(caplog, monkeypatch) -> None:
    factory = build_test_session_factory()
    service = ReconciliationService(
        settings=Settings(reconciliation_auto_resolve_flat_exposure_incidents=False),
        redis_client=FakeRedis(),
        session_factory=factory,
    )
    commits = []
    original_commit = factory.class_.commit

    def commit(session):
        original_commit(session)
        commits.append(True)

    monkeypatch.setattr(factory.class_, "commit", commit)
    logger = Mock(wraps=service.logger)
    service.logger = logger
    logger.info.side_effect = lambda *args: (
        commits == [True] or pytest.fail("cycle summary emitted before commit")
    )
    with caplog.at_level(logging.INFO, logger="reconciler"):
        result = service.run_reconciliation_cycle()
    logger.info.assert_called_once()
    args = logger.info.call_args.args
    assert args[0].startswith("[RECONCILER-CYCLE]")
    assert str(args[1]) == result["run_id"]
    assert args[2:] == (
        "completed",
        result["summary"]["total_findings"],
        result["summary"]["critical_findings"],
        result["summary"]["warning_findings"],
    )


def test_failed_commit_does_not_log_success(monkeypatch) -> None:
    factory = build_test_session_factory()
    service = ReconciliationService(
        settings=Settings(reconciliation_auto_resolve_flat_exposure_incidents=False),
        redis_client=FakeRedis(),
        session_factory=factory,
    )

    def failed_commit(session):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(factory.class_, "commit", failed_commit)
    service.logger = Mock()
    with pytest.raises(RuntimeError, match="database unavailable"):
        service.run_reconciliation_cycle()
    service.logger.info.assert_not_called()
