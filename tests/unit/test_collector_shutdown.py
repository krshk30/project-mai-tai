from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PATH = Path(os.environ.get("V2_RESTART_EVIDENCE_MODULE", str(
    Path(__file__).resolve().parents[2] / "ops/health/v2_restart_evidence.py"
)))
SPEC = importlib.util.spec_from_file_location("collector_shutdown", PATH)
vre = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = vre
SPEC.loader.exec_module(vre)
START = datetime(2026, 10, 9, 21, 11, 7, tzinfo=UTC)
UNIT = "project-mai-tai-orb-schwab.service"


def block(marker=START + timedelta(seconds=2), exception="asyncio.exceptions.CancelledError"):
    return [
        "Traceback (most recent call last):",
        '  File "/app/.venv/bin/mai-tai-orb-schwab", line 6, in <module>',
        "    sys.exit(run())",
        '  File "/usr/lib/python3.12/asyncio/tasks.py", line 665, in sleep',
        "    return await future",
        exception,
        marker.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        + " INFO [orb-schwab] [ORB-SCHWAB] mode=LIVE live_sending=True",
    ]


def journal(start=START):
    return [
        {"_PID": "1", "UNIT": UNIT, "_BOOT_ID": "boot",
         "INVOCATION_ID": "new" if kind == "Started" else "old",
         "__REALTIME_TIMESTAMP": str(int((start + timedelta(seconds=offset)).timestamp() * 1e6)),
         "MESSAGE": f"{kind} {UNIT} - ORB producer."}
        for kind, offset in (("Stopping", -1), ("Stopped", 0), ("Started", 0))
    ]


def parse(lines=None, rows=None, since=START):
    commands = []

    def runner(command):
        commands.append(command)
        return "\n".join(json.dumps(row) for row in (journal() if rows is None else rows))

    result = vre.parse_log_files(
        [("orb-schwab.log", block() if lines is None else lines)],
        since=since, service="orb-schwab", runner=runner,
    )
    return result, commands


def test_shutdown_classification_keeps_raw_block_and_binds_actual_start():
    lines = block()
    original = list(lines)
    result, commands = parse(lines)
    assert lines == original
    assert result.prior_process_exit_tracebacks == 1
    assert result.traceback_times_utc == ()
    assert result.timestamped_records == 1
    assert commands == [["sudo", "-n", "journalctl", "-u", UNIT,
                         "--since", "2026-10-09T21:11:04+00:00",
                         "--until", "2026-10-09T21:11:09+00:00",
                         "--output=json", "--no-pager"]]


def test_retained_historical_start_requires_its_own_journal_transition():
    old = datetime(2026, 10, 9, 0, 10, 30, tzinfo=UTC)
    result, _ = parse(block(old + timedelta(seconds=2)), journal(old))
    assert result.prior_process_exit_tracebacks == 1
    assert result.timestamped_records == 0
    with pytest.raises(vre.EvidenceUnknown):
        parse(block(old + timedelta(seconds=2)), journal())


@pytest.mark.parametrize("seconds,accepted", [(5, True), (5.001, False), (-0.001, False)])
def test_actual_start_to_marker_bound(seconds, accepted):
    lines = block(START + timedelta(seconds=seconds))
    rows = journal()
    # Stop may precede the five-second start bound, so test the exact lower boundary too.
    rows[0]["__REALTIME_TIMESTAMP"] = rows[1]["__REALTIME_TIMESTAMP"]
    if accepted:
        assert parse(lines, rows)[0].prior_process_exit_tracebacks == 1
    else:
        with pytest.raises(vre.EvidenceUnknown):
            parse(lines, rows)


@pytest.mark.parametrize("exception", ["RuntimeError: boom", "KeyboardInterrupt",
    "CancelledError", "asyncio.exceptions.CancelledError: unexpected", ""])
def test_other_or_incomplete_exception_is_unknown(exception):
    with pytest.raises(vre.EvidenceUnknown):
        parse(block(exception=exception))


@pytest.mark.parametrize("change", ["missing", "unit", "pid", "boot", "invocation",
    "duplicate", "reversed", "bad_time", "no_stop", "extra_line", "another_stack",
    "wrong_entrypoint", "wrong_marker"])
def test_missing_or_ambiguous_identity_stays_unknown(change):
    rows, lines = journal(), block()
    if change == "missing":
        rows = []
    elif change == "unit":
        rows[2]["UNIT"] = "project-mai-tai-control.service"
    elif change == "pid":
        rows[2]["_PID"] = "2345"
    elif change == "boot":
        rows[2]["_BOOT_ID"] = "other"
    elif change == "invocation":
        rows[2]["INVOCATION_ID"] = "old"
    elif change == "duplicate":
        rows.append(rows[2])
    elif change == "reversed":
        rows.reverse()
    elif change == "bad_time":
        rows[2]["__REALTIME_TIMESTAMP"] = "unknown"
    elif change == "no_stop":
        rows = rows[1:]
    elif change == "extra_line":
        lines.insert(-1, "unrelated unscoped event")
    elif change == "another_stack":
        lines.insert(-1, "Traceback (most recent call last):")
    elif change == "wrong_entrypoint":
        lines[1] = lines[1].replace("mai-tai-orb-schwab", "mai-tai-control")
    else:
        lines[-1] = lines[-1].replace("[ORB-SCHWAB] mode=", "ordinary mode=")
    with pytest.raises(vre.EvidenceUnknown):
        parse(lines, rows)


def test_timestamped_current_cancellation_is_still_runtime_error():
    lines = ["2026-10-09 21:11:07,001 ERROR current failure"] + block()
    result, commands = parse(lines)
    assert result.traceback_times_utc == (START + timedelta(milliseconds=1),)
    assert result.prior_process_exit_tracebacks == 0
    assert commands == []


def test_journal_error_is_unknown_not_clean_shutdown():
    def broken(command):
        raise vre.EvidenceUnknown("journal unavailable")

    with pytest.raises(vre.EvidenceUnknown, match="journal unavailable"):
        vre.parse_log_files([("orb.log", block())], since=START,
                            service="orb-schwab", runner=broken)
