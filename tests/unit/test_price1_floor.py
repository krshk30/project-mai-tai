"""PRICE1 pure floor controls; author UNRUN, not actual historical replay."""

from decimal import Decimal

import pytest

from project_mai_tai.buy_price_floor import buy_price_allowed


@pytest.mark.parametrize("symbol,price", [("OLOX", ".9975"), ("FUFU", ".79"),
    ("EYPT", ".7597"), ("EYPT", ".8355"), ("CRMT", ".8937")])
def test_named_subdollar_buy_prices_refuse_and_log(symbol, price, caplog):
    with caplog.at_level("INFO", logger="price1"):
        assert not buy_price_allowed(Decimal(price), bot="synthetic", symbol=symbol)
    assert "[PRICE1-REFUSED]" in caplog.text and symbol in caplog.text and price in caplog.text


@pytest.mark.parametrize("price", ["1", "1.20", "5.25"])
def test_dollar_boundary_and_current_prices_admit_without_priorclose(price):
    assert buy_price_allowed(Decimal(price), bot="synthetic", symbol="GIXI")


@pytest.mark.parametrize("price", [None, "NaN", "Infinity", "bad"])
def test_unknown_nonfinite_buy_does_not_become_permission(price):
    assert not buy_price_allowed(price, bot="synthetic", symbol="TEST")


def test_olox_exit_097_always_bypasses_buy_floor():
    assert buy_price_allowed(Decimal(".97"), bot="schwab_1m_v2", symbol="OLOX", side="sell")
