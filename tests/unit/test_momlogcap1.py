"""Offline watchdog catch-up controls; timing helper is main-only, never pytest."""

from pathlib import Path

import pytest

from project_mai_tai.market_data.momwarn_pause_policy import WarningPausePolicy
from project_mai_tai.market_data.momentum_warning_watchdog import WarningHealthReader, WarningWatchdog
from tests.unit.test_momwarn1_gateway import health_reader as reader_fixture

health_reader = reader_fixture
ACTUAL_PREFIX = Path("/Users/velkris/.codex/task-queues/analysis/momwarn1-wed-no-warnings-20261014/v2-log-prefix-0525.txt")
RECORDED_BURSTS = (
    ("v2-real-peak-20261013T080643.txt", 982821, 5334,
     "26e2af2abefa343205cc66708da83268b0450fef6790a1c4b8a28a1dac5a8b93", "peak_second_filter"),
    ("v2-real-GRML-20261014T090720-21.txt", 399106, 953,
     "bccff8b4cd148fe0f84bdaf38eab8e8e5e98514e276cf1d37415127a9782a831", "full_GRML_two_second_filter"),
    ("v2-real-GRML-20261014T090720.txt", 360041, 860,
     "f493b759912726d355269de4dc310a3ec29dbe23e8c9dbc530b34d8795dd4b9d", "partial_GRML_single_second_filter"),
    ("v2-real-GRML-20261014T090719-20.txt", 360331, 861,
     "e76b385fd7e8c6899202aa4a2d5dcc0288f68cd38d1296f776112b548337d57e", "partial_GRML_two_second_filter"),
)


def recorded_burst(artifact):
    import hashlib

    name, size, lines, digest, _ = artifact
    raw = ACTUAL_PREFIX.with_name(name).read_bytes()
    assert len(raw) == size and raw.count(b"\n") == lines
    assert hashlib.sha256(raw).hexdigest() == digest
    return raw


def catch_up(reader, policy, *, limit=12):
    for _ in range(limit):
        try:
            health = reader.read()
        except ValueError:
            policy.trip("monitor_unreadable")
        else:
            reason = WarningWatchdog().check_recoverable(health, publish_lag_seconds=0)
            if reason:
                policy.trip(reason)
            else:
                policy.clean()
        if policy.state in {"active", "off"}:
            break
    assert policy.state == "active"


