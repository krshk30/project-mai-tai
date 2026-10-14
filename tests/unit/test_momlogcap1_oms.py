from pathlib import Path

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
