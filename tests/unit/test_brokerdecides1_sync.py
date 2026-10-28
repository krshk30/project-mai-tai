"""Offline injected collectors only. No production readers or native calls."""
from dataclasses import replace
import logging
from types import SimpleNamespace

import pytest

from project_mai_tai.brokerdecides1 import AdmissionMemory, NativeAccountBook, decode
from project_mai_tai.brokerdecides1_sync import begin_pass, finish_pass, refresh_consumer


class RedisFixture:
    def __init__(self):
        self.raw = None
        self.writes = []

    async def set(self, key, raw, **kwargs):
        self.raw = raw
        self.writes.append((key, decode(raw), kwargs))

    async def get(self, key):
        return self.raw


def service(monkeypatch, reader=None):
    import project_mai_tai.brokerdecides1_sync as module

    monkeypatch.setattr(module, "now_ms", lambda: 1000)
    settings = SimpleNamespace(strategy_schwab_1m_v2_brokerdecides1_enabled=True,
        strategy_schwab_1m_v2_account_name="live:schwab",
        strategy_schwab_1m_v2_webull_account_name="live:webull", redis_stream_prefix="private_fixture")
    adapter = SimpleNamespace()
    if reader is not None:
        adapter.read_native_entry_book = reader

    async def run_db(function, **kwargs):
        if kwargs.get("commit", True):
            return None, 0, 0
        return frozenset({"SAIQ"}), frozenset({("WORX", 900)}), frozenset()

    return SimpleNamespace(settings=settings, broker_adapter=adapter, redis=RedisFixture(),
        _run_db=run_db, logger=logging.getLogger(__name__), strategy=SimpleNamespace())


def native(account):
    return NativeAccountBook(account, "schwab" if "schwab" in account else "webull",
        1000, 1000, frozenset(), frozenset(), True, "fixture_complete")


@pytest.mark.asyncio
async def test_begin_publishes_unknown_before_any_reader(monkeypatch):
    value = service(monkeypatch)
    assert await begin_pass(value) == 1
    assert not value.redis.writes[0][1].local_complete
    assert value.redis.writes[0][1].books == ()


@pytest.mark.asyncio
async def test_source_capability_missing_retains_unknown_not_local_flat(monkeypatch):
    value = service(monkeypatch)
    generation = await begin_pass(value)
    await finish_pass(value, generation, sync_succeeded=True)
    result = decode(value.redis.raw)
    assert not result.local_complete and result.books == ()
    assert "UNAVAILABLE" in result.reason


@pytest.mark.asyncio
async def test_sync_failed_never_calls_native_reader(monkeypatch):
    async def reader(account):
        raise AssertionError("must not read after failed sync")

    value = service(monkeypatch, reader)
    generation = await begin_pass(value)
    await finish_pass(value, generation, sync_succeeded=False)
    assert not decode(value.redis.raw).local_complete


@pytest.mark.asyncio
async def test_both_complete_native_books_and_own_census_published(monkeypatch):
    reads = []

    async def reader(account):
        reads.append(account)
        return native(account)

    value = service(monkeypatch, reader)
    generation = await begin_pass(value)
    await finish_pass(value, generation, sync_succeeded=True)
    result = decode(value.redis.raw)
    assert result.local_complete and len(result.books) == 2
    assert reads == ["live:schwab", "live:webull"]
    assert result.inflight == frozenset({"SAIQ"})
    assert result.filled_segments == frozenset({("WORX", 900)})
    assert len(value.redis.writes) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["foreign", "cached", "partial", "exception"])
async def test_one_unproved_account_invalidates_whole_negative(monkeypatch, failure):
    async def reader(account):
        if "webull" not in account:
            return native(account)
        if failure == "exception":
            raise ValueError("read_failed")
        updates = {"foreign": {"account": "live:foreign"},
            "cached": {"wire_started_ms": 999}, "partial": {"complete": False}}
        return replace(native(account), **updates[failure])

    value = service(monkeypatch, reader)
    generation = await begin_pass(value)
    await finish_pass(value, generation, sync_succeeded=True)
    assert not decode(value.redis.raw).local_complete


@pytest.mark.asyncio
async def test_consumer_unreadable_packet_revokes_negative_but_keeps_reservation(monkeypatch):
    value = service(monkeypatch)
    memory = value.strategy._brokerdecides1_memory = AdmissionMemory()
    memory.reserve("WORX", "exact")
    value.redis.raw = "truncated"
    await refresh_consumer(value)
    assert memory.snapshot is None
    assert not memory.assess(accounts=("live:schwab", "live:webull"), symbol="WORX",
        segment=900, now_ms=1000, max_age_ms=500).allowed
    assert memory.terminal("WORX", "exact", proven=True)


@pytest.mark.asyncio
async def test_flag_off_has_no_transport_or_native_activity(monkeypatch):
    value = service(monkeypatch)
    value.settings.strategy_schwab_1m_v2_brokerdecides1_enabled = False
    assert await begin_pass(value) is None
    await finish_pass(value, None, sync_succeeded=True)
    await refresh_consumer(value)
    assert not value.redis.writes
