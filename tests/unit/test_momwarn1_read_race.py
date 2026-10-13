"""Native read_live replay; recorded bytes, CONTROLLED scheduling/proc/state only."""

import os
from pathlib import Path
import subprocess
import sys
import traceback
from types import ModuleType

import pytest

from tests.unit.test_momwarn1_copytruncate import start
from tests.unit.test_momwarn1_live_health import NS, line, setup as live_setup
from project_mai_tai.market_data import momentum_warning_watchdog as reader_module
from project_mai_tai.momwarn_log_cursor import LogReadRace

setup = live_setup
FIXTURES = Path(__file__).parents[1] / "fixtures" / "momwarn1_revert2"
BASE = 1791886311 * NS + 50_000_000
REVERT = 1791886325 * NS + 730_000_000
LAST_END = 1791886311 * NS + 48_000_000


def recorded(env, *, until="2026-10-13 10:11:51,050"):
    for name in ("oms", "schwab-1m-v2", "strategy"):
        raw = (FIXTURES / f"{name}.slice.log").read_text()
        (env.directory / f"{name}.log").write_text("".join(
            row for row in raw.splitlines(keepends=True) if row[:23] <= until))


def version_module(head, path, monkeypatch):
    source = subprocess.check_output(["git", "show", f"{head}:src/project_mai_tai/{path}.py"],
                                     text=True)
    module = ModuleType(f"controlled_{head}_{path.replace('/', '_')}")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, f"{head}:src/project_mai_tai/{path}.py", "exec"), module.__dict__)
    return module


def original_reader(env, monkeypatch, head):
    contract = version_module(head, "momwarn_health_contract", monkeypatch)
    live = version_module(head, "momwarn_live_health", monkeypatch)
    watchdog = version_module(head, "market_data/momentum_warning_watchdog", monkeypatch)
    live.OmsLogMonitor = contract.OmsLogMonitor
    live.proposed_health_v2 = watchdog.proposed_health_v2 = contract.proposed_health_v2
    source = live.LiveWarningHealthSource(env.cfg, env.directory, proc=env.proc,
                                        wall_ns=lambda: env.clock.ns)
    reader = watchdog.WarningHealthReader(str(env.config), str(env.directory))
    original_open = reader._open_regular
    reader._open_regular = lambda path: original_open(
        env.mem if path == Path("/proc/meminfo") else path)
    reader._source, reader._source_config = source, dict(env.cfg)
    monkeypatch.setattr("project_mai_tai.momwarn_live_health.LiveWarningHealthSource",
                        lambda *args: source)
    return reader


def append_after_read(monkeypatch, path, text, *, times=1):
    original = os.read
    inode = path.stat().st_ino
    calls = []

    def read(fd, size):
        raw = original(fd, size)
        if os.fstat(fd).st_ino == inode:
            calls.append(len(raw))
            if len(calls) <= times:
                with path.open("a") as output:
                    output.write(text)
        return raw

    monkeypatch.setattr(os, "read", read)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("head", ["74a83042", "39fc287e"])
@pytest.mark.parametrize("mid_read", [False, True])
async def test_exact_original_native_slice_replay_inflight_vs_append_race(setup, monkeypatch, head, mid_read):
    env = setup(now=BASE)
    recorded(env)
    reader = original_reader(env, monkeypatch, head)
    baseline = await reader.read_live(env.client, "test")
    assert baseline.oms_sync_seq == 1402
    env.clock.ns = REVERT
    row = next(row for row in (FIXTURES / "oms.slice.log").read_text().splitlines(True)
               if "id=1403 phase=start" in row)
    if mid_read:
        append_after_read(monkeypatch, env.directory / "oms.log", row)
        with pytest.raises(ValueError, match="OMS log changed beyond read coverage") as caught:
            await reader.read_live(env.client, "test")
        frame = traceback.extract_tb(caught.value.__traceback__)[-1]
        print(f"CONTROLLED_REPLAY head={head} exception={type(caught.value).__name__} "
              f"message={caught.value} file={frame.filename} line={frame.lineno}")
    else:
        with (env.directory / "oms.log").open("a") as output:
            output.write(row)
        health = await reader.read_live(env.client, "test")
        assert health.oms_sync_seq == 1402 and health.oms_sync_seconds == .520346
        assert reader._source.oms.latest["oms_measured_wall_ns"] == LAST_END


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["oms.log", "schwab-1m-v2.log"])
async def test_native_append_is_deferred_without_retry_or_restamping(setup, monkeypatch, name):
    env = setup(now=BASE)
    recorded(env)
    await env.reader.read_live(env.client, "test")
    env.clock.ns = REVERT
    calls = append_after_read(monkeypatch, env.directory / name,
                             line(REVERT, "CONTROLLED append during bounded read"))
    health = await env.reader.read_live(env.client, "test")
    assert len(calls) == 1 and env.client.xrevrange.call_count == 2
    assert health.oms_sync_seq == 1402 and health.oms_sync_seconds == .520346
    assert env.source.oms.latest["oms_measured_wall_ns"] == LAST_END
    assert health.observed_wall_ns == REVERT and health.v2_not_applicable
    source = env.source.oms if name == "oms.log" else env.source
    cursor = source.position if name == "oms.log" else source.offset
    assert cursor < (env.directory / name).stat().st_size
    await env.reader.read_live(env.client, "test")
    cursor = source.position if name == "oms.log" else source.offset
    assert len(calls) == 2 and cursor == (env.directory / name).stat().st_size


