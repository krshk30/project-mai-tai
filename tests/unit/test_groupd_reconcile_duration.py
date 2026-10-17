"""TOP20:3x existing INFO timing; real transaction, no global clock patch."""

import logging
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import event, func, select

from project_mai_tai.db.models import ReconciliationRun
from project_mai_tai.reconciliation import service as module
from project_mai_tai.settings import Settings
from tests.unit.test_reconciliation_service import FakeRedis, build_test_session_factory


def duration_service():
    factory = build_test_session_factory()
    service = module.ReconciliationService(
        Settings(_env_file=None, reconciliation_auto_resolve_flat_exposure_incidents=False),
        FakeRedis(), session_factory=factory,
    )
    service.logger = Mock(spec=logging.Logger)
    return service, factory


def test_cycle_info_duration_includes_actual_commit_and_preserves_original_fields(monkeypatch):
    service, factory = duration_service()
    ordering = []

    def monotonic():
        if not ordering:
            ordering.append("clock_start")
            return 100.0
        assert ordering == ["clock_start", "before_commit", "after_commit"]
        ordering.append("clock_end")
        return 100.125

    def before_commit(session):
        assert ordering == ["clock_start"]
        ordering.append("before_commit")

    def after_commit(session):
        assert ordering == ["clock_start", "before_commit"]
        ordering.append("after_commit")

    # Replace only the defining module's reference, not time.monotonic globally:
    # asyncio, DB internals and other tests keep their real clocks.
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=monotonic))
    event.listen(factory.class_, "before_commit", before_commit)
    event.listen(factory.class_, "after_commit", after_commit)
    try:
        result = service.run_reconciliation_cycle()
    finally:
        event.remove(factory.class_, "before_commit", before_commit)
        event.remove(factory.class_, "after_commit", after_commit)
    assert ordering == ["clock_start", "before_commit", "after_commit", "clock_end"]
    assert set(result) == {"run_id", "status", "summary"}
    assert result["status"] == "completed"
    assert result["summary"]["total_findings"] == 0
    assert result["summary"]["critical_findings"] == 0
    assert result["summary"]["warning_findings"] == 0
    assert "duration_ms" not in result["summary"]
    service.logger.info.assert_called_once_with(
        "[RECONCILER-CYCLE] run_id=%s status=%s findings=%s critical=%s warning=%s duration_ms=125.000",
        UUID(result["run_id"]), "completed", 0, 0, 0,
    )
    service.logger.error.assert_not_called()
    service.logger.exception.assert_not_called()
    with factory() as session:
        row = session.get(ReconciliationRun, UUID(result["run_id"]))
        assert row.status == "completed" and row.summary == result["summary"]


def test_failed_commit_still_raises_without_success_duration_info(monkeypatch):
    service, factory = duration_service()
    calls = []

    def monotonic():
        calls.append("clock_start")
        assert calls == ["clock_start"]
        return 100.0

    failure = RuntimeError("controlled real cycle commit failure")

    def before_commit(session):
        raise failure

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=monotonic))
    event.listen(factory.class_, "before_commit", before_commit)
    try:
        with pytest.raises(RuntimeError) as caught:
            service.run_reconciliation_cycle()
    finally:
        event.remove(factory.class_, "before_commit", before_commit)
    assert caught.value is failure
    assert calls == ["clock_start"]
    service.logger.info.assert_not_called()
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ReconciliationRun)) == 0
