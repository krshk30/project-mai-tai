from __future__ import annotations

import importlib.util
import sys
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest


SCRIPT = Path(__file__).parents[2] / "ops" / "health" / "eod_counts.py"


def _load_script(monkeypatch, *, segments: list[dict], query, replay_skipped: int | None = 0):
    check = ModuleType("check")
    check.ET = ZoneInfo("America/New_York")
    check.LIVE_ARM_MAX_AGE_SECS = 600
    check.CAP_ACCT = "live:schwab_1m_v2"

    def parse_segments(_start, _end):
        if replay_skipped is not None:
            parse_segments.replay_skipped = replay_skipped
        elif hasattr(parse_segments, "replay_skipped"):
            delattr(parse_segments, "replay_skipped")
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
        if "/* eod:slot-counts */" in sql:
            slot_queries.append(sql)
            return [("first", 2), ("reclaim", 1), ("", 1)]
        if "/* eod:filled-intents */" in sql:
            return [("CELU", start + timedelta(minutes=5), f"intent-{i}") for i in range(4)]
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
    assert "slot_coverage=3/4 slot_population=4/4 slot_verdict=COULD_NOT_TELL" in output
    assert "no_entry=0 no_entry_verdict=GRADEABLE" in output
    assert "trips=0 trips_verdict=UNEXERCISED" in output
    assert "resting=" not in output
    assert "reactive=" not in output


def test_zero_entries_names_the_zero_denominator(monkeypatch, capsys) -> None:
    def query(sql, _params):
        if "/* eod:slot-counts */" in sql:
            return []
        if "/* eod:filled-intents */" in sql:
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
    assert "slot_coverage=0/0 slot_population=0/0 slot_verdict=COULD_NOT_TELL" in output
    assert "no_entry=0 no_entry_verdict=COULD_NOT_TELL" in output
    assert "trips=0 trips_verdict=UNEXERCISED" in output


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
        if "/* eod:slot-counts */" in sql:
            return [("", 239)]
        if "/* eod:filled-intents */" in sql:
            return [
                ("CELU", start + timedelta(minutes=5), f"intent-{i}")
                for i in range(239)
            ]
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
    assert "slot_coverage=0/239 slot_population=239/239 slot_verdict=COULD_NOT_TELL" in output


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
        if "/* eod:slot-counts */" in sql:
            return [("reclaim", 1)]
        if "/* eod:filled-intents */" in sql:
            return [("CELU", start + timedelta(minutes=5), "intent-reclaim")]
        if "SELECT ba.name, f.symbol" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(monkeypatch, segments=segments, query=query)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module.main() == 0
    output = capsys.readouterr().out

    assert "cw_entry_slot coverage=1/1 = 100.0% -- GRADEABLE" in output
    assert "0 attributed first-slot fills / 1 live in-window arms = 0.0%" in output
    assert (
        "slot_coverage=1/1 slot_population=1/1 slot_verdict=GRADEABLE "
        "first_rate_verdict=GRADEABLE"
    ) in output


def test_normalized_slot_rows_are_summed_instead_of_overwritten(monkeypatch, capsys) -> None:
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
        if "/* eod:slot-counts */" in sql:
            return [("first", 3), ("FIRST", 5)]
        if "/* eod:filled-intents */" in sql:
            return [("CELU", start + timedelta(minutes=5), f"intent-{i}") for i in range(8)]
        if "SELECT ba.name, f.symbol" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(monkeypatch, segments=segments, query=query)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "first=8  reclaim=0  unattributed=0  total=8" in output
    assert "cw_entry_slot coverage=8/8 = 100.0% -- GRADEABLE" in output


def test_first_slot_numerator_above_live_arm_denominator_is_ungradeable(
    monkeypatch, capsys
) -> None:
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
        if "/* eod:slot-counts */" in sql:
            return [("first", 2)]
        if "/* eod:filled-intents */" in sql:
            return [
                ("CELU", start + timedelta(minutes=5), "intent-1"),
                ("CELU", start + timedelta(minutes=6), "intent-2"),
            ]
        if "SELECT ba.name, f.symbol" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(monkeypatch, segments=segments, query=query)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module.main() == 0
    output = capsys.readouterr().out

    assert "cw_entry_slot coverage=2/2 = 100.0% -- GRADEABLE" in output
    assert "2 attributed first-slot fills / 1 live in-window arms -- COULD_NOT_TELL" in output
    assert "numerator must not exceed denominator" in output
    assert "first_rate_verdict=COULD_NOT_TELL" in output
    assert "= 200.0%" not in output


