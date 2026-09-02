import inspect
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from actual_resting_entry_extrema import Fill
from actual_resting_operator_rule import (
    HALT_MIN_PRINT_GAP,
    HALT_MIN_QUOTE_UPDATES,
    HaltWindow,
    LegResult,
    QuotePoint,
    choose_boundary,
    choose_outcome,
    choose_yardstick,
    detect_halt_windows,
    defer_halted_boundary,
    event_outcome,
    halt_exclusion,
    load_population,
    measurement_end,
    quote_points,
    timestamp_is_halted,
    window_contains_halt,
)


def _fill(
    account: str,
    quantity: str = "1",
    *,
    fill_id: str | None = None,
    slot: str = "first",
    cw_flip_level: str | None = "9.73",
) -> Fill:
    return Fill(
        fill_id=fill_id or account,
        account=account,
        symbol="TEST",
        side="buy",
        quantity=Decimal(quantity),
        price=Decimal("10"),
        at=datetime(2026, 9, 1, 13, tzinfo=UTC),
        client_order_id="",
        order_type="STOP_LIMIT",
        reason="",
        fill_source="",
        target_price=None,
        stop_price=None,
        slot=slot,
        cw_flip_level=Decimal(cw_flip_level) if cw_flip_level is not None else None,
    )


def test_executable_prices_come_from_quotes_not_trade_prints() -> None:
    source = inspect.getsource(quote_points)
    execution_source, sanity_source = source.split("print_high =", 1)
    assert "market_capture_quotes" in execution_source
    assert "market_capture_trades" not in execution_source
    assert "market_capture_trades" in sanity_source


def test_luld_definition_is_predeclared_five_minute_shape() -> None:
    assert HALT_MIN_PRINT_GAP == timedelta(seconds=285)
    assert HALT_MIN_QUOTE_UPDATES == 2


def test_halt_detection_is_data_driven_not_symbol_listed() -> None:
    source = inspect.getsource(detect_halt_windows)
    assert "market_capture_trades" in source
    assert "market_capture_quotes" in source
    assert ":minimum_gap" in source
    assert ":minimum_quotes" in source
    assert "confirmed_halt_window" in source
    assert "DAIC" not in source


def test_halt_exclusion_removes_only_quotes_strictly_inside_pause() -> None:
    start = datetime(2026, 8, 24, 15, 21, 14, tzinfo=UTC)
    end = start + timedelta(minutes=5)
    halt = HaltWindow(start, end, 56)

    clause, params = halt_exclusion([halt])

    assert clause == "NOT (event_ts>:halt_start_0 AND event_ts<:halt_end_0)"
    assert params == {"halt_start_0": start, "halt_end_0": end}
    assert not timestamp_is_halted(start, [halt])
    assert timestamp_is_halted(start + timedelta(seconds=1), [halt])
    assert not timestamp_is_halted(end, [halt])


def test_every_bid_measurement_and_trigger_query_uses_halt_exclusion() -> None:
    source = inspect.getsource(quote_points)
    execution_source, sanity_source = source.split("print_high =", 1)
    assert execution_source.count("{tradable}") == 8
    assert "{tradable}" not in sanity_source


def test_halted_flip_defers_to_reopen_and_overlapping_window_is_exposed() -> None:
    start = datetime(2026, 8, 24, 15, 21, 14, tzinfo=UTC)
    end = start + timedelta(minutes=5)
    halt = HaltWindow(start, end, 56)

    effective, deferred = defer_halted_boundary(start + timedelta(minutes=2), [halt])

    assert deferred is True
    assert effective == end
    assert window_contains_halt(start - timedelta(minutes=1), end, [halt])


def test_flip_exit_time_is_first_quote_after_reopen() -> None:
    fill = _fill("live:schwab_1m_v2")
    reopen = fill.at.replace(minute=1)
    first_quote = reopen + timedelta(seconds=2)

    result = choose_outcome(
        fill,
        reopen,
        fill.at.replace(hour=20),
        {
            "first": QuotePoint(fill.at, Decimal("10")),
            "target": None,
            "stop": None,
            "endpoint_after": QuotePoint(first_quote, Decimal("9.8")),
            "endpoint_before": None,
        },
        sell_deferred=True,
    )

    assert result.outcome == "exited on ATR flip"
    assert result.trigger_at == first_quote
    assert "executed after reopen" in result.note


