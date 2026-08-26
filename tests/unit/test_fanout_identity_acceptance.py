from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
import sys

from project_mai_tai.fanout_identity import fanout_slot_id


SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "health" / "fanout_identity_acceptance.py"
SPEC = importlib.util.spec_from_file_location("fanout_identity_acceptance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)

SINCE = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
UNTIL = SINCE + timedelta(hours=4)
PROCESS = tool.ProcessStart(at=SINCE - timedelta(minutes=5), pid=1234)


def _identity(symbol: str, segment: int, slot: str) -> tuple[str, str, str]:
    slot_id = fanout_slot_id(
        strategy_code="schwab_1m_v2",
        symbol=symbol,
        segment_id=segment,
        slot=slot,
    )
    return str(segment), slot, slot_id


def _intent(
    *,
    symbol: str = "XPON",
    segment: int = 1,
    slot: str = "resting",
    at: datetime = SINCE,
    attempt_id: str | None = None,
    account: str = "live:orb",
):
    segment_id, slot_name, slot_id = _identity(symbol, segment, slot)
    return tool.IntentRecord(
        record_id=f"intent-{symbol}-{segment}-{slot}",
        symbol=symbol,
        account=account,
        at=at,
        last_at=at,
        segment_id=segment_id,
        slot=slot_name,
        slot_id=slot_id,
        attempt_id=attempt_id or f"root-{symbol}-{segment}-{slot}",
        source="reactive" if slot == "reclaim" else "rth_resting_mirror",
    )


def _attempt(
    *,
    symbol: str = "XPON",
    segment: int = 1,
    slot: str = "resting",
    attempt_id: str = "root-XPON-1-resting",
    predecessor: str = "",
    at: datetime = SINCE,
    last_at: datetime | None = None,
    status: str = "filled",
    fills: int = 1,
    account: str = "live:orb",
):
    segment_id, slot_name, slot_id = _identity(symbol, segment, slot)
    return tool.AttemptRecord(
        record_id=f"order-{attempt_id}",
        intent_id=f"intent-{symbol}-{segment}-{slot}",
        client_order_id=attempt_id,
        symbol=symbol,
        account=account,
        at=at,
        last_at=last_at or at,
        status=status,
        segment_id=segment_id,
        slot=slot_name,
        slot_id=slot_id,
        attempt_id=attempt_id,
        predecessor_id=predecessor,
        source="reactive" if slot == "reclaim" else "rth_resting_mirror",
        event_total=1,
        event_identity=1,
        fill_total=fills,
        fill_identity=fills,
    )


def _evaluate(intents, attempts, starts=(PROCESS,)):
    return tool.evaluate(
        intents=intents,
        attempts=attempts,
        starts=starts,
        since=SINCE,
        until=UNTIL,
    )


def test_complete_depth_one_fill_is_pass_not_missing_data() -> None:
    report = _evaluate([_intent()], [_attempt()])

    assert report.exit_code == tool.PASS
    output = "\n".join(report.lines)
    assert "max_chain_depth=1" in output
    assert "filled_attempts=1" in output
    assert "[V2-FANOUT-IDENTITY-ACCEPTED]" in output


def test_realistic_depth_fifty_chain_stays_readable_and_complete() -> None:
    attempts = []
    predecessor = ""
    for index in range(50):
        attempt_id = f"attempt-{index:02d}"
        attempts.append(
            _attempt(
                attempt_id=attempt_id,
                predecessor=predecessor,
                at=SINCE + timedelta(seconds=index),
                status="filled" if index == 49 else "cancelled",
                fills=1 if index == 49 else 0,
            )
        )
        predecessor = attempt_id

    report = _evaluate([_intent(attempt_id="attempt-00")], attempts)

    assert report.exit_code == tool.PASS
    output = "\n".join(report.lines)
    assert "submitted=50" in output
    assert "roots=1 max_chain_depth=50" in output


def test_multiple_unlinked_roots_fail_and_refuse_duplicate_grade() -> None:
    attempts = [
        _attempt(attempt_id="attempt-a", fills=0, status="cancelled"),
        _attempt(attempt_id="attempt-b", fills=1, status="filled"),
    ]

    report = _evaluate([_intent()], attempts)

    assert report.exit_code == tool.FAIL
    output = "\n".join(report.lines)
    assert "has 2 roots across 2 attempts" in output
    assert "duplicate grade refused" in output


def test_missing_identity_fails_instead_of_printing_clean_duplicate_zero() -> None:
    broken = _attempt()
    broken = tool.AttemptRecord(**{**broken.__dict__, "slot_id": ""})

    report = _evaluate([_intent()], [broken])

    assert report.exit_code == tool.FAIL
    assert "duplicate grade refused" in "\n".join(report.lines)


def test_same_venue_two_filled_attempts_is_duplicate_but_cross_venue_pair_is_not() -> None:
    first = _attempt(attempt_id="attempt-a")
    second = _attempt(
        attempt_id="attempt-b",
        predecessor="attempt-a",
        at=SINCE + timedelta(seconds=1),
    )
    report = _evaluate([_intent()], [first, second])
    assert report.exit_code == tool.FAIL
    assert "duplicates=1" in "\n".join(report.lines)

    other_venue = tool.AttemptRecord(
        **{
            **second.__dict__,
            "account": "paper:schwab_1m_v2",
            "intent_id": "intent-XPON-1-resting-schwab",
            "predecessor_id": "",
        }
    )
    other_intent = tool.IntentRecord(
        **{
            **_intent(account="paper:schwab_1m_v2", attempt_id="attempt-b").__dict__,
            "record_id": "intent-XPON-1-resting-schwab",
        }
    )
    report = _evaluate([_intent(), other_intent], [first, other_venue])
    assert "duplicates=0" in "\n".join(report.lines)


def test_intent_root_must_link_to_the_persisted_order_chain() -> None:
    report = _evaluate([_intent(attempt_id="missing-root")], [_attempt()])

    assert report.exit_code == tool.FAIL
    assert "root attempt missing-root is absent" in "\n".join(report.lines)


def test_same_symbol_across_restart_is_could_not_tell_not_clean() -> None:
    restart = tool.ProcessStart(at=SINCE + timedelta(hours=1), pid=5678)
    attempts = [
        _attempt(attempt_id="attempt-a", fills=0, status="cancelled"),
        _attempt(
            attempt_id="attempt-b",
            predecessor="attempt-a",
            at=restart.at + timedelta(seconds=1),
        ),
    ]

    report = _evaluate([_intent()], attempts, starts=(PROCESS, restart))

    assert report.exit_code == tool.COULD_NOT_TELL
    assert "restart-spanning symbols=XPON" in "\n".join(report.lines)


def test_one_order_updated_after_restart_is_could_not_tell_not_clean() -> None:
    restart = tool.ProcessStart(at=SINCE + timedelta(hours=1), pid=5678)
    attempt = _attempt(last_at=restart.at + timedelta(seconds=1))

    report = _evaluate([_intent()], [attempt], starts=(PROCESS, restart))

    assert report.exit_code == tool.COULD_NOT_TELL
    assert "restart-spanning symbols=XPON" in "\n".join(report.lines)


def test_no_process_evidence_is_could_not_tell() -> None:
    report = _evaluate([_intent()], [_attempt()], starts=())

    assert report.exit_code == tool.COULD_NOT_TELL
    assert "no PID/process start overlaps" in "\n".join(report.lines)


def test_zero_denominator_is_unexercised() -> None:
    report = _evaluate([], [])

    assert report.exit_code == tool.UNEXERCISED
    assert "queued=0" in "\n".join(report.lines)


def test_process_start_parser_requires_timestamp_and_pid() -> None:
    starts = tool.parse_process_starts(
        ["2026-08-26 20:01:02,003 INFO schwab_1m_v2 bot starting pid=1234 (enabled=True)"]
    )
    assert starts == [tool.ProcessStart(datetime(2026, 8, 26, 20, 1, 2, 3000, tzinfo=UTC), 1234)]

    try:
        tool.parse_process_starts(["2026-08-26 20:01:02 INFO schwab_1m_v2 bot starting pid=?"])
    except tool.EvidenceFailure as exc:
        assert "timestamp/PID is unreadable" in str(exc)
    else:
        raise AssertionError("malformed start marker was accepted")


def test_database_query_is_fixed_and_read_only() -> None:
    assert "BEGIN READ ONLY" in tool.SQL
    assert "COPY (" in tool.SQL
    assert "trade_intents" in tool.SQL and "broker_orders" in tool.SQL
    assert "fills" in tool.SQL and "broker_order_events" in tool.SQL
    assert "last_at" in tool.SQL and "max(e.event_at)" in tool.SQL
    assert ":'window_since'" in tool.SQL and ":'window_until'" in tool.SQL