def test_slot_and_detail_queries_share_the_exact_filled_intent_population(
    monkeypatch
) -> None:
    queries: dict[str, str] = {}

    def query(sql, _params):
        if "/* eod:slot-counts */" in sql:
            queries["slot"] = sql
            return []
        if "/* eod:filled-intents */" in sql:
            queries["detail"] = sql
            return []
        if "SELECT ba.name, f.symbol" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(monkeypatch, segments=[], query=query)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module.main() == 0
    slot_cte = queries["slot"].split("/* eod:slot-counts */", 1)[0]
    detail_cte = queries["detail"].split("/* eod:filled-intents */", 1)[0]
    assert slot_cte == detail_cte
    for predicate in (
        "s.id=ti.strategy_id",
        "ba.id=ti.broker_account_id",
        "bo.intent_id=ti.id",
        "f.order_id=bo.id AND f.side='buy'",
        "ti.intent_type='open'",
        "ti.status='filled'",
        "f.filled_at>=%s AND f.filled_at<%s",
    ):
        assert predicate in slot_cte


def test_even_population_uses_arithmetic_median(monkeypatch) -> None:
    module = _load_script(monkeypatch, segments=[], query=lambda _sql, _params: [])
    assert module.median([-10.0, -5.0, 5.0, 10.0]) == statistics.median(
        [-10.0, -5.0, 5.0, 10.0]
    ) == 0.0


