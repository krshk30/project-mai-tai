"""Controlled metadata-only cases; no historical deciding-print proof."""
import json
from types import SimpleNamespace

import pytest

from project_mai_tai.strategy_core.flip_print_diagnostic import (
    FlipPrintDiagnostic, MAX_SYMBOLS, MAX_TRADES_PER_SYMBOL,
)

BAR = 1793290860000  # NCPL 2026-10-29 12:21 ET; controlled clocks/prices below.
END = BAR + 60000


def tick(**changes):
    return SimpleNamespace(**(dict(symbol="NCPL", kind="trade",
        service="TIMESALE_EQUITY", event_ts_ms=END - 2000,
        raw={"1": END - 2000, "conditions": [0, 12]}, raw_hash="a" * 40,
        price=1.7, size=100) | changes))


def fields(observer, **changes):
    line = observer.line(**(dict(symbol="NCPL", flip="SELL", bar_ms=BAR,
        close=1.7101, phase="live", state_only=False) | changes))
    return json.loads(line.split(" ", 1)[1]) if line is not None else None


def test_causal_fields_unknown_even_when_observed_trade_matches_close():
    observer = FlipPrintDiagnostic()
    observer.observe_accepted_tick(tick(price=1.7101), (END - 1000) * 1000000)
    value = fields(observer)
    assert value["deciding_print_ts"] == "UNKNOWN"
    assert value["deciding_print_size"] == "UNKNOWN"
    assert value["deciding_print_conditions"] == "UNKNOWN"
    assert value["deciding_print_seconds_before_minute_end"] == "UNKNOWN"
    assert value["reason"] == "bar_has_no_print_association"
    trade = value["observed_last_trade"]
    assert trade["label"] == "observed_not_bar_cause"
    assert trade["exchange_seconds_before_minute_end"] == "2"
    assert trade["received_seconds_before_minute_end"] == "1"
    assert trade["bar_close_minus_price"] == "0.0000"
    assert value["capture_completeness"] == "UNKNOWN"


def test_no_observations_is_unknown_not_zero_or_bar_volume():
    assert fields(FlipPrintDiagnostic())["observed_last_trade"] == "UNKNOWN"


@pytest.mark.parametrize("exchange,receive", [
    (END, END - 1), (END + 1, END - 1),
    (END - 1, END), (END - 1, END + 1),
])
def test_end_equal_or_later_on_either_clock_is_excluded(exchange, receive):
    observer = FlipPrintDiagnostic()
    observer.observe_accepted_tick(tick(event_ts_ms=exchange, raw={"1": exchange}), receive * 1000000)
    assert fields(observer)["observed_last_trade"] == "UNKNOWN"


def test_latest_arrival_wins_without_exchange_sorting_and_late_arrival_cannot_rewrite():
    observer = FlipPrintDiagnostic()
    observer.observe_accepted_tick(tick(price=1.6), (END - 3000) * 1000000)
    observer.observe_accepted_tick(tick(price=1.7, event_ts_ms=END - 4000,
        raw={"1": END - 4000}), (END - 2000) * 1000000)
    observer.observe_accepted_tick(tick(price=9.0), (END + 1000) * 1000000)
    trade = fields(observer)["observed_last_trade"]
    assert trade["price"] == "1.7"
    assert trade["exchange_ts_ms"] == END - 4000
    assert trade["bar_close_minus_price"] == "0.0101"


@pytest.mark.parametrize("raw", [{}, {"1": None}, {"1": 0}, {"1": True}, {"1": END - 1}])
def test_envelope_fallback_or_conflicting_exchange_field_not_accepted(raw):
    observer = FlipPrintDiagnostic()
    observer.observe_accepted_tick(tick(raw=raw), (END - 1000) * 1000000)
    assert fields(observer)["observed_last_trade"] == "UNKNOWN"


def test_levelone_explicit_trade_clock_and_missing_conditions_are_separate():
    observer = FlipPrintDiagnostic()
    observer.observe_accepted_tick(tick(service="LEVELONE_EQUITIES",
        raw={"35": END - 2000}), (END - 1000) * 1000000)
    trade = fields(observer)["observed_last_trade"]
    assert trade["conditions"] == "UNKNOWN"
    assert trade["size"] == 100
    assert trade["received_clock"] == "UTC_epoch_service_tick_callback_entry"


def test_conditions_copied_and_log_injection_not_emitted():
    observer = FlipPrintDiagnostic()
    original = [12]
    observer.observe_accepted_tick(tick(raw={"1": END - 2000, "c": original}), (END - 1000) * 1000000)
    original.append(99)
    assert fields(observer)["observed_last_trade"]["conditions"] == [12]
    observer.observe_accepted_tick(tick(raw={"1": END - 2000, "c": "\nERROR token"}), (END - 999) * 1000000)
    assert fields(observer)["observed_last_trade"]["conditions"] == "UNKNOWN"


@pytest.mark.parametrize("change", [{"phase": "replay"}, {"state_only": True},
    {"flip": "none"}, {"bar_ms": BAR + 1}, {"symbol": "NCPL\nERROR"}])
def test_non_real_or_invalid_flip_is_not_logged(change):
    assert fields(FlipPrintDiagnostic(), **change) is None


def test_retention_hard_caps_and_evicted_symbol_is_unknown():
    observer = FlipPrintDiagnostic()
    for index in range(MAX_SYMBOLS + 1):
        for offset in range(MAX_TRADES_PER_SYMBOL + 1):
            observer.observe_accepted_tick(tick(symbol=f"S{index}"), (END - 1000) * 1000000 + offset)
    assert len(observer._trades) == MAX_SYMBOLS
    assert all(len(queue) == MAX_TRADES_PER_SYMBOL for queue in observer._trades.values())
    assert fields(observer, symbol="S0")["observed_last_trade"] == "UNKNOWN"
    assert fields(observer, symbol=f"S{MAX_SYMBOLS}")["observed_last_trade"]["size"] == 100


def test_received_order_not_received_timestamp_sort_even_if_utc_clock_moves_back():
    observer = FlipPrintDiagnostic()
    observer.observe_accepted_tick(tick(price=1.6), (END - 1000) * 1000000)
    observer.observe_accepted_tick(tick(price=1.7), (END - 2000) * 1000000)
    assert fields(observer)["observed_last_trade"]["price"] == "1.7"


def test_earlier_bar_cannot_select_future_received_trade_but_next_bar_can():
    observer = FlipPrintDiagnostic()
    observer.observe_accepted_tick(tick(), (END + 1000) * 1000000)
    assert fields(observer)["observed_last_trade"] == "UNKNOWN"
    assert fields(observer, bar_ms=END)["observed_last_trade"]["price"] == "1.7"


def test_zero_observed_size_is_retained_and_missing_size_is_unknown_not_zero():
    observer = FlipPrintDiagnostic()
    observer.observe_accepted_tick(tick(size=0), (END - 2000) * 1000000)
    assert fields(observer)["observed_last_trade"]["size"] == 0
    observer.observe_accepted_tick(tick(size=None), (END - 1000) * 1000000)
    assert fields(observer)["observed_last_trade"]["size"] == "UNKNOWN"


def test_quote_and_nonfinite_price_never_become_trade_observations():
    observer = FlipPrintDiagnostic()
    observer.observe_accepted_tick(tick(kind="quote"), (END - 2000) * 1000000)
    observer.observe_accepted_tick(tick(price=float("nan")), (END - 1000) * 1000000)
    assert fields(observer)["observed_last_trade"] == "UNKNOWN"
