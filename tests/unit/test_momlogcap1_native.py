"""Native cursor controls; offline chain helper is run only in main's CPU slot."""

import hashlib
import inspect
from pathlib import Path
from threading import Event, Thread
import time

import pytest

from project_mai_tai import momwarn_live_health as live
from project_mai_tai.momwarn_cohort_lateness import CohortTracker
from tests.unit import test_momwarn1_live_health as controlled
from tests.unit.test_momwarn1_live_health import NS, NOW, line

setup = controlled.setup


def append(env, raw):
    with env.source.log.open("ab") as output:
        output.write(raw)


def baseline(env):
    env.source._read_probes(env.clock.ns)
    return env.source.offset


def burst(size, at=NOW):
    row = line(at, "[V2-ATR-BAR-GAP] controlled replay filler " + "x" * 350).encode()
    return row * (size // len(row)) + b"x" * (size % len(row) - 1) + b"\n"


def probe(symbol="NEW", at=NOW):
    minute = NOW // (60 * NS) * 60_000 - 60_000
    return line(at, f"[V2-MACD-PROBE] sym={symbol} ts_ms={minute} close=10 ").encode()


@pytest.mark.parametrize("size", [402_000, 982_809, 2 * 1024 * 1024])
def test_burst_clears_one_pass_without_pause_and_retains_probe(setup, size):
    env = setup()
    before = baseline(env)
    data = burst(size // 2) + probe() + burst(size // 2 - len(probe()))
    append(env, data)
    env.source._read_probes(NOW)
    assert env.source.offset == before + len(data)
    assert ("NEW", NOW // (60 * NS) * 60_000 - 60_000) in env.source.bars


def test_over_budget_commits_every_chunk_and_converges(setup):
    env = setup()
    before = baseline(env)
    append(env, probe() + burst(2 * live.MAX_LOG_PASS_BYTES + 500))
    offsets = []
    for _ in range(3):
        try:
            env.source._read_probes(NOW)
        except ValueError as exc:
            assert "catch-up incomplete" in str(exc)
        offsets.append(env.source.offset)
    assert offsets[:2] == [before + 2097152, before + 4194304]
    assert offsets[2] == env.source.log.stat().st_size
    assert ("NEW", NOW // (60 * NS) * 60_000 - 60_000) in env.source.bars
    assert len(env.source.partial) <= live.BOUND


def test_end_only_commit_mutation_fails_progress_control(setup):
    env = setup()
    before = baseline(env)
    append(env, burst(live.MAX_LOG_PASS_BYTES + 1))
    original = inspect.getsource(type(env.source)._read_probes)
    source = original.replace("    def _read_probes", "def mutated", 1)
    source = "\n".join(row[4:] if row.startswith("    ") else row for row in source.splitlines())
    source = source.replace("self.identity, self.offset, self.partial = identity, covered, partial",
                            "self.identity, self.offset, self.partial = identity, self.offset, partial")
    namespace = dict(vars(live))
    exec(compile(source, "<end-only-commit-mutation>", "exec"), namespace)
    with pytest.raises(ValueError, match="catch-up incomplete"):
        namespace["mutated"](env.source, NOW)
    assert env.source.offset == before
    with pytest.raises(AssertionError):
        assert env.source.offset == before + 2097152


def test_later_invalid_chunk_preserves_only_previous_parsed_chunk(setup):
    env = setup()
    before = baseline(env)
    valid = probe("KEPT")
    first = valid + b"x" * (live.BOUND - len(valid) - 1) + b"\n"
    append(env, first + probe("UNCOMMITTED") + probe("FUTURE", NOW + NS))
    with pytest.raises(ValueError, match="future v2"):
        env.source._read_probes(NOW)
    assert env.source.offset == before + 65536
    assert {symbol for symbol, _ in env.source.bars} == {"AAA", "BBB", "KEPT"}
    assert env.source.anchor == b"x" * 255 + b"\n"


@pytest.mark.parametrize("marker", ["probe", "publication", "seed"])
def test_huge_line_advances_boundedly_and_preserves_actionable_evidence(setup, marker):
    env = setup()
    baseline(env)
    env.source.cohort = CohortTracker()
    minute = NOW // (60 * NS) * 60_000 - 60_000
    start = line(NOW, "").rstrip("\n").encode()
    if marker == "probe":
        body = b"[V2-MACD-PROBE] sym=HUGE ts_ms=" + str(minute).encode() + b" close=10 "
    elif marker == "publication":
        body = (b"[V2-LINE-RESTORE] sym=HUGE " + b"x " * live.BOUND
                + b"current_bar_ms=" + str(minute).encode() + b" "
                + b"x " * live.BOUND + b"outcome=published ")
    else:
        body = b"[V2-DB-SEED-GAP] HUGE "
    append(env, start + b"x" * (3 * live.MAX_LOG_PASS_BYTES) + b" " + body + b"\n")
    previous = env.source.offset
    for _ in range(5):
        try:
            env.source._read_probes(NOW)
        except ValueError as exc:
            assert "catch-up incomplete" in str(exc) or "partial line bound" in str(exc)
        assert env.source.offset > previous
        previous = env.source.offset
        assert len(env.source.partial) <= live.BOUND
        if env.source.long_line:
            assert len(env.source.long_line["tail"]) <= live.BOUND
        if previous == env.source.log.stat().st_size:
            break
    assert env.source.offset == env.source.log.stat().st_size
    env.source._read_probes(NOW)
    if marker == "seed":
        assert "HUGE" in env.source.cohort.seeds
    else:
        assert env.source.bars["HUGE", minute] == NOW


def test_first_read_is_only_eof_bound_and_drops_first_partial_line(setup):
    env = setup()
    raw = probe("OLD") + burst(300_000) + probe("TAIL")
    env.source.log.write_bytes(raw)
    env.source._read_probes(NOW)
    assert env.source.offset == len(raw)
    assert {symbol for symbol, _ in env.source.bars} == {"TAIL"}


@pytest.mark.parametrize("mode", ["shrink", "same_size", "regrow"])
def test_rewrite_resets_contiguous_baseline_once_and_parses_new_receipts(setup, mode):
    env = setup()
    baseline(env)
    old = env.source.offset
    raw = probe("REPLACED")
    if mode != "shrink":
        size = old if mode == "same_size" else old + 1000
        raw += b"x" * (size - len(raw) - 1) + b"\n"
    env.source.log.write_bytes(raw)
    env.source._read_probes(NOW)
    assert env.source.offset == len(raw)
    assert env.source.truncations == 1
    assert env.source.covered_from_ns == NOW
    assert ("REPLACED", NOW // (60 * NS) * 60_000 - 60_000) in env.source.bars
    env.source._read_probes(NOW)
    assert env.source.truncations == 1


def test_rotation_remains_unknown_without_cursor_advance(setup):
    env = setup()
    before = baseline(env)
    env.source.log.rename(env.source.log.with_suffix(".old"))
    env.source.log.write_bytes(probe())
    with pytest.raises(ValueError, match="rotated"):
        env.source._read_probes(NOW)
    assert env.source.offset == before


def test_map_cutoff_and_cap_remain_fail_closed(setup):
    env = setup()
    before = baseline(env)
    env.source.bars["OLD", NOW // 1_000_000 - 120_001] = NOW
    env.source._read_probes(NOW)
    assert "OLD" not in {symbol for symbol, _ in env.source.bars}
    saved = dict(env.source.bars)
    append(env, b"".join(probe(f"X{index}") for index in range(1025)))
    with pytest.raises(ValueError, match="map bound"):
        env.source._read_probes(NOW)
    assert len(env.source.bars) <= 1024
    assert env.source.offset >= before
    assert set(saved) <= set(env.source.bars)


@pytest.mark.parametrize("marker", ["probe", "publication", "seed"])
def test_future_markers_beyond_first_chunk_remain_unknown(setup, marker):
    env = setup()
    before = baseline(env)
    messages = {
        "probe": probe("FUTURE", NOW + NS),
        "publication": line(NOW + NS, "[V2-LINE-RESTORE] sym=AAA current_bar_ms=0 outcome=published").encode(),
        "seed": line(NOW + NS, "[V2-WARMUP-LINE] AAA").encode(),
    }
    append(env, b"x" * (live.BOUND - 1) + b"\n" + messages[marker])
    with pytest.raises(ValueError, match="future v2"):
        env.source._read_probes(NOW)
    assert env.source.offset == before + 65536


def test_seed_cohort_commit_survives_later_bad_chunk(setup):
    env = setup()
    baseline(env)
    env.source.cohort = CohortTracker()
    seed = line(NOW, "[V2-WARMUP-LINE] SEEDED").encode()
    append(env, seed + b"x" * (live.BOUND - len(seed) - 1) + b"\n" + probe("BAD", NOW + NS))
    with pytest.raises(ValueError, match="future v2"):
        env.source._read_probes(NOW)
    assert "SEEDED" in env.source.cohort.seeds


def test_long_line_utf8_and_marker_across_chunk_boundary(setup):
    env = setup()
    baseline(env)
    prefix = line(NOW, "").rstrip("\n").encode()
    minute = NOW // (60 * NS) * 60_000 - 60_000
    raw = (prefix + b"x" * (2 * live.BOUND - len(prefix) - 3)
           + "\u00e9".encode() + b" [V2-MACD-PROBE] sym=CROSS ts_ms="
           + str(minute).encode() + b" close=10 \n")
    append(env, raw)
    with pytest.raises(ValueError, match="partial line bound"):
        env.source._read_probes(NOW)
    assert env.source.offset == env.source.log.stat().st_size
    assert env.source.bars["CROSS", minute] == NOW


def test_unbounded_line_with_invalid_utf8_never_proves_completion(setup):
    env = setup()
    baseline(env)
    append(env, line(NOW, "").rstrip("\n").encode() + b"x" * live.BOUND + b"\xff" + probe())
    with pytest.raises(UnicodeError):
        env.source._read_probes(NOW)
    assert "NEW" not in {symbol for symbol, _ in env.source.bars}


def test_cal_malformed_bucket_after_valid_chunk_remains_fail_closed(setup):
    env = setup()
    before = baseline(env)
    env.source.cohort = CohortTracker()
    append(env, b"x" * (live.BOUND - 1) + b"\n"
           + line(NOW, "[V2-MACD-PROBE] sym=BAD ts_ms=123 close=10 ").encode())
    with pytest.raises(ValueError, match="CAL probe bucket is malformed"):
        env.source._read_probes(NOW)
    assert env.source.offset == before + 65536


def test_regular_growth_during_each_chunk_keeps_bounded_progress(setup, monkeypatch):
    env = setup()
    baseline(env)
    append(env, burst(402_000))
    original = live.os.read
    reads = []

    def growing_read(fd, requested):
        raw = original(fd, requested)
        reads.append(len(raw))
        append(env, probe("GROWTH"))
        return raw

    monkeypatch.setattr(live.os, "read", growing_read)
    env.source._read_probes(NOW)
    assert max(reads) <= live.BOUND
    assert env.source.offset < env.source.log.stat().st_size
    monkeypatch.setattr(live.os, "read", original)
    env.source._read_probes(NOW)
    assert "GROWTH" in {symbol for symbol, _ in env.source.bars}


def _real_grml_thread_control(setup, monkeypatch, *, mutated=False):
    path = Path("/Users/velkris/.codex/task-queues/analysis/momwarn1-wed-no-warnings-20261014/"
                "v2-real-GRML-20261014T090720-21.txt")
    if not path.exists():
        pytest.skip("retained real GRML packet required for reviewer control (l)")
    raw = path.read_bytes()
    assert len(raw) == 399_106
    assert hashlib.sha256(raw).hexdigest() == "bccff8b4cd148fe0f84bdaf38eab8e8e5e98514e276cf1d37415127a9782a831"
    # CONTROLLED UTC process/clock, before/after the unchanged retained body.
    now = 1_791_968_842 * NS  # 2026-10-14 09:07:22 UTC
    env = setup(now=now)
    before = baseline(env)
    rows = raw.splitlines(keepends=True)
    prefix = b"".join(rows[:20])
    append(env, prefix)
    started, finished, stop = Event(), Event(), Event()
    failures = []

    def writer():
        try:
            assert started.wait(3), "reader never reached the start snapshot"
            chunks = [b"".join(rows[index:index + 32]) for index in range(20, len(rows), 32)]
            start = time.monotonic()
            for index, chunk in enumerate(chunks):
                if stop.wait(max(0, start + 0.150 * (index + 1) / len(chunks) - time.monotonic())):
                    return
                append(env, chunk)
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    original_read = live.os.read
    captured = []

    def overlap_read(fd, requested):
        data = original_read(fd, requested)
        if not captured:
            captured.append(env.source.log.stat().st_size)
            started.set()
            # Force a mid-burst pass to observe >64 KiB of post-snapshot growth.
            assert finished.wait(3), "writer did not finish its 150 ms burst"
        return data

    read = env.source._read_probes
    if mutated:
        source = inspect.getsource(type(env.source)._read_probes)
        source = source.replace("    def _read_probes", "def mutated", 1)
        source = "\n".join(row[4:] if row.startswith("    ") else row for row in source.splitlines())
        assert "if offset < info.st_size:" in source
        source = source.replace("if offset < info.st_size:",
                                "if offset < info.st_size or os.fstat(fd).st_size - offset > BOUND:")
        namespace = dict(vars(live))
        exec(compile(source, "<post-read-growth-mutation>", "exec"), namespace)
        def read(now):
            return namespace["mutated"](env.source, now)
    monkeypatch.setattr(live.os, "read", overlap_read)
    thread = Thread(target=writer, name="CONTROLLED-real-GRML-writer")
    thread.start()
    pauses, offsets = [], []
    start = time.monotonic()
    try:
        for index in range(2):
            if index:
                stop.wait(max(0, start + index - time.monotonic()))
            try:
                read(now)
            except ValueError as exc:
                pauses.append(str(exc))
            offsets.append(env.source.offset)
    finally:
        stop.set()
        thread.join(3)
    assert not thread.is_alive() and not failures
    assert offsets == [before + len(prefix), before + len(raw)]
    assert env.source.log.stat().st_size - captured[0] > live.BOUND
    assert pauses == [], f"post-snapshot growth caused monitor pauses: {pauses}"


def test_l_real_grml_150ms_writer_one_second_passes_zero_native_pauses(setup, monkeypatch):
    _real_grml_thread_control(setup, monkeypatch)


def test_l_restore_growth_guard_mutation_fails_zero_pause_control(setup, monkeypatch):
    with pytest.raises(AssertionError, match="post-snapshot growth caused monitor pauses"):
        _real_grml_thread_control(setup, monkeypatch, mutated=True)


async def offline_native_chain(reader, client, *, stream="CONTROLLED-OFFLINE", passes=32):
    """Main-only timing helper: actual read_live order, no sockets with a fake client.

    Main stages the immutable copy into the reader's temporary log directory and
    sets both cursors before invoking this helper after exclusive CLEAR. A native
    exception intentionally prevents the watchdog scan, exactly as in production.
    This helper never declares an INSTALL gate or relaxes the one-second shield.
    """
    rows = []
    for index in range(passes):
        start = time.perf_counter_ns()
        try:
            health = await reader.read_live(client, stream)
            error = None
        except ValueError as exc:
            health, error = None, str(exc)
        rows.append(dict(pass_index=index + 1, wall_ms=(time.perf_counter_ns() - start) / 1e6,
                         native_offset=reader._source.offset,
                         watchdog_offsets={name: item[1] for name, item in reader._logs.items()},
                         error=error, live_error=None if health is None else health.new_live_error))
        if error is None:
            break
    return rows


def verified_offline_prefix(path: Path) -> bytes:
    raw = path.read_bytes()
    assert len(raw) == 19_022_548
    assert hashlib.sha256(raw).hexdigest() == "6cbecdd9ec5c47e43ef0702f5aea1d63bcded0d110ba8b7432b340095ba5bd59"
    return raw
