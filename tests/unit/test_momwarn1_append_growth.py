"""Fast CONTROLLED native samples, not 600s wall soak/30min/live/peak evidence."""

from collections import Counter, defaultdict
import os
import re
import time

import pytest

from tests.unit.test_momwarn1_live_health import start_native as start, three_clean_native_reads
from tests.unit.test_momwarn1_live_health import NS, line, setup as live_setup
from tests.unit.test_momwarn1_read_race import BASE

setup = live_setup
LOGS = ("oms.log", "schwab-1m-v2.log", "strategy.log", "orb-schwab.log")
MARKER = re.compile(rb"R2PRIME/(\S+)")


class ReadTap:
    def __init__(self, source, after):
        self.source, self.after = source, after

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.source.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.source, name)

    def read(self, size):
        raw = self.source.read(size)
        self.after(raw)
        return raw


@pytest.mark.asyncio
async def test_600_native_samples_every_read_appends_each_cursor_consumes_lines_once(setup, monkeypatch):
    env = setup(now=BASE)
    svc, provider, _ = await start(env)
    written, consumed = defaultdict(Counter), defaultdict(Counter)
    inode_names = {(env.directory / name).stat().st_ino: name for name in LOGS}
    sample = [0]
    append = [True]

    def consume(lane, raw):
        consumed[lane].update(value.decode() for value in MARKER.findall(raw))

    def add(name, writer):
        if append[0]:
            identifier = f"{name}/{writer}/{sample[0]}"
            written[name][identifier] += 1
            with (env.directory / name).open("a") as output:
                output.write(line(env.clock.ns, f"R2PRIME/{identifier}"))

    native_read = os.read

    def read(fd, size):
        raw = native_read(fd, size)
        name = inode_names.get(os.fstat(fd).st_ino)
        if name in ("oms.log", "schwab-1m-v2.log"):
            consume(f"native/{name}", raw)
            add(name, "native")
        return raw

    monkeypatch.setattr(os, "read", read)
    original_open = env.reader._open_regular

    def open_regular(path):
        source = original_open(path)
        if path.name not in LOGS:
            return source

        def after(raw):
            consume(f"errors/{path.name}", raw)
            add(path.name, "errors")

        return ReadTap(source, after)

    monkeypatch.setattr(env.reader, "_open_regular", open_regular)
    before = env.client.xrevrange.call_count
    wall_started = time.perf_counter()
    for index in range(1, 601):
        sample[0] = index
        env.clock.ns = BASE + index * NS
        if index % 15 == 0:
            with (env.directory / "oms.log").open("a") as output:
                output.write(line(env.clock.ns,
                    f"[OMS-BROKER-SYNC-PASS] id={10+index//15} phase=end outcome=ok duration_ms=520.000"))
        await svc._warning_monitor_once()
        assert svc._warning_active and svc._warning_revert_reason is None
        assert env.client.xrevrange.call_count == before + index  # No append retries.
    append[0] = False
    await svc._warning_monitor_once()  # Flush the last sample's deferred bytes.
    assert svc._warning_active and svc._warning_revert_reason is None
    assert provider._ws.removed == [] and env.reader.log_observability()["copytruncate_counts"] == {}
    for name in LOGS:
        assert consumed[f"errors/{name}"] == written[name]
        assert all(count == 1 for count in written[name].values())
        if name in ("oms.log", "schwab-1m-v2.log"):
            assert consumed[f"native/{name}"] == written[name]
    assert sum(sum(values.values()) for values in written.values()) == 3600
    assert env.reader._source.oms.latest["oms_sync_seq"] == 50
    print(f"CONTROLLED_600_NATIVE samples=600 flush=1 reverts=0 retries=0 "
          f"unique_lines=3600 consumed_once_per_cursor=true wall_seconds={time.perf_counter()-wall_started:.6f} "
          "NOT_LIVE_NOT_600S_WALL_NOT_30MIN_SOAK")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", LOGS)
async def test_ERROR_appended_after_error_read_detected_next_native_sample(setup, monkeypatch, name):
    env = setup()
    svc, provider, redis = await start(env)
    owners, saved = dict(svc._desired_symbols_by_consumer), dict(redis.hashes)
    original_open = env.reader._open_regular
    writes = []

    def open_regular(path):
        source = original_open(path)
        if path.name != name:
            return source

        def after(_):
            if not writes:
                writes.append(name)
                with path.open("a") as output:
                    output.write("ERROR CONTROLLED appended after error scan\n")

        return ReadTap(source, after)

    monkeypatch.setattr(env.reader, "_open_regular", open_regular)
    before = env.client.xrevrange.call_count
    await svc._warning_monitor_once()
    assert svc._warning_active and svc._warning_revert_reason is None and writes == [name]
    assert env.client.xrevrange.call_count == before + 1
    await svc._warning_monitor_once()
    assert svc._warning_revert_reason is None and svc._warning_active and provider._ws.removed == []
    assert env.reader.error_observations()["error_lines_by_log"][name] == 1
    assert env.client.xrevrange.call_count == before + 2
    assert svc._desired_symbols_by_consumer == owners and redis.hashes == saved
    assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["oms.log", "schwab-1m-v2.log"])
