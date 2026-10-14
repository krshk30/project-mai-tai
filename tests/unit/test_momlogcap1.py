"""Offline watchdog catch-up controls; timing helper is main-only, never pytest."""

from pathlib import Path

import pytest

from project_mai_tai.market_data.momwarn_pause_policy import WarningPausePolicy
from project_mai_tai.market_data.momentum_warning_watchdog import WarningHealthReader, WarningWatchdog
from tests.unit.test_momwarn1_gateway import health_reader as reader_fixture

health_reader = reader_fixture


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


def burst_control(reader, directory):
    reader.read()
    policy = WarningPausePolicy()
    for _ in range(3):
        policy.clean()
    path = directory / "strategy.log"
    before = reader._logs[path.name][1]
    with path.open("ab") as stream:
        stream.write(b"INFO replay\n" * (402 * 1024 // 12))
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


def test_402kb_pause_then_three_clean_resume(health_reader):
    reader, _, directory = health_reader
    burst_control(reader, directory)


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
    with pytest.raises(ValueError, match="catch-up incomplete"):
        reader.read()
    assert reader._logs[path.name][1] == before + len(b"INFO scanned\n")
    assert reader.read().new_live_error


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


@pytest.mark.parametrize("position", [10, 65533, 262141, 350000])
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


def test_native_v2_backlog_is_separate_unmodified_guard():
    # This build must not claim native probe convergence from the ERROR scanner.
    import inspect
    from project_mai_tai.momwarn_live_health import LiveWarningHealthSource

    assert 'raise ValueError("v2 log truncation/backlog")' in inspect.getsource(
        LiveWarningHealthSource._read_probes)


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
    for size in (1024 * 1024, len(raw)):
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
            rows.append(dict(bytes=size, passes=passes, max_pass_seconds=max(p["seconds"] for p in passes),
                             scanner_only=True, full_live_read_qualification=False))
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    import sys
    timing_helper(sys.argv[1])
