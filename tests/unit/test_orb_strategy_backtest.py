import inspect
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from orb_strategy_backtest import (
    DISCLOSURE,
    EntryPlan,
    QuotePoint,
    TradePoint,
    choose_entry_population,
    detect_halts,
    evaluate_entry,
    render,
    replay_simulated_symbol,
    stamped_trail_pct,
)
from project_mai_tai.market_halts import HALT_MIN_PRINT_GAP, HALT_MIN_QUOTE_UPDATES
from project_mai_tai.settings import Settings


DAY = date(2026, 9, 3)


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 3, hour, minute, second, tzinfo=UTC)


def _entry(*, price: str = "100", trail: str = "5", source: str = "SIMULATED") -> EntryPlan:
    return EntryPlan(
        symbol="TEST",
        at=_at(13, 31),
        price=Decimal(price),
        metadata={"trail_pct": trail, "orb_intended_break_level": price},
        source=source,
        note="ASSUMED ASK FILL" if source == "SIMULATED" else "READ FROM DURABLE FILL",
    )


def _settings(**overrides) -> Settings:
    values = {
        "orb_running_high_enabled": True,
        "orb_intrabar_reclaim_enabled": False,
        "orb_oms_quote_priced_entry_enabled": True,
        "orb_resting_entry_enabled": False,
        "orb_reclaim_trail_pct": 5.0,
        "orb_reclaim_quantity": 1,
        "orb_running_high_gap_cap_pct": 1.5,
        "orb_running_high_window_minutes": 30,
        "orb_window_flatten_enabled": True,
        "orb_window_flatten_hour_et": 10,
        "orb_window_flatten_minute_et": 0,
    }
    values.update(overrides)
    return Settings(**values)


def test_shared_halt_contract_is_the_deployed_285_second_rule() -> None:
    assert HALT_MIN_PRINT_GAP == timedelta(seconds=285)
    assert HALT_MIN_QUOTE_UPDATES == 2
    assert "confirmed_halt_window" in inspect.getsource(detect_halts)


def test_halted_bid_cannot_fire_an_exit() -> None:
    entry = _entry()
    last_print = _at(13, 31, 5)
    reopen = _at(13, 36, 5)
    trades = [
        TradePoint(last_print, Decimal("100"), 1),
        TradePoint(reopen, Decimal("99"), 1),
    ]
    quotes = [
        QuotePoint(_at(13, 32), Decimal("90"), Decimal("91")),
        QuotePoint(_at(13, 33), Decimal("91"), Decimal("92")),
        QuotePoint(reopen, Decimal("99"), Decimal("100")),
        QuotePoint(_at(13, 37), Decimal("98"), Decimal("99")),
    ]
    halts = detect_halts(trades, quotes)

    row = evaluate_entry(entry, quotes, halts, _at(13, 37))

    assert len(halts) == 1
    assert row.exit_rule == "WINDOW_FLATTEN"
    assert row.exit_price == Decimal("98")
    assert row.low_bid == Decimal("98")


def test_extrema_and_trailing_exit_use_bid_not_trade_print_or_ask() -> None:
    entry = _entry()
    quotes = [
        QuotePoint(_at(13, 31, 1), Decimal("104"), Decimal("120")),
        QuotePoint(_at(13, 32), Decimal("98"), Decimal("119")),
    ]

    row = evaluate_entry(entry, quotes, [], _at(13, 32))

    assert row.high_bid == Decimal("104")
    assert row.high_pct == Decimal("4.00")
    assert row.exit_price == Decimal("98")


def test_zero_bid_is_not_executable() -> None:
    entry = _entry()
    quotes = [
        QuotePoint(_at(13, 31, 1), Decimal("0"), Decimal("120")),
        QuotePoint(_at(13, 32), Decimal("99"), Decimal("100")),
    ]

    row = evaluate_entry(entry, quotes, [], _at(13, 32))

    assert row.high_bid == Decimal("99")
    assert row.low_bid == Decimal("99")
    assert row.exit_price == Decimal("99")


