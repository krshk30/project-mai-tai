import sys
from pathlib import Path

from scripts import resting_entry_slippage as slippage
from scripts.resting_entry_slippage import format_slot_coverage, resolve_entry_slot


SCRIPT = Path(__file__).parents[2] / "scripts" / "resting_entry_slippage.py"


def _row(**overrides):
    row = {
        "symbol": "CELU",
        "stop_price": "1.23",
        "limit_price": "1.24",
        "entry_slot": "",
        # Deliberately tempting legacy inputs: neither may decide economic slot.
        "resting": "true",
        "order_type": "STOP_LIMIT",
    }
    row.update(overrides)
    return row


def test_durable_entry_slot_outranks_conflicting_historical_log() -> None:
    index = {("CELU", "1.23", "1.24"): "first"}

    assert resolve_entry_slot(_row(entry_slot="reclaim"), index) == ("reclaim", False)


def test_pre_821_history_can_fall_back_to_exact_placement_log() -> None:
    index = {("CELU", "1.23", "1.24"): "first"}

    assert resolve_entry_slot(_row(), index) == ("first", False)


def test_order_style_is_never_used_as_economic_slot_proxy() -> None:
    assert resolve_entry_slot(_row(resting="true"), {}) == ("unattributed", True)


def test_query_reads_cw_entry_slot_from_trade_intent_metadata() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "ti.payload->'metadata'->>'cw_entry_slot'" in source
    assert "LEFT JOIN trade_intents ti ON ti.id = bo.intent_id" in source
    assert "bo.payload->>'resting_entry'          AS resting" not in source
    assert "format_slot_coverage(attributed, slot_population)" in source


def test_zero_slot_coverage_is_could_not_tell_not_numeric_zero() -> None:
    result = format_slot_coverage(0, 239)

    assert result.startswith("cw_entry_slot coverage=0/239 -- COULD_NOT_TELL")
    assert "239 fills have unknown classification" in result
    assert "0.0%" not in result


def test_empty_slot_population_names_zero_denominator() -> None:
    result = format_slot_coverage(0, 0)

    assert result.startswith("cw_entry_slot coverage=0/0 -- COULD_NOT_TELL")
    assert "denominator=0" in result
    assert "MISSING_OR_ROTATED" not in result


def test_complete_slot_population_is_gradeable() -> None:
    assert format_slot_coverage(2, 2) == (
        "cw_entry_slot coverage=2/2 = 100.0% -- GRADEABLE"
    )


def test_slot_numerator_above_denominator_is_ungradeable_not_exception() -> None:
    result = format_slot_coverage(3, 2)

    assert result.startswith("cw_entry_slot coverage=3/2 -- COULD_NOT_TELL")
    assert "numerator must not exceed denominator" in result


def test_missing_or_rotated_service_logs_are_not_a_zero_marker_population(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(slippage.glob, "glob", lambda _pattern: [])

    index, verdict = slippage.load_slot_index()
    output = capsys.readouterr().out

    assert index == {}
    assert verdict == "MISSING_OR_ROTATED"
    assert "SERVICE LOGS MISSING_OR_ROTATED (0 files)" in output
    assert "not a zero placement population" in output


def test_readable_service_log_with_no_markers_is_distinct_from_missing(
    monkeypatch, tmp_path, capsys
) -> None:
    retained_log = tmp_path / "schwab-1m-v2.log"
    retained_log.write_text("2026-08-28 heartbeat healthy\n", encoding="utf-8")
    monkeypatch.setattr(slippage.glob, "glob", lambda _pattern: [str(retained_log)])

    index, verdict = slippage.load_slot_index()
    output = capsys.readouterr().out

    assert index == {}
    assert verdict == "AVAILABLE_NO_MARKERS"
    assert "0 placement markers across readable retained service logs" in output
    assert "MISSING_OR_ROTATED" not in output


def test_readable_service_log_with_marker_is_available(monkeypatch, tmp_path) -> None:
    retained_log = tmp_path / "schwab-1m-v2.log"
    retained_log.write_text(
        "[V2-RESTING-PLACE] CELU slot=reclaim stop=1.23 limit=1.24\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(slippage.glob, "glob", lambda _pattern: [str(retained_log)])

    index, verdict = slippage.load_slot_index()

    assert verdict == "AVAILABLE"
    assert index[("CELU", "1.23", "1.24")] == "reclaim"


def test_terminal_output_keeps_log_evidence_separate_from_zero_denominator(
    monkeypatch, capsys
) -> None:
    class EmptyResult:
        def mappings(self):
            return self

        def all(self):
            return []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    monkeypatch.setattr(slippage, "get_settings", lambda: object())
    monkeypatch.setattr(slippage, "build_session_factory", lambda _settings: Session)
    monkeypatch.setattr(
        slippage,
        "load_slot_index",
        lambda: ({}, "MISSING_OR_ROTATED"),
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--days", "1"])

    assert slippage.main() == 0
    output = capsys.readouterr().out

    assert "cw_entry_slot coverage=0/0 -- COULD_NOT_TELL (denominator=0" in output
    assert "cw_entry_slot_coverage=0/0 historical_log_verdict=MISSING_OR_ROTATED" in output
