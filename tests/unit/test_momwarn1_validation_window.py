from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.momentum_paper.engine import MomentumPaperEngine, detection_time_is_eligible
from project_mai_tai.momentum_paper.models import TradePrint
from project_mai_tai.momwarn_validation_window import MomentumValidationWindow
from project_mai_tai.settings import Settings

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 13)


def window():
    return MomentumValidationWindow(DAY, time(18), time(18), time(18, 10))


def ms(day, clock):
    return int(datetime.combine(day, clock, ET).astimezone(UTC).timestamp() * 1000)


@pytest.mark.parametrize("day", [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)])
def test_approved_daily_window_without_date_gate(day):
    value = MomentumValidationWindow()
    assert value.gateway_end_for(day) == time(18)
    assert value.detection_end_for(day) == time(18)
    assert value.guard_end_for(day) == time(18, 10)
    assert value.paper_close_for(day) == time(18, 10, 1)
    assert detection_time_is_eligible(ms(day, time(9, 30)), value)
    assert not detection_time_is_eligible(ms(day, time(18)), value)


def test_explicit_limits_are_daily_even_with_legacy_date():
    value = window()
    assert value.gateway_end_for(DAY) == time(18)
    assert value.detection_end_for(DAY) == time(18)
    assert value.guard_end_for(DAY) == time(18, 10)
    assert value.paper_close_for(DAY) == time(18, 10, 1)
    for other in (date(2026, 10, 12), date(2026, 10, 14)):
        assert value.gateway_end_for(other) == time(18)
        assert value.detection_end_for(other) == time(18)
        assert value.guard_end_for(other) == time(18, 10)
        assert detection_time_is_eligible(ms(other, time(12)), value)


@pytest.mark.parametrize("clock,expected", [
    (time(4, 10, 59), False), (time(4, 11), True), (time(9, 30), True),
    (time(17, 59, 59), True), (time(18), False),
])
def test_extended_detection_boundaries(clock, expected):
    assert detection_time_is_eligible(ms(DAY, clock), window()) is expected


@pytest.mark.parametrize("args", [
    (None, time(17), time(18), time(18, 10)),
    (DAY, time(17), time(18), time(18, 10)),
    (DAY, time(18), time(18), time(18, 9)),
    (DAY, time(18, 0, 1), time(18), time(18, 10)),
    (DAY, time(21), time(18), time(18, 10)),
])
def test_malformed_or_unbound_configuration_refuses(args):
    with pytest.raises(ValueError):
        MomentumValidationWindow(*args)


def test_settings_parse_explicit_date_and_clocks():
    settings = Settings(_env_file=None, momentum_validation_session_date="2026-10-13",
                        market_data_momentum_warning_end_et="18:00",
                        momentum_paper_detection_end_et="18:00",
                        momentum_paper_guard_end_et="18:10")
    assert MomentumValidationWindow.from_settings(settings) == window()


def test_post0930_detection_is_labelled_and_next_print_still_fills():
    value = MomentumPaperEngine(prior_closes={"TEST": Decimal("1")},
                                condition_version="controlled", coverage_started_ms=ms(DAY, time(4)),
                                validation_window=window())
    def trade(clock, price):
        return TradePrint("TEST", ms(DAY, clock), Decimal(price), 100)
    assert not value.ingest(trade(time(10), "1"))
    rows = value.ingest(trade(time(10, 0, 20), "1.20"))
    detects = [row for row in rows if row.event_type == "DETECTED"]
    assert len(detects) == 2
    assert all(row.payload["validation_window"] == "validation-window" for row in detects)
    fills = value.ingest(trade(time(10, 0, 21), "1.21"))
    assert len([row for row in fills if row.event_type == "FILLED"]) == 2
    assert all(row.payload["validation_window"] == "validation-window"
               for row in fills if row.event_type == "FILLED")


def test_daily_override_detects_next_day_with_unchanged_next_print_rule():
    tomorrow = date(2026, 10, 14)
    value = MomentumPaperEngine(prior_closes={"TEST": Decimal("1")},
                                condition_version="controlled", coverage_started_ms=ms(tomorrow, time(4)),
                                validation_window=window())
    assert not value.ingest(TradePrint("TEST", ms(tomorrow, time(10)), Decimal("1"), 100))
    records = value.ingest(TradePrint("TEST", ms(tomorrow, time(10, 0, 20)), Decimal("1.20"), 100))
    assert len([row for row in records if row.event_type == "DETECTED"]) == 2
    records = value.ingest(TradePrint("TEST", ms(tomorrow, time(10, 0, 21)), Decimal("1.21"), 100))
    assert len([row for row in records if row.event_type == "FILLED"]) == 2


def test_explicit_clock_override_needs_no_session_date():
    value = MomentumValidationWindow(None, time(9, 30), time(9, 30), time(9, 40))
    assert value.gateway_end_for(date(2026, 10, 14)) == time(9, 30)
    assert not detection_time_is_eligible(ms(date(2026, 10, 14), time(9, 30)), value)
