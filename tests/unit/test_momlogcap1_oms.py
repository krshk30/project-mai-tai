from pathlib import Path
import hashlib
import os
import threading
import time

import pytest

from project_mai_tai.momwarn_health_contract import OmsLogMonitor


def completed(sequence):
    return (f"2026-10-14 09:58:00,000 INFO [oms-risk] [OMS-BROKER-SYNC-PASS] "
            f"id={sequence} phase=end outcome=ok duration_ms=1000.000 exceeds_note_age=False\n").encode()


def monitor(tmp_path):
    process = tmp_path / "12"
    process.mkdir()
    fields = ["0"] * 20
    fields[0], fields[19] = "S", "100"
    (process / "stat").write_text("12 (oms) " + " ".join(fields))
    log = tmp_path / "oms.log"
    log.write_bytes(completed(10))
    reader = OmsLogMonitor(log, 12, verified_timezone="UTC", cadence_seconds=15,
                           cadence_source="CONTROLLED", wall_ns=lambda: 1791971880000000000,
                           proc=tmp_path)
    assert reader.read()["oms_sync_seq"] == 10
    return reader, log


def test_402kb_with_completed_pass_is_one_read(tmp_path):
    reader, log = monitor(tmp_path)
    with log.open("ab") as output:
        output.write(b"INFO bounded replay\n" * 22000 + completed(11))
    assert reader.read()["oms_sync_seq"] == 11
    assert reader.position == log.stat().st_size


def test_larger_backlog_commits_progress_before_unknown(tmp_path):
    reader, log = monitor(tmp_path)
    with log.open("ab") as output:
        output.write(b"INFO replay\n" * 200000 + completed(11))
    before = reader.position
    with pytest.raises(ValueError, match="catch-up incomplete"):
        reader.read()
    assert reader.position - before == reader.MAX_LOG_PASS_BYTES
    assert reader.read()["oms_sync_seq"] == 11
    assert reader.position == log.stat().st_size


def test_huge_unrelated_line_cannot_lock_cursor(tmp_path):
    reader, log = monitor(tmp_path)
    with log.open("ab") as output:
        output.write(b"X" * (reader.MAX_LOG_PASS_BYTES + 200000) + b"\n" + completed(11))
    with pytest.raises(ValueError, match="catch-up incomplete"):
        reader.read()
    assert len(reader.partial) <= 65536
    with pytest.raises(ValueError, match="oversized evidence parsed"):
        reader.read()
    assert reader.read()["oms_sync_seq"] == 11


def test_huge_line_preserves_actual_end_receipt(tmp_path):
    reader, log = monitor(tmp_path)
    original = completed(11)
    with log.open("ab") as output:
        output.write(original[:29] + b"X" * 200000 + b" " + original[29:])
    with pytest.raises(ValueError, match="oversized evidence parsed"):
        reader.read()
    assert reader.position == log.stat().st_size
    assert reader.read()["oms_sync_seq"] == 11


def test_sequence_gap_in_later_chunk_stays_unknown(tmp_path):
    reader, log = monitor(tmp_path)
    with log.open("ab") as output:
        output.write(completed(11) + b"INFO replay\n" * 10000 + completed(13))
    with pytest.raises(ValueError, match="sequence gap"):
        reader.read()
    assert reader.latest["oms_sync_seq"] == 11


def test_copytruncate_identical_completion_keeps_original_stamp(tmp_path):
    reader, log = monitor(tmp_path)
    original = reader.read()["oms_measured_wall_ns"]
    with log.open("ab") as output:
        output.write(b"INFO noise\n" * 10000)
    reader.read()
    log.write_bytes(completed(10))
    result = reader.read()
    assert result["oms_sync_seq"] == 10
    assert result["oms_measured_wall_ns"] == original
    assert result["oms_log_copytruncate_count"] == 1


def test_copytruncate_changed_same_sequence_is_still_refused(tmp_path):
    reader, log = monitor(tmp_path)
    with log.open("ab") as output:
        output.write(b"INFO noise\n" * 10000)
    reader.read()
    log.write_bytes(completed(10).replace(b"duration_ms=1000.000", b"duration_ms=2000.000"))
    with pytest.raises(ValueError, match="sequence gap"):
        reader.read()


def test_mutation_raise_without_progress_fails_catchup_control(tmp_path, monkeypatch):
    reader, log = monitor(tmp_path)
    original = OmsLogMonitor.read

    def old_order(self):
        if self.position is not None and self.log.stat().st_size - self.position > 65536:
            raise ValueError("OMS log truncated/backlog bound")
        return original(self)

    monkeypatch.setattr(OmsLogMonitor, "read", old_order)
    with log.open("ab") as output:
        output.write(b"INFO replay\n" * 200000 + completed(11))
    before = reader.position
    with pytest.raises(ValueError, match="backlog bound"):
        reader.read()
    with pytest.raises(AssertionError):
        assert reader.position > before


def writer_growth_control(tmp_path, monkeypatch, *, old_growth_check=False):
    path = Path("/Users/velkris/.codex/task-queues/analysis/momwarn1-wed-no-warnings-20261014/"
                "v2-real-GRML-20261014T090720-21.txt")
    if not path.exists():
        pytest.skip("requires retained real GRML bytes; no synthetic evidence substitution")
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "bccff8b4cd148fe0f84bdaf38eab8e8e5e98514e276cf1d37415127a9782a831"
    reader, log = monitor(tmp_path)
    begin, enough = threading.Event(), threading.Event()
    original = os.read
    inode = log.stat().st_ino
    armed = False

    def writer():
        assert begin.wait(2)
        start = time.monotonic()
        with log.open("ab", buffering=0) as output:
            for index in range(15):
                output.write(raw[len(raw) * index // 15:len(raw) * (index + 1) // 15])
                if index >= 3:
                    enough.set()
                time.sleep(max(0, start + (index + 1) * .010 - time.monotonic()))

    def read(fd, size):
        nonlocal armed
        result = original(fd, size)
        if not armed and os.fstat(fd).st_ino == inode:
            armed = True
            begin.set()
            assert enough.wait(2)  # Force real growth after START fstat, before the read returns.
        return result

    monkeypatch.setattr(os, "read", read)
    if old_growth_check:
        native_read = reader.read

        def restored_growth_check():
            result = native_read()
            if log.stat().st_size - reader.position > 65536:
                raise ValueError("OMS postread growth bound restored by mutation")
            return result

        monkeypatch.setattr(reader, "read", restored_growth_check)
    worker = threading.Thread(target=writer)
    worker.start()
    try:
        start = time.monotonic()
        for index in range(3):
            time.sleep(max(0, start + index - time.monotonic()))
            error = None
            try:
                reader.read()
            except ValueError as exc:
                error = str(exc)
            assert error is None, error
        assert reader.position == log.stat().st_size
        assert reader.latest["oms_sync_seq"] == 10
    finally:
        begin.set()
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_l_real_writer_growth_is_next_pass_without_failure(tmp_path, monkeypatch):
    writer_growth_control(tmp_path, monkeypatch)


def test_l_mutation_postread_growth_guard_breaks_clean_pass(tmp_path, monkeypatch):
    with pytest.raises(AssertionError):
        writer_growth_control(tmp_path, monkeypatch, old_growth_check=True)
