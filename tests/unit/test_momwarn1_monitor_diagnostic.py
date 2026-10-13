"""Controlled local permission/logging evidence, never a box readiness receipt."""

import asyncio
import errno
import json
import logging
import os
from types import SimpleNamespace

import pytest

from project_mai_tai.market_data.gateway import _warning_monitor_diagnostic
from tests.unit.test_momwarn1_gateway import aggregate, event, healthy, service
from tests.unit.test_momwarn1_live_health import setup as live_setup

setup = live_setup


@pytest.mark.asyncio
@pytest.mark.parametrize("log_name", ["oms.log", "strategy.log", "schwab-1m-v2.log", "orb-schwab.log"])
async def test_actual_permission_error_one_warning_and_sticky_A_only_revert(setup, caplog, log_name):
    if os.geteuid() == 0:
        pytest.skip("OS permission proof requires nonroot; never substitute root success")
    env = setup()
    svc, provider, redis, warning_redis = service()
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["NEW"]))
    svc._warning_health_reader, svc._warning_redis = env.reader, env.client
    await svc._warning_monitor_once()
    provider._handle_messages([aggregate()])
    owners = {key: set(value) for key, value in svc._desired_symbols_by_consumer.items()}
    saved = dict(redis.hashes)
    ordinary_entries = list(redis.entries)
    path = env.directory / log_name
    original_mode = path.stat().st_mode & 0o777
    path.chmod(0)
    try:
        with pytest.raises(PermissionError) as denied:
            path.open("rb")
        assert denied.value.errno == errno.EACCES
        with caplog.at_level(logging.WARNING, logger="market-data-gateway"):
            await svc._warning_monitor_once()
            await svc._warning_monitor_once()
    finally:
        path.chmod(original_mode)
    rows = [row for row in caplog.records if row.levelno >= logging.WARNING]
    assert len(rows) == 1 and rows[0].levelno == logging.WARNING
    assert rows[0].exc_info is None
    assert "[MOMWARN1-AUTO-REVERT] reason=monitor_unreadable A_only=true" in rows[0].message
    detail = json.loads(rows[0].message.split("detail=", 1)[1])
    assert detail["exception_class"] == "PermissionError"
    assert "Permission denied" in detail["message"] and str(path) in detail["message"]
    assert "\n" not in rows[0].message and "\r" not in rows[0].message
    assert svc._warning_revert_reason == "monitor_unreadable"
    assert not svc._warning_active and not provider._warning_aggregates_enabled
    assert provider._ws.removed == ["A.*"]
    assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}
    assert svc._desired_symbols_by_consumer == owners and redis.hashes == saved
    assert redis.entries == ordinary_entries and warning_redis.entries == []
    assert svc._warning_queue.empty()
    reverted = svc._warning_reverted_utc_ns
    await svc._warning_monitor_once()
    assert svc._warning_reverted_utc_ns == reverted
    assert provider._ws.added.count("A.*") == 1


@pytest.mark.parametrize("exc", [
    ValueError('bad\nvalue\r\x1b[31m "quotes" ' + "x" * 5000),
    RuntimeError("redis://user:password@localhost/0 token=secret password='two words' "
                 "Authorization:Bearer-private Bearer abc123 1234567:telegramsecret chat_id=-12345"),
    RuntimeError("Authorization: Bearer abc123 access_token=secretvalue refresh-token='two words' "
                 '"client_secret": "privatevalue"'),
    TimeoutError(),
])
def test_exception_detail_bounded_escaped_and_credentials_redacted(exc):
    raw = _warning_monitor_diagnostic(exc)
    detail = json.loads(raw)
    assert detail["exception_class"] == type(exc).__name__
    assert len(detail["message"]) <= 512 and len(raw) <= 3200
    assert not any(value in raw for value in ("\n", "\r", "\x1b", "telegramsecret",
                                             "abc123", "two words", "-12345", "user:password"))
    if isinstance(exc, RuntimeError):
        assert "REDACTED" in raw and "secret" not in raw.lower()


def test_diagnostic_string_failure_cannot_prevent_safety_revert():
    class BrokenMessage(Exception):
        def __str__(self):
            raise RuntimeError("private message cannot be formatted")

    detail = json.loads(_warning_monitor_diagnostic(BrokenMessage()))
    assert detail == {"exception_class": "BrokenMessage", "message": "[message unavailable]"}


@pytest.mark.asyncio
async def test_first_monitor_permission_failure_never_subscribes_A(setup, caplog):
    if os.geteuid() == 0:
        pytest.skip("actual file denial control requires nonroot")
    env = setup()
    svc, provider, redis, _ = service()
    await svc.apply_subscription_event(event("v2", ["WFF"]))
    await svc.apply_subscription_event(event("momentum-warning", ["NEW"]))
    svc._warning_health_reader, svc._warning_redis = env.reader, env.client
    saved = dict(redis.hashes)
    path = env.directory / "oms.log"
    mode = path.stat().st_mode & 0o777
    path.chmod(0)
    try:
        with caplog.at_level(logging.WARNING, logger="market-data-gateway"):
            await svc._warning_monitor_once()
            await svc._warning_monitor_once()
    finally:
        path.chmod(mode)
    assert len(caplog.records) == 1 and "PermissionError" in caplog.records[0].message
    assert svc._warning_revert_reason == "monitor_unreadable" and not svc._warning_active
    assert "A.*" not in provider._ws.added and provider._ws.removed == []
    assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}
    assert redis.hashes == saved


@pytest.mark.asyncio
async def test_cancelled_monitor_is_not_unreadability_warning(caplog):
    svc, _, _, _ = service()

    async def read_live(*_):
        raise asyncio.CancelledError

    svc._warning_health_reader = SimpleNamespace(read_live=read_live)
    with caplog.at_level(logging.WARNING, logger="market-data-gateway"):
        with pytest.raises(asyncio.CancelledError):
            await svc._warning_monitor_once()
    assert not caplog.records and svc._warning_revert_reason is None


@pytest.mark.asyncio
async def test_healthy_monitor_does_not_emit_diagnostic(caplog):
    svc, _, _, _ = service()
    svc._warning_health_reader = SimpleNamespace(read=healthy)
    with caplog.at_level(logging.WARNING, logger="market-data-gateway"):
        await svc._warning_monitor_once()
    assert not caplog.records and svc._warning_active