@pytest.mark.asyncio
async def test_two_source_appends_are_normal_no_retry_or_revert(setup, monkeypatch):
    env = setup()
    svc, provider, _ = await start(env)
    original = os.read
    calls = []
    names = {name: (env.directory / name).stat().st_ino
             for name in ("oms.log", "schwab-1m-v2.log")}

    def read(fd, size):
        raw = original(fd, size)
        name = next((name for name, inode in names.items() if inode == os.fstat(fd).st_ino), None)
        if (name == "oms.log" and not calls
                or name == "schwab-1m-v2.log" and len(calls) == 1):
            calls.append(name)
            with (env.directory / name).open("a") as output:
                output.write(line(env.clock.ns, "CONTROLLED second distinct-source race"))
        return raw

    monkeypatch.setattr(os, "read", read)
    before = env.client.xrevrange.call_count
    await svc._warning_monitor_once()
    assert calls == ["oms.log", "schwab-1m-v2.log"]
    assert env.client.xrevrange.call_count == before + 1
    assert svc._warning_revert_reason is None and svc._warning_active
    assert provider._ws.removed == []
    assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}


@pytest.mark.asyncio
async def test_ERROR_appended_before_error_cursor_read_is_caught_same_sample(setup, monkeypatch):
    env = setup()
    svc, provider, _ = await start(env)
    calls = append_after_read(monkeypatch, env.directory / "oms.log", "ERROR actual observed failure\n")
    await svc._warning_monitor_once()
    assert len(calls) == 1 and svc._warning_revert_reason == "live_error"
    assert provider._ws.removed == ["A.*"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["permission", "config", "identity", "backlog", "sequence", "stale"])
async def test_nonrace_failures_are_never_retried(setup, monkeypatch, failure):
    env = setup()
    svc, provider, _ = await start(env)
    if failure == "permission":
        def denied():
            raise PermissionError("CONTROLLED no read permission")
        monkeypatch.setattr(env.reader._source.oms, "read", denied)
    elif failure == "config":
        env.config.write_text("{}")
    elif failure == "identity":
        path = env.proc / "12" / "stat"
        path.write_text(path.read_text().rsplit(" ", 1)[0] + " 999")
    elif failure == "backlog":
        with (env.directory / "oms.log").open("a") as output:
            output.write("X" * 65537)
    elif failure == "sequence":
        with (env.directory / "oms.log").open("a") as output:
            output.write(line(env.clock.ns,
                "[OMS-BROKER-SYNC-PASS] id=12 phase=end outcome=ok duration_ms=1000.000"))
    else:
        env.clock.ns += 20 * NS
    before = env.client.xrevrange.call_count
    await svc._warning_monitor_once()
    assert env.client.xrevrange.call_count - before == (0 if failure == "config" else 1)
    assert svc._warning_revert_reason == "monitor_unreadable" and provider._ws.removed == ["A.*"]


@pytest.mark.asyncio
async def test_repeated_same_source_append_never_retries_or_latches(setup, monkeypatch):
    env = setup()
    svc, provider, _ = await start(env)
    calls = append_after_read(monkeypatch, env.directory / "oms.log", "INFO race\n", times=3)
    await svc._warning_monitor_once()
    assert len(calls) == 1 and svc._warning_revert_reason is None and svc._warning_active
    await svc._warning_monitor_once()
    assert len(calls) == 2 and provider._ws.removed == [] and svc._warning_active
    assert provider._subscriptions == {"WFF"} and provider._warning_trade_symbols == {"NEW"}


