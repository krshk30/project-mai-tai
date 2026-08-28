from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo


SCRIPT = Path(__file__).parents[2] / "ops" / "health" / "eod_counts.py"


def _load_script(monkeypatch, *, segments: list[dict], query):
    check = ModuleType("check")
    check.ET = ZoneInfo("America/New_York")
    check.LIVE_ARM_MAX_AGE_SECS = 600
    check.CAP_ACCT = "live:schwab_1m_v2"

    def parse_segments(_start, _end):
        parse_segments.replay_skipped = 0
        return list(segments)

    check.parse_segments = parse_segments
    check.q = query
    monkeypatch.setitem(sys.modules, "check", check)

    spec = importlib.util.spec_from_file_location("test_eod_counts_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_entry_split_reads_explicit_economic_slot_not_order_style(
    monkeypatch, capsys
) -> None:
    """A reclaim may itself rest; only cw_entry_slot can grade first vs reclaim."""
    start = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)
    segments = [
        {
            "sym": "CELU",
            "start": start,
            "end": start + timedelta(minutes=30),
            "trig": 3.0,
            "flip": 2.9,
            "enterable": True,
        }
    ]
    slot_queries: list[str] = []

    def query(sql, _params):
        if "FROM trade_intents ti" in sql:
            slot_queries.append(sql)
            return [("first", 2), ("reclaim", 1), ("", 1)]
        if "SELECT f.symbol, f.filled_at" in sql:
            return []
        if "SELECT ba.name, f.symbol" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(monkeypatch, segments=segments, query=query)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module.main() == 0
    output = capsys.readouterr().out

    assert len(slot_queries) == 1
    assert "cw_entry_slot" in slot_queries[0]
    assert "resting_entry" not in slot_queries[0]
    assert "first=2  reclaim=1  unattributed=1  total=4" in output
    assert "cw_entry_slot coverage=3/4 -- COULD_NOT_TELL" in output
    assert (
        "2 attributed first-slot fills / 1 live in-window arms -- COULD_NOT_TELL"
        in output
    )
    assert "= 200.0%" not in output
    assert "entries=4 (first=2 reclaim=1 unattributed=1)" in output
    assert "slot_coverage=3/4 slot_verdict=COULD_NOT_TELL" in output
    assert "resting=" not in output
    assert "reactive=" not in output


def test_zero_entries_names_the_zero_denominator(monkeypatch, capsys) -> None:
    def query(sql, _params):
        if "FROM trade_intents ti" in sql:
            return []
        if "SELECT f.symbol, f.filled_at" in sql:
            return []
        if "SELECT ba.name, f.symbol" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(monkeypatch, segments=[], query=query)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module.main() == 0
    output = capsys.readouterr().out

    assert "first=0  reclaim=0  unattributed=0  total=0" in output
    assert "cw_entry_slot coverage=0/0 -- COULD_NOT_TELL (denominator=0" in output
    assert "0 attributed first-slot fills / 0 live in-window arms -- COULD_NOT_TELL" in output
    assert "entries=0 (first=0 reclaim=0 unattributed=0)" in output
    assert "slot_coverage=0/0 slot_verdict=COULD_NOT_TELL" in output


def test_zero_slot_coverage_with_filled_population_is_could_not_tell(
    monkeypatch, capsys
) -> None:
    """The pre-#821 0/239 population is unknown classification, never a 0.0% result."""
    start = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)
    segments = [
        {
            "sym": "CELU",
            "start": start,
            "end": start + timedelta(minutes=30),
            "trig": 3.0,
            "flip": 2.9,
            "enterable": True,
        }
    ]

    def query(sql, _params):
        if "FROM trade_intents ti" in sql:
            return [("", 239)]
        if "SELECT f.symbol, f.filled_at" in sql:
            return []
        if "SELECT ba.name, f.symbol" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(monkeypatch, segments=segments, query=query)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module.main() == 0
    output = capsys.readouterr().out

    assert "cw_entry_slot coverage=0/239 -- COULD_NOT_TELL" in output
    assert "239 filled entries have unknown classification" in output
    assert "0 attributed first-slot fills / 1 live in-window arms -- COULD_NOT_TELL" in output
    assert "= 0.0%" not in output
    assert "slot_coverage=0/239 slot_verdict=COULD_NOT_TELL" in output


def test_gradeable_population_preserves_a_true_numeric_zero(monkeypatch, capsys) -> None:
    """Zero first-slot fills is numeric only when every filled entry has a known slot."""
    start = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)
    segments = [
        {
            "sym": "CELU",
            "start": start,
            "end": start + timedelta(minutes=30),
            "trig": 3.0,
            "flip": 2.9,
            "enterable": True,
        }
    ]

    def query(sql, _params):
        if "FROM trade_intents ti" in sql:
            return [("reclaim", 1)]
        if "SELECT f.symbol, f.filled_at" in sql:
            return []
        if "SELECT ba.name, f.symbol" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(monkeypatch, segments=segments, query=query)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module.main() == 0
    output = capsys.readouterr().out

    assert "cw_entry_slot coverage=1/1 = 100.0% -- GRADEABLE" in output
    assert "0 attributed first-slot fills / 1 live in-window arms = 0.0%" in output
    assert "slot_coverage=1/1 slot_verdict=GRADEABLE first_rate_verdict=GRADEABLE" in output
