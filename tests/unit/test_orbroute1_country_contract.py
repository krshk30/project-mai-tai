"""ORBROUTE1 20:3x: fixed ten-share selection, not an order or ownership grant."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from project_mai_tai.momentum_live_sec import CountryRecord, CountryRoute, CountryRouter
from project_mai_tai.orb_country_route import ORB_ROUTE_QUANTITY, select_orb_country_route

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)
SCHWAB = "live:schwab_1m_v2"
WEBULL = "live:orb"


def router_with(record):
    router = CountryRouter(None)
    router._records = {} if record is None else {"ABC": record}
    return router


def record_with(is_us, *, status="READY", as_of=NOW - timedelta(days=1)):
    return CountryRecord("ABC", "0000000001", "NY" if is_us else "A6",
                         "DE" if is_us else "A6", is_us, as_of, status, as_of.date())


@pytest.mark.parametrize(
    "record,account,reason",
    [
        (record_with(True), SCHWAB, "sec_us"),
        (record_with(False), WEBULL, "sec_non_us"),
        (None, WEBULL, "sec_unknown"),
        (record_with(None, status="FETCH_FAILED"), WEBULL, "sec_unknown"),
        (record_with(None, status="NO_CIK"), WEBULL, "sec_unknown"),
        (record_with(None, status="INCOMPLETE"), WEBULL, "sec_unknown"),
        (record_with(True, as_of=NOW + timedelta(seconds=1)), WEBULL, "sec_unknown"),
    ],
)
def test_original_router_selects_exactly_one_ten_share_venue(record, account, reason):
    selection = select_orb_country_route(
        router_with(record), "ABC", NOW, schwab_account=SCHWAB, webull_account=WEBULL
    )
    assert selection.broker_account_name == account
    assert selection.country.reason == reason
    assert selection.quantity == ORB_ROUTE_QUANTITY == Decimal("10")


@pytest.mark.parametrize("now", [NOW.replace(hour=13, minute=28), NOW.replace(hour=14)])
def test_country_selection_does_not_change_between_existing_preopen_and_rth_calls(now):
    selection = select_orb_country_route(
        router_with(record_with(False)), "ABC", now, schwab_account=SCHWAB, webull_account=WEBULL
    )
    assert selection.broker_account_name == WEBULL
    assert selection.quantity == Decimal("10")


@pytest.mark.parametrize("is_us", [True, False, None])
def test_missing_selected_account_never_becomes_a_second_venue(is_us):
    with pytest.raises(ValueError, match="orb_country_account_missing"):
        select_orb_country_route(
            router_with(record_with(is_us)), "ABC", NOW,
            schwab_account="" if is_us is True else SCHWAB,
            webull_account=WEBULL if is_us is True else "",
        )


def test_country_selection_is_a_sealed_value_not_a_later_reroute():
    router = router_with(record_with(False))
    selected = select_orb_country_route(
        router, "ABC", NOW, schwab_account=SCHWAB, webull_account=WEBULL
    )
    router._records["ABC"] = record_with(True)
    assert selected.broker_account_name == WEBULL
    assert selected.country.reason == "sec_non_us"


@pytest.mark.parametrize("symbol", ["", "abc", " ABC", "ABC "])
def test_invalid_symbol_does_not_gain_a_route(symbol):
    with pytest.raises(ValueError, match="orb_country_symbol_invalid"):
        select_orb_country_route(
            router_with(None), symbol, NOW, schwab_account=SCHWAB, webull_account=WEBULL
        )


def test_naive_clock_does_not_gain_a_route():
    with pytest.raises(ValueError, match="orb_country_clock_invalid"):
        select_orb_country_route(
            router_with(None), "ABC", NOW.replace(tzinfo=None),
            schwab_account=SCHWAB, webull_account=WEBULL
        )


def test_closed_router_window_cannot_select_a_broker():
    with pytest.raises(ValueError, match="orb_country_entry_window_closed"):
        select_orb_country_route(
            router_with(record_with(True)), "ABC", NOW.replace(hour=20),
            schwab_account=SCHWAB, webull_account=WEBULL
        )


def test_paper_binding_cannot_become_a_live_orb_route():
    with pytest.raises(ValueError, match="orb_country_account_missing"):
        select_orb_country_route(
            router_with(record_with(False)), "ABC", NOW,
            schwab_account=SCHWAB, webull_account="paper:orb"
        )


def test_same_account_cannot_represent_both_venues():
    with pytest.raises(ValueError, match="orb_country_account_collision"):
        select_orb_country_route(
            router_with(record_with(True)), "ABC", NOW,
            schwab_account=SCHWAB, webull_account=SCHWAB
        )


def test_unknown_broker_is_not_silently_reinterpreted(monkeypatch):
    router = router_with(None)
    monkeypatch.setattr(router, "route_for", lambda *_: CountryRoute(
        "other", "unknown", "UNKNOWN", None, None, None
    ))
    with pytest.raises(ValueError, match="orb_country_broker_invalid"):
        select_orb_country_route(
            router, "ABC", NOW, schwab_account=SCHWAB, webull_account=WEBULL
        )