def test_real_fill_population_replaces_simulation_for_the_whole_session() -> None:
    real = _entry(price="7.73", source="REAL_FILL")
    simulated = _entry(price="8.20")

    selected = choose_entry_population([real], [simulated])

    assert selected == [real]
    assert selected[0].price == Decimal("7.73")


def test_stamped_trail_level_controls_exit_without_config_fallback() -> None:
    entry = _entry(trail="10")
    quotes = [
        QuotePoint(_at(13, 31, 1), Decimal("93"), Decimal("94")),
        QuotePoint(_at(13, 32), Decimal("94"), Decimal("95")),
    ]

    row = evaluate_entry(entry, quotes, [], _at(13, 32))

    assert stamped_trail_pct(entry.metadata) == Decimal("10")
    assert row.exit_rule == "WINDOW_FLATTEN"
    assert row.exit_at == _at(13, 32)


def test_missing_stamped_trail_is_unanswerable_not_recomputed() -> None:
    entry = EntryPlan("TEST", _at(13, 31), Decimal("100"), {}, "REAL_FILL", "")
    row = evaluate_entry(entry, [], [], _at(14, 0))
    assert row.exit_rule == "UNANSWERABLE"
    assert "stamped trail_pct" in row.note


def test_deployed_running_high_signal_and_quote_pricing_drive_simulated_fill() -> None:
    trades = [
        TradePoint(_at(13, 25, 10), Decimal("10.00"), 100),
        TradePoint(_at(13, 26, 10), Decimal("10.10"), 100),
        TradePoint(_at(13, 30, 10), Decimal("10.20"), 100),
        TradePoint(_at(13, 30, 30), Decimal("10.30"), 100),
        TradePoint(_at(13, 31, 0), Decimal("10.20"), 100),
        TradePoint(_at(13, 32, 0), Decimal("10.00"), 100),
    ]
    quotes = [
        QuotePoint(_at(13, 30, 59), Decimal("10.12"), Decimal("10.15")),
        QuotePoint(_at(13, 31, 1), Decimal("9.60"), Decimal("9.65")),
    ]

    result = replay_simulated_symbol(
        day=DAY,
        symbol="TEST",
        trades=trades,
        quotes=quotes,
        settings=_settings(),
        universe={"TEST"},
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.entry.at == _at(13, 31)
    assert row.entry.price == Decimal("10.1500")
    assert row.entry.metadata["orb_intended_break_level"] == "10.2000"
    assert row.entry.metadata["trail_pct"] == "5.0"
    assert row.exit_rule == "TRAIL-5%"
    assert row.exit_price == Decimal("9.60")


def test_quote_priced_entry_abandons_stale_ask() -> None:
    trades = [
        TradePoint(_at(13, 25, 10), Decimal("10.00"), 100),
        TradePoint(_at(13, 26, 10), Decimal("10.10"), 100),
        TradePoint(_at(13, 30, 10), Decimal("10.20"), 100),
        TradePoint(_at(13, 30, 30), Decimal("10.30"), 100),
        TradePoint(_at(13, 31), Decimal("10.20"), 100),
    ]
    quotes = [QuotePoint(_at(13, 30, 50), Decimal("10.12"), Decimal("10.15"))]

    result = replay_simulated_symbol(
        day=DAY,
        symbol="TEST",
        trades=trades,
        quotes=quotes,
        settings=_settings(),
        universe={"TEST"},
    )

    assert result.rows == []
    assert result.abandoned == {"NO_FRESH_QUOTE": 1}


def test_render_states_simulation_and_assumption_once() -> None:
    row = evaluate_entry(
        _entry(),
        [QuotePoint(_at(13, 32), Decimal("99"), Decimal("100"))],
        [],
        _at(13, 32),
    )
    report = render([row], {})
    assert report.count(DISCLOSURE) == 1
    assert "ASSUMED ASK FILL" in report
    assert "1/1 gradable" in report


def test_runner_sql_is_read_only() -> None:
    source = (
        Path(__file__).resolve().parents[2].joinpath("scripts/orb_strategy_backtest.py").read_text()
    )
    upper = source.upper()
    assert "BROKER_ADAPTER" not in upper
    assert " INSERT " not in upper
    assert " UPDATE " not in upper
    assert " DELETE " not in upper