def test_halted_threshold_executes_at_reopen_even_if_reopen_bid_receded() -> None:
    fill = _fill("live:schwab_1m_v2")
    observed = fill.at + timedelta(seconds=20)
    reopen = fill.at + timedelta(minutes=5)
    result = choose_outcome(
        fill,
        fill.at + timedelta(minutes=10),
        fill.at.replace(hour=20),
        {
            "first": QuotePoint(fill.at, Decimal("10")),
            "target": QuotePoint(reopen, Decimal("10.20"), observed),
            "stop": None,
            "endpoint_after": None,
            "endpoint_before": None,
        },
    )

    assert result.outcome == "exited at +5%"
    assert result.trigger_at == reopen
    assert result.return_pct == Decimal("2.00")
    assert "observed during halt" in result.note


def test_halted_threshold_without_reopen_quote_is_unanswerable() -> None:
    fill = _fill("live:schwab_1m_v2")
    observed = fill.at + timedelta(seconds=20)
    reopen = fill.at + timedelta(minutes=5)
    result = choose_outcome(
        fill,
        fill.at + timedelta(minutes=10),
        fill.at.replace(hour=20),
        {
            "first": QuotePoint(fill.at, Decimal("10")),
            "target": None,
            "stop": None,
            "target_unanswerable": (reopen, observed),
            "endpoint_after": QuotePoint(fill.at + timedelta(minutes=10), Decimal("10")),
            "endpoint_before": None,
        },
    )

    assert result.outcome == "UNANSWERABLE"
    assert result.trigger_at == reopen
    assert "no executable quote" in result.note


def test_halted_target_and_stop_order_by_observation_before_shared_reopen() -> None:
    fill = _fill("live:schwab_1m_v2")
    reopen = fill.at + timedelta(minutes=5)
    result = choose_outcome(
        fill,
        fill.at + timedelta(minutes=10),
        fill.at.replace(hour=20),
        {
            "first": QuotePoint(fill.at, Decimal("10")),
            "target": QuotePoint(reopen, Decimal("9.50"), fill.at + timedelta(seconds=30)),
            "stop": QuotePoint(reopen, Decimal("9.50"), fill.at + timedelta(seconds=20)),
            "endpoint_after": None,
            "endpoint_before": None,
        },
    )

    assert result.outcome == "exited at -8%"
    assert result.return_pct == Decimal("-5.00")


