"""Controlled failure logging: no SDK/provider construction or private payloads."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from project_mai_tai import brokerdecides1_collect as collector
from project_mai_tai import brokerdecides1_sync as sync


def failure_records(caplog):
    return [record for record in caplog.records
        if "[BROKERDECIDES1-NATIVE-FAIL]" in record.getMessage()]


@pytest.mark.asyncio
@pytest.mark.parametrize("foreground", [None, False, True])
async def test_webull_failure_one_sanitized_log_from_thread_preserves_public_error(monkeypatch, caplog, foreground):
    private = "TOKEN_SECRET account=123456 URL=https://private.invalid/native"

    def blocking(adapter, account):
        collector._diagnostic_stage("positions", 2, 429)
        raise RuntimeError(private)

    monkeypatch.setattr(collector, "_webull_blocking", blocking)
    token = collector.BACKGROUND_FOREGROUND_ACTIVE.set(
        None if foreground is None else lambda: foreground)
    prior = collector.NATIVE_DIAGNOSTIC.get()
    try:
        with caplog.at_level(logging.WARNING, logger=collector.__name__):
            with pytest.raises(ValueError, match="^Webull_native_collection_UNKNOWN$") as caught:
                await collector.read_webull(SimpleNamespace(), "live:orb")
        assert str(caught.value) == "Webull_native_collection_UNKNOWN"
        assert collector.NATIVE_DIAGNOSTIC.get() is prior
    finally:
        collector.BACKGROUND_FOREGROUND_ACTIVE.reset(token)
    records = failure_records(caplog)
    assert len(records) == 1 and records[0].exc_info is None
    text = records[0].getMessage()
    expected = "UNKNOWN" if foreground is None else str(foreground).lower()
    assert f"foreground_sync_active={expected}" in text
    assert "account=live:orb" in text and "stage=positions page=2 http_status=429" in text
    assert "exc=RuntimeError msg=REDACTED" in text
    assert all(secret not in text for secret in (private, "TOKEN_SECRET", "123456", "https://"))


@pytest.mark.asyncio
@pytest.mark.parametrize("message,expected", [
    ("positions background budget unavailable", "positions background budget unavailable"),
    ("list-open background budget unavailable", "list-open background budget unavailable"),
    ("positions background budget unavailable PRIVATE123", "REDACTED"),
])
async def test_query_budget_only_exact_allowlisted_messages(monkeypatch, caplog, message, expected):
    class QueryBudgetUnavailable(RuntimeError):
        pass

    def blocking(adapter, account):
        collector._diagnostic_stage("orders", 1)
        raise QueryBudgetUnavailable(message)

    monkeypatch.setattr(collector, "_webull_blocking", blocking)
    with caplog.at_level(logging.WARNING, logger=collector.__name__):
        with pytest.raises(ValueError, match="^Webull_native_collection_UNKNOWN$"):
            await collector.read_webull(SimpleNamespace(), "live:orb")
    records = failure_records(caplog)
    assert len(records) == 1 and f"msg={expected}" in records[0].getMessage()
    assert "PRIVATE123" not in records[0].getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["positions_http", "orders_http", "private_transport"])
async def test_schwab_one_log_original_exception_stage_and_no_private_data(caplog, fault):
    private_error = RuntimeError("PRIVATE_TOKEN ACCOUNT_9876 https://secret.invalid")
    calls = []

    async def request(method, path):
        calls.append(path)
        if path.endswith("accountNumbers"):
            return 200, {}, [{"accountNumber": "123", "hashValue": "fixture-hash"}]
        if "fields=positions" in path:
            if fault == "private_transport":
                raise private_error
            return (503 if fault == "positions_http" else 200), {}, {
                "securitiesAccount": {"accountNumber": "123", "positions": []}}
        return 429, {}, {"PRIVATE": "TOKEN"}

    adapter = SimpleNamespace(accounts_by_name={"live:schwab_1m_v2":
        SimpleNamespace(account_hash="fixture-hash")}, _authorized_request_json=request)
    with caplog.at_level(logging.WARNING, logger=collector.__name__):
        with pytest.raises((ValueError, RuntimeError)) as caught:
            await collector.read_schwab(adapter, "live:schwab_1m_v2")
    record, = failure_records(caplog)
    text = record.getMessage()
    if fault == "private_transport":
        assert caught.value is private_error
        assert "stage=positions page=1 http_status=UNKNOWN" in text
    elif fault == "positions_http":
        assert str(caught.value) == "Schwab_native_positions_UNKNOWN"
        assert "msg=Schwab_native_positions_UNKNOWN stage=positions page=1 http_status=503" in text
    else:
        assert str(caught.value) == "Schwab_native_query_refused_not_range_UNKNOWN"
        assert "stage=orders page=1 http_status=429" in text
    assert len(calls) == (3 if fault == "orders_http" else 2)
    assert all(secret not in text for secret in ("PRIVATE", "9876", "https://", "fixture-hash"))
    assert collector.NATIVE_DIAGNOSTIC.get() is None


@pytest.mark.asyncio
async def test_inflight_rejection_one_log_no_second_invocation(caplog):
    release = asyncio.Event()
    existing = asyncio.create_task(release.wait())
    adapter = SimpleNamespace(_brokerdecides_native_tasks={"live:orb": existing})
    try:
        with caplog.at_level(logging.WARNING, logger=collector.__name__):
            with pytest.raises(ValueError, match="^native_read_inflight_UNKNOWN$"):
                await collector.read_webull(adapter, "live:orb")
        record, = failure_records(caplog)
        assert "msg=native_read_inflight_UNKNOWN" in record.getMessage()
        assert adapter._brokerdecides_native_tasks["live:orb"] is existing
    finally:
        release.set()
        await existing


@pytest.mark.asyncio
async def test_closing_refusal_does_not_claim_foreground_sync(monkeypatch, caplog):
    service = SimpleNamespace(_brokerdecides1_foreground_syncs=0, _brokerdecides1_closing=False)

    async def begin(_service):
        _service._brokerdecides1_closing = True
        return "controlled-generation"

    async def finish(_service, generation, *, sync_succeeded):
        with pytest.raises(ValueError, match="^native_foreground_sync_has_priority_UNKNOWN$"):
            await collector.read_webull(SimpleNamespace(), "live:orb")

    monkeypatch.setattr(sync, "begin_pass", begin)
    monkeypatch.setattr(sync, "finish_pass", finish)
    with caplog.at_level(logging.WARNING, logger=collector.__name__):
        await sync._background_refresh(service)
    record, = failure_records(caplog)
    assert "foreground_sync_active=false" in record.getMessage()
    assert "msg=native_foreground_sync_has_priority_UNKNOWN" in record.getMessage()
    assert collector.BACKGROUND_FOREGROUND_ACTIVE.get() is None
    assert collector.BACKGROUND_ALLOWED.get() is None


@pytest.mark.asyncio
async def test_success_has_no_failure_log_and_resets_context(monkeypatch, caplog):
    sentinel = object()
    monkeypatch.setattr(collector, "_webull_blocking", lambda adapter, account: sentinel)
    with caplog.at_level(logging.WARNING, logger=collector.__name__):
        assert await collector.read_webull(SimpleNamespace(), "live:orb") is sentinel
    assert failure_records(caplog) == [] and collector.NATIVE_DIAGNOSTIC.get() is None


@pytest.mark.asyncio
async def test_logger_failure_cannot_replace_original_schwab_error(monkeypatch):
    original = RuntimeError("PRIVATE_TRANSPORT")

    async def request(*args):
        raise original

    def broken_logger(*args, **kwargs):
        raise RuntimeError("controlled logging sink failure")

    monkeypatch.setattr(collector.LOGGER, "warning", broken_logger)
    adapter = SimpleNamespace(accounts_by_name={"live:schwab":
        SimpleNamespace(account_hash="fixture-hash")}, _authorized_request_json=request)
    with pytest.raises(RuntimeError) as caught:
        await collector.read_schwab(adapter, "live:schwab")
    assert caught.value is original and collector.NATIVE_DIAGNOSTIC.get() is None


@pytest.mark.asyncio
async def test_cancelled_waiter_retains_delegate_then_logs_its_failure_once(caplog):
    entered, release = asyncio.Event(), asyncio.Event()
    original = RuntimeError("PRIVATE_LATE_FAILURE")

    async def request(*args):
        entered.set()
        await release.wait()
        raise original

    adapter = SimpleNamespace(accounts_by_name={"live:schwab":
        SimpleNamespace(account_hash="fixture-hash")}, _authorized_request_json=request)
    with caplog.at_level(logging.WARNING, logger=collector.__name__):
        caller = asyncio.create_task(collector.read_schwab(adapter, "live:schwab"))
        await entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        retained = adapter._brokerdecides_native_tasks["live:schwab"]
        assert not retained.done() and failure_records(caplog) == []
        release.set()
        with pytest.raises(RuntimeError) as caught:
            await retained
    assert caught.value is original and collector.NATIVE_DIAGNOSTIC.get() is None
    record, = failure_records(caplog)
    assert "msg=REDACTED" in record.getMessage()
    assert "PRIVATE_LATE_FAILURE" not in record.getMessage()


@pytest.mark.asyncio
async def test_unknown_account_and_exception_type_never_leak_identifiers(monkeypatch, caplog):
    class PRIVATE_TYPE_ACCOUNT_9876(Exception):
        pass

    def blocking(*args):
        raise PRIVATE_TYPE_ACCOUNT_9876("Schwab_native_positions_UNKNOWN SECRET" + "x" * 400)

    monkeypatch.setattr(collector, "_webull_blocking", blocking)
    with caplog.at_level(logging.WARNING, logger=collector.__name__):
        with pytest.raises(ValueError, match="^Webull_native_collection_UNKNOWN$"):
            await collector.read_webull(SimpleNamespace(), "PRIVATE_ACCOUNT_9876")
    record, = failure_records(caplog)
    text = record.getMessage()
    assert "account=UNKNOWN" in text and "exc=Exception msg=REDACTED" in text
    assert all(value not in text for value in ("PRIVATE", "9876", "SECRET", "xxxx"))