def burst_control(reader, directory, *, size=None):
    reader.read()
    policy = WarningPausePolicy()
    for _ in range(3):
        policy.clean()
    path = directory / "strategy.log"
    before = reader._logs[path.name][1]
    if size is None:
        size = reader.MAX_LOG_PASS_BYTES + 402 * 1024
    with path.open("ab") as stream:
        stream.write(b"INFO replay\n" * (size // 12))
    with pytest.raises(ValueError, match="catch-up incomplete"):
        reader.read()
    policy.trip("monitor_unreadable")
    assert reader._logs[path.name][1] - before == reader.MAX_LOG_PASS_BYTES
    assert not policy.clean_streak
    for index in range(3):
        assert not reader.read().new_live_error
        policy.clean()
        assert (policy.state == "active") is (index == 2)
    assert reader._logs[path.name][1] == path.stat().st_size


def test_over_budget_pause_then_three_clean_resume(health_reader):
    reader, _, directory = health_reader
    burst_control(reader, directory)


def test_historical_256kb_budget_402kb_pause_then_three_clean_resume(health_reader, monkeypatch):
    reader, _, directory = health_reader
    monkeypatch.setattr(reader, "MAX_LOG_PASS_BYTES", 4 * reader.MAX_LOG_BYTES)
    burst_control(reader, directory, size=402 * 1024)


@pytest.mark.parametrize("size", [402 * 1024, 982809, 1024 * 1024, 2 * 1024 * 1024])
def test_observed_burst_sizes_clear_one_pass_without_pause(health_reader, size):
    reader, _, directory = health_reader
    reader.read()
    policy = WarningPausePolicy()
    for _ in range(3):
        policy.clean()
    path = directory / "schwab-1m-v2.log"
    with path.open("ab") as stream:
        stream.write(b"INFO replay\n" * (size // 12) + b" " * (size % 12))
    assert not reader.read().new_live_error
    policy.clean()
    assert policy.state == "active" and policy.wire_pause_count == 0
    assert reader._logs[path.name][1] == path.stat().st_size


def test_real_19mb_copy_cursor_1mb_behind_catches_up_one_pass(health_reader):
    import hashlib
    from project_mai_tai.momwarn_log_cursor import cursor_anchor

    raw = ACTUAL_PREFIX.read_bytes()
    assert len(raw) == 19022548
    assert hashlib.sha256(raw).hexdigest() == "6cbecdd9ec5c47e43ef0702f5aea1d63bcded0d110ba8b7432b340095ba5bd59"
    reader, _, directory = health_reader
    path = directory / "schwab-1m-v2.log"
    path.write_bytes(raw)
    reader.read()
    offset = len(raw) - 1024 * 1024
    with path.open("rb") as stream:
        reader._log_anchors[path.name] = cursor_anchor(stream.fileno(), offset)
    reader._logs[path.name] = (path.stat().st_ino, offset, b"")
    assert not reader.read().new_live_error
    assert reader._logs[path.name][1] == len(raw)


def test_partial_GRML_090720_timestamp_slice_clears_without_pause(health_reader):
    raw = b"".join(line for line in ACTUAL_PREFIX.read_bytes().splitlines(keepends=True)
                   if line.startswith(b"2026-10-14 09:07:20,"))
    assert 350000 <= len(raw) <= 500000
    reader, _, directory = health_reader
    reader.read()
    policy = WarningPausePolicy()
    for _ in range(3):
        policy.clean()
    path = directory / "schwab-1m-v2.log"
    with path.open("ab") as stream:
        stream.write(raw)
    assert not reader.read().new_live_error
    policy.clean()
    assert policy.state == "active" and policy.wire_pause_count == 0
    assert reader._logs[path.name][1] == path.stat().st_size


@pytest.mark.parametrize("artifact", RECORDED_BURSTS, ids=[a[4] for a in RECORDED_BURSTS])
def test_recorded_burst_bytes_clear_one_pass_zero_pauses(health_reader, artifact):
    raw = recorded_burst(artifact)
    reader, _, directory = health_reader
    reader.read()
    policy = WarningPausePolicy()
    for _ in range(3):
        policy.clean()
    path = directory / "schwab-1m-v2.log"
    before = reader._logs[path.name][1]
    with path.open("ab") as stream:
        stream.write(raw)
    health = reader.read()
    assert WarningWatchdog().check_recoverable(health, publish_lag_seconds=0) is None
    policy.clean()
    assert policy.state == "active" and policy.wire_pause_count == 0
    assert reader._logs[path.name][1] == before + len(raw) == path.stat().st_size


@pytest.mark.parametrize("artifact", RECORDED_BURSTS[:2], ids=[a[4] for a in RECORDED_BURSTS[:2]])
def test_injected_ERROR_inside_recorded_burst_is_sticky(health_reader, artifact):
    raw = recorded_burst(artifact)
    mid = len(raw) // 2
    reader, _, directory = health_reader
    reader.retain_cancelled_harm = True
    reader.read()
    with (directory / "schwab-1m-v2.log").open("ab") as stream:
        stream.write(raw[:mid] + b"\nERROR CONTROLLED injected harm\n" + raw[mid:])
    watchdog = WarningWatchdog()
    assert watchdog.check_recoverable(reader.read(), publish_lag_seconds=0) == "live_error"
    assert reader.read().new_live_error
    reader.reset_window()
    assert reader.read().new_live_error


def test_old_64kb_plus_one_and_huge_line_converge(health_reader):
    reader, _, directory = health_reader
    reader.read()
    with (directory / "strategy.log").open("ab") as stream:
        stream.write(b" " * (reader.MAX_LOG_BYTES + 1))
    assert not reader.read().new_live_error
    assert reader._logs["strategy.log"][1] == (directory / "strategy.log").stat().st_size
    assert len(reader._logs["strategy.log"][2]) == 6


def test_continuous_growth_cannot_starve_any_error_cursor(health_reader):
    reader, _, directory = health_reader
    reader.read()
    offsets = {name: reader._logs[name][1] for name in reader.LOG_NAMES}
    for _ in range(len(reader.LOG_NAMES)):
        for name in reader.LOG_NAMES:
            with (directory / name).open("ab") as stream:
                stream.write(b" " * reader.MAX_LOG_PASS_BYTES)
        with pytest.raises(ValueError, match="catch-up incomplete"):
            reader.read()
    assert all(reader._logs[name][1] > offsets[name] for name in reader.LOG_NAMES)


def test_error_at_partial_chunk_end_is_conservative_harm(health_reader):
    reader, _, directory = health_reader
    reader.retain_cancelled_harm = True
    reader.read()
    with (directory / "strategy.log").open("ab") as stream:
        stream.write(b" " * (reader.MAX_LOG_BYTES - 5) + b"ERRORx\n")
    # An incomplete ERROR is already fail-closed in the original reader.
    assert reader.read().new_live_error


def test_large_append_after_snapshot_commits_scanned_chunk_and_defers_bytes(health_reader, monkeypatch):
    from tests.unit.test_momwarn1_append_growth import ReadTap

    reader, _, directory = health_reader
    reader.read()
    path = directory / "strategy.log"
    before = reader._logs[path.name][1]
    with path.open("ab") as stream:
        stream.write(b"INFO scanned\n")
    original = reader._open_regular
    writes = []

    def open_regular(open_path):
        source = original(open_path)
        if open_path != path:
            return source

        def after(_):
            if not writes:
                writes.append(True)
                with path.open("ab") as stream:
                    stream.write(b" " * 100000 + b" ERROR deferred\n")
        return ReadTap(source, after)

    monkeypatch.setattr(reader, "_open_regular", open_regular)
    assert not reader.read().new_live_error
    assert reader._logs[path.name][1] == before + len(b"INFO scanned\n")
    assert reader.read().new_live_error


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [False, True], ids=["START_snapshot", "restore_growth_failure"])
async def test_l_real_GRML_thread_150ms_during_shielded_read_zero_pauses(health_reader, monkeypatch, mutation):
    import asyncio
    import threading
    import time
    from tests.unit.test_momwarn1_append_growth import ReadTap

    raw = recorded_burst(RECORDED_BURSTS[1])
    reader, _, directory = health_reader
    reader.read()
    policy = WarningPausePolicy()
    for _ in range(3):
        policy.clean()
    path = directory / "schwab-1m-v2.log"
    first, _, remaining = raw.partition(b"\n")
    with path.open("ab") as stream:
        stream.write(first + b"\n")
    release, finished = threading.Event(), threading.Event()
    original = reader._open_regular
    tapped = []

    def writer():
        assert release.wait(1)
        pieces = 3
        for index in range(pieces):
            time.sleep(.05)
            with path.open("ab") as stream:
                stream.write(remaining[len(remaining)*index//pieces:len(remaining)*(index+1)//pieces])
        finished.set()

    def open_regular(open_path):
        source = original(open_path)
        if open_path != path:
            return source

        def after(_):
            if not tapped:
                tapped.append(True)
                release.set()
                assert finished.wait(1)
        return ReadTap(source, after)

    monkeypatch.setattr(reader, "_open_regular", open_regular)
    if mutation:
        scan = reader._scan_live_logs

        def old_growth_check():
            result = scan()
            if path.stat().st_size - reader._logs[path.name][1] > reader.MAX_LOG_BYTES:
                raise ValueError("live log catch-up incomplete; evidence unknown")
            return result
        monkeypatch.setattr(reader, "_scan_live_logs", old_growth_check)
    producer = threading.Thread(target=writer)
    producer.start()

    async def positive():
        try:
            health = await asyncio.wait_for(asyncio.shield(asyncio.create_task(asyncio.to_thread(reader.read))), 1)
        except ValueError:
            policy.trip("monitor_unreadable")
        else:
            assert not health.new_live_error
            policy.clean()
        assert policy.state == "active" and policy.wire_pause_count == 0
        await asyncio.sleep(1)  # The next actual one-second poll consumes deferred bytes.
        assert not (await asyncio.to_thread(reader.read)).new_live_error
        assert reader._logs[path.name][1] == path.stat().st_size

    try:
        if mutation:
            with pytest.raises(AssertionError):
                await positive()
        else:
            await positive()
    finally:
        release.set()
        await asyncio.to_thread(producer.join, 1)
        assert not producer.is_alive() and finished.is_set()


def test_raise_without_advance_mutation_kills_positive_control(health_reader, monkeypatch):
    reader, _, directory = health_reader
    original = reader._scan_live_logs

    def mutant():
        for name, (_, offset, _) in reader._logs.items():
            if (directory / name).stat().st_size - offset > reader.MAX_LOG_BYTES:
                raise ValueError("live log catch-up incomplete; evidence unknown")
        return original()

    monkeypatch.setattr(reader, "_scan_live_logs", mutant)
    with pytest.raises(AssertionError):
        burst_control(reader, directory)


@pytest.mark.parametrize("position", [10, 65533, 262141, 350000, 2097149])
def test_error_inside_burst_and_chunk_or_pass_split_stays_sticky(health_reader, position):
    reader, _, directory = health_reader
    reader.retain_cancelled_harm = True
    reader.read()
    with (directory / "strategy.log").open("ab") as stream:
        stream.write(b" " * position + b"ERROR harm\n" + b" " * 402000 + b"\n")
    policy = WarningPausePolicy()
    with pytest.raises(AssertionError):
        catch_up(reader, policy)
    assert policy.state == "off" and policy.reason == "live_error"
    assert reader.read().new_live_error
    reader.reset_window()
    assert reader.read().new_live_error


@pytest.mark.parametrize("token", [b"ERROR", b"xERROR", b"ERRORx"])
def test_huge_line_bounded_tail_and_word_boundary(health_reader, token):
    reader, _, directory = health_reader
    reader.read()
    with (directory / "strategy.log").open("ab") as stream:
        stream.write(b" " * (reader.MAX_LOG_PASS_BYTES - 3) + token + b" " * 400000)
    found = False
    for _ in range(4):
        try:
            found |= reader.read().new_live_error
        except ValueError as exc:
            assert "catch-up incomplete" in str(exc)
        assert len(reader._logs["strategy.log"][2]) <= reader.MAX_LOG_BYTES
    assert found is (token == b"ERROR")
    assert reader._logs["strategy.log"][1] == (directory / "strategy.log").stat().st_size


def test_consumed_error_survives_later_file_failure(health_reader):
    reader, _, directory = health_reader
    reader.retain_cancelled_harm = True
    reader.read()
    with (directory / "oms.log").open("ab") as stream:
        stream.write(b"ERROR harm\n")
    path = directory / "orb-schwab.log"
    path.rename(directory / "saved.log")
    with pytest.raises(FileNotFoundError):
        reader.read()
    (directory / "saved.log").rename(path)
    assert reader.read().new_live_error


def timing_helper(prefix):
    """Main exclusively runs: python tests/unit/test_momlogcap1.py PREFIX."""
    import hashlib
    import json
    import tempfile
    import time

    path = Path(prefix)
    raw = path.read_bytes()
    assert len(raw) == 19022548
    assert hashlib.sha256(raw).hexdigest() == "6cbecdd9ec5c47e43ef0702f5aea1d63bcded0d110ba8b7432b340095ba5bd59"
    rows = []
    for size in (402 * 1024, 982809, 1024 * 1024, 2 * 1024 * 1024, len(raw)):
        with tempfile.TemporaryDirectory(prefix="momlogcap1-offline-") as temporary:
            directory = Path(temporary)
            reader = WarningHealthReader("unused", temporary)
            for name in reader.LOG_NAMES:
                (directory / name).touch()
            reader._scan_live_logs()
            (directory / "schwab-1m-v2.log").write_bytes(raw[:size])
            passes = []
            for _ in range(100):
                started = time.perf_counter()
                try:
                    error = reader._scan_live_logs()
                    complete = True
                except ValueError as exc:
                    assert "catch-up incomplete" in str(exc)
                    complete, error = False, reader._pending_live_error
                elapsed = time.perf_counter() - started
                offset = reader._logs["schwab-1m-v2.log"][1]
                passes.append(dict(seconds=elapsed, offset=offset, complete=complete, error=error))
                if complete:
                    break
            assert complete and offset == size
            rows.append(dict(bytes=size, budget=reader.MAX_LOG_PASS_BYTES, passes=passes,
                             max_pass_seconds=max(p["seconds"] for p in passes),
                             scanner_only=True, full_live_read_qualification=False))
    with tempfile.TemporaryDirectory(prefix="momlogcap1-tail-offline-") as temporary:
        directory = Path(temporary)
        reader = WarningHealthReader("unused", temporary)
        for name in reader.LOG_NAMES:
            (directory / name).touch()
        path = directory / "schwab-1m-v2.log"
        path.write_bytes(raw)
        reader._scan_live_logs()
        from project_mai_tai.momwarn_log_cursor import cursor_anchor
        offset = len(raw) - 1024 * 1024
        with path.open("rb") as stream:
            reader._log_anchors[path.name] = cursor_anchor(stream.fileno(), offset)
        reader._logs[path.name] = (path.stat().st_ino, offset, b"")
        started = time.perf_counter()
        error = reader._scan_live_logs()
        elapsed = time.perf_counter() - started
        assert reader._logs[path.name][1] == len(raw)
        rows.append(dict(scenario="real_19mb_cursor_1mb_behind", bytes=1024 * 1024,
                         budget=reader.MAX_LOG_PASS_BYTES, passes=1, seconds=elapsed,
                         final_offset=len(raw), error=error, scanner_only=True,
                         full_live_read_qualification=False))
    for artifact in RECORDED_BURSTS:
        burst = recorded_burst(artifact)
        with tempfile.TemporaryDirectory(prefix="momlogcap1-recorded-offline-") as temporary:
            directory = Path(temporary)
            reader = WarningHealthReader("unused", temporary)
            for name in reader.LOG_NAMES:
                (directory / name).touch()
            reader._scan_live_logs()
            path = directory / "schwab-1m-v2.log"
            path.write_bytes(burst)
            started = time.perf_counter()
            error = reader._scan_live_logs()
            elapsed = time.perf_counter() - started
            assert reader._logs[path.name][1] == len(burst)
            rows.append(dict(scenario=artifact[4], source=artifact[0], bytes=len(burst),
                             lines=artifact[2], sha256=artifact[3],
                             budget=reader.MAX_LOG_PASS_BYTES, passes=1, seconds=elapsed,
                             error=error, scanner_only=True, full_live_read_qualification=False,
                             filtered_timestamp_slice_not_contiguous_prefix=True))
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    import sys
    timing_helper(sys.argv[1])