def test_real_resting_population_excludes_reclaim_and_never_derives_entries(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy.csv"
    legacy.write_text("date_et,fill_id\n2026-09-01,Webull: legacy-real-fill\n")
    fills = [
        _fill("live:orb", fill_id="stamped-first", slot="first"),
        _fill("live:orb", fill_id="stamped-reclaim", slot="reclaim"),
        _fill("live:orb", fill_id="legacy-real-fill", slot=""),
        _fill("live:orb", fill_id="unvetted-fill", slot=""),
    ]

    events = load_population(
        legacy,
        fills,
        start_day="2026-09-01",
        end_day="2026-09-01",
    )

    assert {fill.fill_id for _, legs in events for fill in legs} == {
        "stamped-first",
        "legacy-real-fill",
    }


def test_real_resting_population_requires_stamped_flip_level(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.csv"
    legacy.write_text("date_et,fill_id\n")

    with pytest.raises(RuntimeError, match="no stamped cw_flip_level"):
        load_population(
            legacy,
            [_fill("live:orb", cw_flip_level=None)],
            start_day="2026-09-01",
            end_day="2026-09-01",
        )


def test_sell_boundary_clips_every_threshold_query_before_close() -> None:
    source = inspect.getsource(quote_points)
    assert source.count("event_ts<=:endpoint") >= 5
    assert '"endpoint": endpoint' in source

    sell = datetime(2026, 9, 1, 15, tzinfo=UTC)
    close = datetime(2026, 9, 1, 20, tzinfo=UTC)
    assert measurement_end(sell, close) is sell
    assert measurement_end(None, close) is close
    assert "measurement_end(sell_at, cutoff)" in inspect.getsource(
        sys.modules[measurement_end.__module__].main
    )


def test_runner_is_structurally_read_only() -> None:
    source = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath("scripts/actual_resting_operator_rule.py")
        .read_text()
    )
    upper = source.upper()
    assert "BROKER_ADAPTERS" not in upper
    assert " INSERT " not in upper
    assert " UPDATE " not in upper
    assert " DELETE " not in upper


def test_event_outcome_weights_broker_legs_by_actual_quantity() -> None:
    schwab = _fill("live:schwab_1m_v2", "2")
    webull = _fill("live:orb", "1")
    outcome, result, note = event_outcome(
        [
            LegResult(
                schwab,
                "exited at +5%",
                schwab.at,
                Decimal("10.5"),
                Decimal("5"),
                "endpoint-independent",
            ),
            LegResult(
                webull,
                "exited at +5%",
                webull.at,
                Decimal("10.6"),
                Decimal("6"),
                "endpoint-independent",
            ),
        ]
    )
    assert outcome == "exited at +5%"
    assert result == Decimal("16") / Decimal("3")
    assert note == "endpoint-independent"


def test_event_outcome_refuses_cross_broker_trigger_disagreement() -> None:
    schwab = _fill("live:schwab_1m_v2")
    webull = _fill("live:orb")
    outcome, result, note = event_outcome(
        [
            LegResult(
                schwab,
                "exited at +5%",
                schwab.at,
                Decimal("10.5"),
                Decimal("5"),
                "endpoint-independent",
            ),
            LegResult(
                webull,
                "exited at -8%",
                webull.at,
                Decimal("9.2"),
                Decimal("-8"),
                "endpoint-independent",
            ),
        ]
    )
    assert outcome == "UNANSWERABLE"
    assert result is None
    assert "broker legs disagree" in note


def test_target_before_stop_wins_by_timestamp() -> None:
    fill = _fill("live:schwab_1m_v2")
    target_at = fill.at.replace(second=2)
    stop_at = fill.at.replace(second=3)
    result = choose_outcome(
        fill,
        fill.at.replace(minute=1),
        fill.at.replace(hour=20),
        {
            "first": QuotePoint(fill.at, Decimal("10")),
            "target": QuotePoint(target_at, Decimal("10.5")),
            "stop": QuotePoint(stop_at, Decimal("9.2")),
            "endpoint_after": None,
            "endpoint_before": None,
        },
    )
    assert result.outcome == "exited at +5%"
    assert result.trigger_at == target_at


def test_sell_before_target_uses_caveated_endpoint() -> None:
    fill = _fill("live:schwab_1m_v2")
    sell_at = fill.at.replace(second=2)
    result = choose_outcome(
        fill,
        sell_at,
        fill.at.replace(hour=20),
        {
            "first": QuotePoint(fill.at, Decimal("10")),
            "target": None,
            "stop": None,
            "endpoint_after": QuotePoint(sell_at, Decimal("9.8")),
            "endpoint_before": None,
        },
    )
    assert result.outcome == "exited on ATR flip"
    assert result.return_pct == Decimal("-2.00")
    assert "recalculated" in result.note


def test_no_stop_yardstick_uses_endpoint_when_target_never_trades() -> None:
    fill = _fill("live:schwab_1m_v2")
    sell_at = fill.at.replace(second=4)
    result = choose_yardstick(
        fill,
        sell_at,
        fill.at.replace(hour=20),
        {
            "first": QuotePoint(fill.at, Decimal("10")),
            "target": None,
            "target10": None,
            "endpoint_after": QuotePoint(sell_at, Decimal("9.7")),
            "endpoint_before": None,
        },
        target_key="target10",
        target_label="exited at +10%",
    )
    assert result.outcome == "exited on ATR flip"
    assert result.return_pct == Decimal("-3.00")


def test_stopped_counterfactual_uses_halt_aware_atr_sell_quote() -> None:
    fill = _fill("live:schwab_1m_v2")
    sell_at = fill.at + timedelta(minutes=5)
    executable_at = sell_at + timedelta(seconds=2)

    result = choose_boundary(
        fill,
        sell_at,
        fill.at.replace(hour=20),
        {
            "first": QuotePoint(fill.at, Decimal("10")),
            "endpoint_after": QuotePoint(executable_at, Decimal("8.50")),
            "endpoint_before": None,
        },
        sell_deferred=True,
    )

    assert result.outcome == "exited on ATR flip"
    assert result.trigger_at == executable_at
    assert result.return_pct == Decimal("-15.00")
    assert "executed after reopen" in result.note
