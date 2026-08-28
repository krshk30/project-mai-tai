from pathlib import Path

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


def test_complete_slot_population_is_gradeable() -> None:
    assert format_slot_coverage(2, 2) == (
        "cw_entry_slot coverage=2/2 = 100.0% -- GRADEABLE"
    )
