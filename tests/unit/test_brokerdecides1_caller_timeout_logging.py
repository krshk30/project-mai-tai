"""Caller-wait logging only; injected collectors, no provider/DB or native proof."""

import asyncio
import logging

import pytest

from project_mai_tai.brokerdecides1 import decode
from project_mai_tai.brokerdecides1_sync import begin_pass, finish_pass
from tests.unit.test_brokerdecides1_sync import native, service


def _timeout_records(caplog):
    return [record for record in caplog.records
        if "[BROKERDECIDES1-CALLER-TIMEOUT]" in record.getMessage()]


@pytest.mark.asyncio
async def test_actual_five_second_caller_timeout_warns_once_and_rate_limits(caplog, monkeypatch):
    reads, cancelled = [], []

    async def reader(account):
        reads.append(account)
        if account == "live:schwab":
            return native(account)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(account)

    value = service(monkeypatch, reader)
    with caplog.at_level(logging.WARNING, logger=value.logger.name):
        for expected_generation in (1, 2):
            generation = await begin_pass(value)
            assert generation == expected_generation
            await finish_pass(value, generation, sync_succeeded=True)
            packet = decode(value.redis.raw)
            assert packet.generation == generation and not packet.local_complete
            assert [book.account for book in packet.books] == ["live:schwab"]
            assert packet.reason == "ValueError:native_account_book_UNKNOWN"
            assert packet.inflight == frozenset() and packet.filled_segments == frozenset()
            assert not value.__dict__.get("_brokerdecides1_pass_waiters")
    record, = _timeout_records(caplog)
    assert record.levelno == logging.WARNING and record.exc_info is None
    assert record.args[:3] == (1, "live:webull", "live:webull")
    assert type(record.args[3]) is int and record.args[3] >= 5000
    text = record.getMessage()
    assert "exc=TimeoutError" in text and "budget_ms=5000" in text
    assert "elapsed_scope=max_timeout_reader_wait" in text
    assert reads == ["live:schwab", "live:webull"] * 2
    assert cancelled == ["live:webull"] * 2
    assert len(value.redis.writes) == 4  # Original begin+finish publication per pass.


@pytest.mark.asyncio
async def test_normal_native_publication_has_no_caller_timeout_warning(caplog, monkeypatch):
    reads = []

    async def reader(account):
        reads.append(account)
        return native(account)

    value = service(monkeypatch, reader)
    with caplog.at_level(logging.WARNING, logger=value.logger.name):
        generation = await begin_pass(value)
        await finish_pass(value, generation, sync_succeeded=True)
    packet = decode(value.redis.raw)
    assert packet.local_complete and len(packet.books) == 2
    assert packet.reason == "complete_native_and_local_census"
    assert packet.inflight == frozenset({"SAIQ"})
    assert packet.filled_segments == frozenset({("WORX", 900)})
    assert reads == ["live:schwab", "live:webull"]
    assert len(value.redis.writes) == 2 and _timeout_records(caplog) == []
    assert "_brokerdecides1_caller_timeout_warning_at" not in value.__dict__
    assert not value.__dict__.get("_brokerdecides1_pass_waiters")
