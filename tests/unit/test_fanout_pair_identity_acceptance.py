from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from project_mai_tai.fanout_identity import fanout_slot_id


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "ops"
    / "health"
    / "fanout_pair_identity_acceptance.py"
)
SPEC = importlib.util.spec_from_file_location("fanout_pair_identity_acceptance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SINCE = datetime(2026, 8, 27, 11, 0, tzinfo=UTC)
UNTIL = datetime(2026, 8, 27, 20, 0, tzinfo=UTC)
SEGMENT = "1787830200000"


def _record(*, matches: int = 1, slot_id: str | None = None):
    expected = fanout_slot_id(
        strategy_code="schwab_1m_v2",
        symbol="YYGH",
        segment_id=SEGMENT,
        slot="resting",
    )
    return MODULE.PairRecord(
        intent_id="webull-intent-1",
        symbol="YYGH",
        segment_id=SEGMENT,
        slot="resting",
        slot_id=slot_id if slot_id is not None else expected,
        matching_primary_intents=matches,
    )


def test_complete_shared_identity_passes_and_names_scope() -> None:
    report = MODULE.evaluate([_record()], since=SINCE, until=UNTIL)

    assert report.exit_code == MODULE.PASS
    assert any("paired=1 of 1" in line for line in report.lines)
    assert any("V2-CROSS-VENUE-IDENTITY-PAIRED" in line for line in report.lines)
    assert any("no duplicate, slot-consumption, or fill verdict" in line for line in report.lines)


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (_record(matches=0), "no Schwab primary intent"),
        (_record(slot_id="wrong"), "slot id does not match"),
    ],
)
def test_unpaired_or_malformed_identity_fails(record, message: str) -> None:
    report = MODULE.evaluate([record], since=SINCE, until=UNTIL)

    assert report.exit_code == MODULE.FAIL
    assert any("paired=0 of 1" in line for line in report.lines)
    assert any(message in line for line in report.lines)


def test_zero_denominator_is_unexercised_not_pass() -> None:
    report = MODULE.evaluate([], since=SINCE, until=UNTIL)

    assert report.exit_code == MODULE.UNEXERCISED
    assert any("paired=0 of 0" in line for line in report.lines)
    assert any("denominator is 0" in line for line in report.lines)


def test_malformed_window_refuses_before_a_clean_grade() -> None:
    def query_must_not_run(_since, _until):
        raise AssertionError("malformed window reached the database")

    report = MODULE.run_report(since=UNTIL, until=SINCE, query=query_must_not_run)

    assert report.exit_code == MODULE.COULD_NOT_TELL
    assert any("window start is not before end" in line for line in report.lines)


def test_query_uses_stdin_so_psql_interpolates_the_real_window(monkeypatch) -> None:
    expected_header = (
        "intent_id,symbol,segment_id,slot,slot_id,matching_primary_intents\n"
    )
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=expected_header, stderr="")

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)

    assert MODULE.query_database(SINCE, UNTIL) == []
    assert captured["command"][-2:] == ["-f", "-"]
    assert captured["input"] == MODULE.SQL
    assert ":'window_since'" in MODULE.SQL and ":'window_until'" in MODULE.SQL
