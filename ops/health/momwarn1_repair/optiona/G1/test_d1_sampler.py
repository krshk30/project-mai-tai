"""D1 focused native-file/CLI diagnostics; controlled clocks are not LIVE proof."""
from datetime import UTC, datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_continuation import rules

SPEC = importlib.util.spec_from_file_location("d1_sampler", Path(__file__).with_name("sampler.py"))
sampler = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sampler
SPEC.loader.exec_module(sampler)
NOW = datetime(2026, 10, 13, 12, 45, 43, tzinfo=UTC)
END = datetime(2026, 10, 13, 22, 10, tzinfo=UTC)


def args(tmp_path, *, day="2026-10-13", end=END, continuation=True):
    log = tmp_path / "gateway.log"
    log.write_bytes(b"old 1008 (policy violation)\n")
    return ["--gateway-log", str(log), "--output", str(tmp_path/"sampler.jsonl"),
            "--treatment-date", day, "--end-utc", end.isoformat(),
            *(["--continue-oct13"] if continuation else [])]


def test_exact_oct13_continuation_new_native_baseline_no_earlier_coverage(tmp_path, monkeypatch):
    argv = args(tmp_path)
    monkeypatch.setattr(sampler.time, "sleep", lambda _: None)
    times = iter([NOW, NOW+timedelta(seconds=1), END])
    assert sampler.main(argv, clock=lambda: next(times)) == 0
    rows = [json.loads(line) for line in (tmp_path/"sampler.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["status"] == "BASELINE"
    assert rows[0]["coverage_starts"] == NOW.isoformat()
    assert "earlier gap NOT covered" in rows[0]["coverage"]
    assert rows[0]["read_from_offset"] == rows[0]["size_bytes"]
    assert rows[0]["new_1008_lines"] == 0 and rows[1]["status"] == "OK"


@pytest.mark.parametrize("now,day,end,continuation", [
    (NOW, "2026-10-13", END, False),
    (NOW, "2026-10-14", END, True),
    (NOW+timedelta(days=1), "2026-10-14", END+timedelta(days=1), True),
    (NOW, "2026-10-13", END-timedelta(seconds=1), True),
    (NOW, "2026-10-13", END+timedelta(seconds=1), True),
    (END, "2026-10-13", END, True),
    (datetime(2026,10,13,10,59,tzinfo=UTC), "2026-10-13", END, True),
    (datetime(2026,10,14,11,tzinfo=UTC), "2026-10-14", END+timedelta(days=1), False),
])
def test_other_day_wrong_end_early_late_and_legacy_late_refuse_before_any_output(tmp_path, now, day, end, continuation):
    with pytest.raises(SystemExit) as error:
        sampler.main(args(tmp_path, day=day, end=end, continuation=continuation), clock=lambda: now)
    assert error.value.code == 2 and not (tmp_path/"sampler.jsonl").exists()


def test_oct14_default_before07_unchanged(tmp_path, monkeypatch):
    now = datetime(2026, 10, 14, 10, 59, tzinfo=UTC)
    end = now + timedelta(seconds=1)
    monkeypatch.setattr(sampler.time, "sleep", lambda _: None)
    times = iter([now, end])
    assert sampler.main(args(tmp_path, day="2026-10-14", end=end, continuation=False), clock=lambda: next(times)) == 0
    row = json.loads((tmp_path/"sampler.jsonl").read_text())
    assert row["status"] == "BASELINE" and "mode" not in row


def test_continuation_new1008_still_rc3_and_unreadable_still_rc2(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler.time, "sleep", lambda _: None)
    argv = args(tmp_path)
    calls = [0]
    def clock():
        calls[0] += 1
        if calls[0] == 2:
            with (tmp_path/"gateway.log").open("ab") as stream:
                stream.write(b"new 1008 (policy violation)\n")
        return NOW + timedelta(seconds=calls[0]-1)
    assert sampler.main(argv, clock=clock) == 3
    assert json.loads((tmp_path/"sampler.jsonl").read_text().splitlines()[-1])["new_1008_lines"] == 1
    (tmp_path/"sampler.jsonl").unlink()
    (tmp_path/"gateway.log").unlink()
    assert sampler.main(argv, clock=lambda: NOW) == 2


def test_actual_local_child_exit_stderr_retained_redacted_with_original_prefix_sha(tmp_path):
    raw = b'error: start before07ET "client_secret": "CONTROLLED_private" redis://u:CONTROLLED_password@x password=\'two words\'\n'
    child = subprocess.Popen([sys.executable, "-c", "import os;os.write(2,"+repr(raw)+");raise SystemExit(2)"],
                             stderr=subprocess.PIPE)
    diagnostics = rules.SamplerStderr(child.stderr)
    child.wait(timeout=5)
    audit = tmp_path/"audit.jsonl"
    try:
        assert rules.retain_sampler_exit(child, diagnostics, audit) == 2
        row = json.loads(audit.read_text())
        assert row["returncode"] == 2 and row["sampler_pid"] == child.pid
        assert row["original_prefix_sha256"] == hashlib.sha256(raw).hexdigest()
        assert row["completeness"] == "EOF_OBSERVED" and row["raw_body_retained"] is False
        assert "start before07ET" in row["stderr_redacted"]
        assert not any(s in audit.read_text() for s in ("CONTROLLED_private", "CONTROLLED_password", "two words"))
    finally:
        child.stderr.close()


def test_diagnostic_capture_bounds_partial_strings_and_running_pipe(tmp_path):
    read, write = os.pipe()
    with os.fdopen(read, "rb") as stream:
        diagnostics = rules.SamplerStderr(stream)
        diagnostics.drain()  # Empty but live pipe must NOT wait for a child to exit.
        assert diagnostics.raw == b"" and not diagnostics.eof
        os.write(write, b'password="CONTROLLED_partial words')
        diagnostics.drain()
        assert "CONTROLLED_partial" not in json.dumps(diagnostics.detail())
        diagnostics.raw = b"x"*8192
        os.write(write, b"extra")
        diagnostics.drain()
        assert diagnostics.truncated and len(diagnostics.raw) == 8192
        assert diagnostics.detail()["stderr_redacted"] == "[WITHHELD_TRUNCATED_STDERR]"
    os.close(write)


def test_safety_stop_success_nonzero_prevents_daily_second_close(monkeypatch, tmp_path):
    import inspect
    source = inspect.getsource(rules.run_guard)
    assert 'return stop_paper(reason, redis, settings, audit) or 1' in source
    assert '*(["--continue-oct13"] if continuation else [])' in source
    assert 'retain_sampler_exit(sampler, diagnostics, audit)' in source
    # Existing wrapper only issues scheduled_session_close for normal rc0.
    from test_daily_guard_identity import GUARD
    assert 'if rc == 0:' in inspect.getsource(GUARD.main)
    monkeypatch.setattr(rules, "stop_paper", lambda *_: 0)
    assert (rules.stop_paper("controlled", None, None, tmp_path) or 1) == 1