@pytest.mark.parametrize("unread_size", [65536, 65537])
async def test_postread_growth_belongs_to_next_pass_without_pausing(
        setup, monkeypatch, name, unread_size):
    env = setup()
    svc, provider, _ = await start(env)
    path = env.directory / name
    with path.open("a") as output:
        output.write("INFO prefix already consumed\n" * 32)
    unread = "I\n" * (unread_size // 2) + ("\n" if unread_size % 2 else "")
    calls = []
    original_open = env.reader._open_regular

    def open_regular(open_path):
        source = original_open(open_path)
        if open_path != path:
            return source

        def after(_):
            if not calls:
                calls.append(unread_size)
                with path.open("a") as output:
                    output.write(unread)

        return ReadTap(source, after)

    monkeypatch.setattr(env.reader, "_open_regular", open_regular)
    before = env.client.xrevrange.call_count
    await svc._warning_monitor_once()
    assert len(calls) == 1 and env.client.xrevrange.call_count == before + 1
    assert svc._warning_revert_reason is None and svc._warning_active
    await svc._warning_monitor_once()
    assert svc._warning_revert_reason is None and svc._warning_active
    source = env.reader._source.oms if name == "oms.log" else env.reader._source
    offset = source.position if name == "oms.log" else source.offset
    assert offset == path.stat().st_size and provider._ws.removed == []


@pytest.mark.asyncio
async def test_midread_shrink_requires_actual_short_pread_then_truthful_gap_rebaseline(setup, monkeypatch):
    env = setup()
    svc, provider, _ = await start(env)
    path = env.directory / "oms.log"
    original = os.read
    inode = path.stat().st_ino

    def read(fd, size):
        raw = original(fd, size)
        if os.fstat(fd).st_ino == inode:
            path.write_text("")
        return raw

    monkeypatch.setattr(os, "read", read)
    before = env.client.xrevrange.call_count
    await svc._warning_monitor_once()
    assert svc._warning_revert_reason is None and svc._warning_active
    assert provider._ws.removed == [] and env.client.xrevrange.call_count == before + 2
    assert env.reader.log_observability()["coverage_status"] == "GAP_UNOBSERVED_COPYTRUNCATE"


@pytest.mark.asyncio
@pytest.mark.parametrize("two_short_reads", [False, True])
async def test_only_positive_short_pread_has_one_global_retry_and_truthful_gap(
        setup, monkeypatch, two_short_reads):
    env = setup()
    svc, provider, _ = await start(env)
    source = env.reader._source
    end = dict(source.oms.latest)
    targets = ["oms.log", "schwab-1m-v2.log"] if two_short_reads else ["oms.log"]
    names = {(env.directory / name).stat().st_ino: name for name in targets}
    shortened = []
    original = os.pread

    def pread(fd, size, offset):
        name = names.get(os.fstat(fd).st_ino)
        if name and name not in shortened:
            shortened.append(name)
            (env.directory / name).write_text("")
        return original(fd, size, offset)

    monkeypatch.setattr(os, "pread", pread)
    before = env.client.xrevrange.call_count
    await svc._warning_monitor_once()
    assert shortened == targets and env.client.xrevrange.call_count == before + 2
    assert source.oms.latest == end
    if two_short_reads:
        assert svc._warning_revert_reason is None and svc._warning_active
        assert svc.warning_metrics()["pause_state"]["state"] == "active"
        assert svc.warning_metrics()["watchdog_observed_reason"] == "monitor_unreadable"
        assert provider._ws.removed == []
        await three_clean_native_reads(svc, env)
        assert env.client.xrevrange.call_count == before + 5  # One bounded retry, then 3 clean reads.
        assert provider._ws.added.count("A.*") == 1
        assert env.reader.log_observability()["coverage_status"] == "GAP_UNOBSERVED_COPYTRUNCATE"
    else:
        assert svc._warning_active and svc._warning_revert_reason is None
        assert env.reader.log_observability()["coverage_status"] == "GAP_UNOBSERVED_COPYTRUNCATE"
        assert provider._ws.removed == []