def test_missing_replay_counter_is_could_not_tell_not_excluded_zero(
    monkeypatch, capsys
) -> None:
    def query(sql, _params):
        if "/* eod:slot-counts */" in sql:
            return []
        if "/* eod:filled-intents */" in sql:
            return []
        if "SELECT ba.name, f.symbol" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(
        monkeypatch,
        segments=[],
        query=query,
        replay_skipped=None,
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "warmup-replay arms excluded=COULD_NOT_TELL (counter absent)" in output
    assert "replay_excluded=COULD_NOT_TELL" in output
    assert "replay_excluded=0" not in output
    assert "no_entry_verdict=COULD_NOT_TELL" in output


def test_zero_enterable_crosses_names_denominator(monkeypatch, capsys) -> None:
    def query(sql, _params):
        if "/* eod:slot-counts */" in sql:
            return []
        if "/* eod:filled-intents */" in sql:
            return []
        if "SELECT ba.name, f.symbol" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(monkeypatch, segments=[], query=query)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module.main() == 0
    output = capsys.readouterr().out
    assert (
        "0 of 0 live in-window crosses -- COULD_NOT_TELL "
        "(denominator=0; no-entry rate is not zero)"
    ) in output
    assert "no_entry=0 no_entry_verdict=COULD_NOT_TELL" in output


def test_entry_and_fill_population_disagreement_degrades_only_slot_section(
    monkeypatch, capsys
) -> None:
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
        if "/* eod:slot-counts */" in sql:
            return [("first", 6)]
        if "/* eod:filled-intents */" in sql:
            return []
        if "SELECT ba.name, f.symbol" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(monkeypatch, segments=segments, query=query)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module.main() == 0
    output = capsys.readouterr().out

    assert "filled-intent populations disagree: slot_counts=6 detail_rows=0" in output
    assert "filled-intent population slot_counts=6 detail_rows=0 verdict=COULD_NOT_TELL" in output
    assert "slot_population=6/0 slot_verdict=COULD_NOT_TELL" in output
    assert "-- NO-ENTRY CROSSES" in output
    assert "1 of 1 live in-window crosses produced no Schwab entry" in output
    assert "no_entry=1 no_entry_verdict=GRADEABLE" in output
    assert "-- CLOSED ROUND TRIPS" in output
    assert "trips=0 trips_verdict=UNEXERCISED" in output


def test_terminal_verdict_prints_even_when_a_late_section_raises(
    monkeypatch, capsys
) -> None:
    def query(sql, _params):
        if "/* eod:slot-counts */" in sql:
            return []
        if "/* eod:filled-intents */" in sql:
            return []
        if "SELECT ba.name, f.symbol" in sql:
            raise TypeError("bad fill price")
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(monkeypatch, segments=[], query=query)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    with pytest.raises(TypeError, match="bad fill price"):
        module._run_cli()
    output = capsys.readouterr().out
    assert "VERDICT eod day=2026-08-27 report_verdict=COULD_NOT_TELL" in output
    assert output.index("-- CLOSED ROUND TRIPS") < output.index("VERDICT eod")


def test_null_or_zero_fill_price_is_could_not_tell_and_reaches_verdict(
    monkeypatch, capsys
) -> None:
    def query(sql, _params):
        if "/* eod:slot-counts */" in sql:
            return []
        if "/* eod:filled-intents */" in sql:
            return []
        if "SELECT ba.name, f.symbol" in sql:
            return [
                ("live:orb", "BAD", "buy", 1, None, None),
                ("live:orb", "BAD", "sell", 1, 2.0, None),
                ("live:orb", "ZERO", "buy", 1, 0.0, None),
                ("live:orb", "ZERO", "sell", 1, 2.0, None),
            ]
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(monkeypatch, segments=[], query=query)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module._run_cli() == 0
    output = capsys.readouterr().out
    assert "account=live:orb n=0 ambiguous=0 invalid_price=2 verdict=COULD_NOT_TELL" in output
    assert "VERDICT eod day=2026-08-27" in output
    assert "trips_verdict=COULD_NOT_TELL" in output


def test_round_trip_medians_are_split_by_broker_account(monkeypatch, capsys) -> None:
    def query(sql, _params):
        if "/* eod:slot-counts */" in sql or "/* eod:filled-intents */" in sql:
            return []
        if "SELECT ba.name, f.symbol" in sql:
            return [
                ("live:schwab_1m_v2", "SCHW", "buy", 1, 100.0, None),
                ("live:schwab_1m_v2", "SCHW", "sell", 1, 110.0, None),
                ("live:orb", "WEB", "buy", 1, 100.0, None),
                ("live:orb", "WEB", "sell", 1, 80.0, None),
            ]
        raise AssertionError(f"unexpected SQL: {sql}")

    module = _load_script(monkeypatch, segments=[], query=query)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module.main() == 0
    output = capsys.readouterr().out

    assert "account=live:schwab_1m_v2 n=1" in output
    assert "MEDIAN +10.00%" in output
    assert "account=live:orb n=1" in output
    assert "MEDIAN -20.00%" in output
    assert "MEDIAN -5.00%" not in output
    assert "trips=2 trips_verdict=GRADEABLE" in output


def test_settings_failure_is_inside_terminal_verdict_protection(
    monkeypatch, capsys
) -> None:
    module = _load_script(monkeypatch, segments=[], query=lambda _sql, _params: [])
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    def fail_settings():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(module, "get_settings", fail_settings)

    with pytest.raises(RuntimeError, match="settings unavailable"):
        module._run_cli()
    output = capsys.readouterr().out
    assert "VERDICT eod day=2026-08-27 report_verdict=COULD_NOT_TELL" in output
    assert "aborted_before_terminal_verdict" in output


def test_entry_close_comes_from_live_gate_source_and_limit_is_documented(
    monkeypatch, capsys
) -> None:
    module = _load_script(monkeypatch, segments=[], query=lambda _sql, _params: [])
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--day", "2026-08-27"])

    assert module.main() == 0
    output = capsys.readouterr().out

    assert "entry close=16:00 ET" in output
    assert "source=project_mai_tai.strategy_core.entry_gate.resolve_entry_window(get_settings())" in output
    assert "entry_close_et=16:00 entry_close_source=project_mai_tai.strategy_core.entry_gate" in output
    assert "HISTORICAL LIMIT" in module.__doc__
    assert "0/239 Schwab BUY fills" in module.__doc__