@pytest.mark.asyncio
async def test_midread_growth_past_bound_is_backlog_not_retriable_race(setup, monkeypatch):
    env = setup()
    svc, provider, _ = await start(env)
    calls = append_after_read(monkeypatch, env.directory / "oms.log", "X" * 65537)
    await svc._warning_monitor_once()
    assert len(calls) == 1 and svc._warning_revert_reason == "monitor_unreadable"
    assert provider._ws.removed == ["A.*"]


@pytest.mark.asyncio
async def test_inflight_keeps_original_completion_only_through_unchanged_age20s(setup):
    env = setup(now=BASE)
    recorded(env)
    await env.reader.read_live(env.client, "test")
    with (env.directory / "oms.log").open("a") as output:
        output.write(next(row for row in (FIXTURES / "oms.slice.log").read_text().splitlines(True)
                          if "id=1403 phase=start" in row))
    env.clock.ns = LAST_END + 20 * NS
    assert (await env.reader.read_live(env.client, "test")).oms_sync_seq == 1402
    assert env.source.oms.latest["oms_measured_wall_ns"] == LAST_END
    env.clock.ns += 1
    with pytest.raises(ValueError, match="OMS cadence"):
        await env.reader.read_live(env.client, "test")


@pytest.mark.asyncio
async def test_recorded_partial_end_is_not_completed_until_actual_newline(setup):
    env = setup(now=BASE)
    recorded(env)
    await env.reader.read_live(env.client, "test")
    row = next(row for row in (FIXTURES / "oms.slice.log").read_text().splitlines(True)
               if "id=1403 phase=end" in row)
    env.clock.ns = REVERT
    with (env.directory / "oms.log").open("a") as output:
        output.write(row[:45])
    assert (await env.reader.read_live(env.client, "test")).oms_sync_seq == 1402
    env.clock.ns = 1791886326 * NS + 93_000_000
    with (env.directory / "oms.log").open("a") as output:
        output.write(row[45:])
    health = await env.reader.read_live(env.client, "test")
    assert health.oms_sync_seq == 1403 and health.oms_sync_seconds == .537179
    assert env.source.oms.latest["oms_measured_wall_ns"] == env.clock.ns


@pytest.mark.asyncio
async def test_error_seen_before_later_cursor_race_is_not_consumed_or_lost(setup, monkeypatch):
    env = setup()
    svc, provider, _ = await start(env)
    with (env.directory / "oms.log").open("a") as output:
        output.write("ERROR actual earlier-file failure\n")
    target = env.directory / "strategy.log"
    with target.open("a") as output:
        output.write("INFO new bytes\n")
    original = reader_module.cursor_anchor
    inode, size = target.stat().st_ino, target.stat().st_size
    calls = []

    def anchor(fd, offset):
        if os.fstat(fd).st_ino == inode and offset == size and not calls:
            calls.append(offset)
            target.write_text("")
        return original(fd, offset)

    monkeypatch.setattr(reader_module, "cursor_anchor", anchor)
    await svc._warning_monitor_once()
    assert calls == [size] and svc._warning_revert_reason == "live_error"
    assert provider._ws.removed == ["A.*"]
    assert env.reader.log_observability()["copytruncate_counts"] == {"strategy.log": 1}


@pytest.mark.asyncio
async def test_final_anchor_race_does_not_commit_OMS_completion_or_offset(setup, monkeypatch):
    env = setup()
    await env.reader.read_live(env.client, "test")
    prior = (env.source.oms.position, dict(env.source.oms.latest), env.source.oms.partial)
    with (env.directory / "oms.log").open("a") as output:
        output.write(line(env.clock.ns,
            "[OMS-BROKER-SYNC-PASS] id=11 phase=end outcome=ok duration_ms=1000.000"))
    def race(*args):
        raise LogReadRace("CONTROLLED final anchor race")
    monkeypatch.setattr("project_mai_tai.momwarn_health_contract.cursor_anchor", race)
    with pytest.raises(LogReadRace):
        env.source.oms.read()
    assert (env.source.oms.position, env.source.oms.latest, env.source.oms.partial) == prior
