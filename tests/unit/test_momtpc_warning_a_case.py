"""W3 composes warning-A guards with approved broader C raw-case ingestion."""

from types import SimpleNamespace

import pytest

from project_mai_tai.market_data.massive_provider import MassiveTradeStream
from tests.unit.test_momwarn1_gateway import aggregate, stream


@pytest.mark.parametrize('preferred,common', [('TpC', 'TPC'), ('BCpC', 'BCPC')])
def test_warning_A_filters_raw_case_before_parser_without_revert(preferred, common, monkeypatch):
    provider = stream()
    warnings, reverts = [], []
    provider.configure_momentum_warning(warnings.append, reverts.append)
    provider._warning_wildcard_used = provider._warning_aggregates_enabled = True
    monkeypatch.setattr(provider, '_warning_window_is_open', lambda: True)
    provider._handle_messages([aggregate(sym=preferred), aggregate(sym=common)])
    assert [row.symbol for row in warnings] == [common]
    assert not reverts and provider._warning_aggregates_enabled and not provider._warning_failed


@pytest.mark.parametrize('preferred', ['TpC', 'BCpC'])
def test_bad_preferred_A_is_discarded_not_a_false_invalid_aggregate_trip(preferred, monkeypatch):
    provider = stream()
    warnings, reverts = [], []
    provider.configure_momentum_warning(warnings.append, reverts.append)
    provider._warning_wildcard_used = provider._warning_aggregates_enabled = True
    monkeypatch.setattr(provider, '_warning_window_is_open', lambda: True)
    provider._handle_messages([aggregate(sym=preferred, o=float('nan'))])
    assert not warnings and not reverts and provider._warning_aggregates_enabled


@pytest.mark.parametrize('preferred,common', [('TpC', 'TPC'), ('BCpC', 'BCPC')])
def test_direct_warning_A_parser_never_accepts_an_uppercased_preferred_alias(preferred, common):
    assert MassiveTradeStream._normalize_warning_aggregate(aggregate(sym=preferred), common) is None


def test_direct_warning_A_parser_requires_exact_raw_binding():
    assert MassiveTradeStream._normalize_warning_aggregate(aggregate(sym='TPC'), 'BCPC') is None
    assert MassiveTradeStream._normalize_warning_aggregate(aggregate(sym='TPC'), 'TPC') is not None


def test_common_A_shape_failure_still_trips_existing_gate(monkeypatch):
    provider = stream()
    reverts = []
    provider.configure_momentum_warning(lambda _: None, reverts.append)
    provider._warning_wildcard_used = provider._warning_aggregates_enabled = True
    monkeypatch.setattr(provider, '_warning_window_is_open', lambda: True)
    provider._handle_messages([aggregate(sym='TPC', o=float('nan'))])
    assert reverts == ['invalid_aggregate'] and provider._warning_failed


@pytest.mark.parametrize('warning_used', [False, True])
def test_approved_C_watched_TQ_drops_mixed_and_preserves_real_common(warning_used):
    provider = stream()
    provider._warning_wildcard_used = warning_used
    provider._warning_aggregates_enabled = warning_used
    provider._subscriptions = {'TPC'}
    trades, quotes = [], []
    provider._on_trade, provider._on_quote = trades.append, quotes.append
    provider._handle_messages([SimpleNamespace(ev='T', symbol='TpC', price=15.73, size=1),
                              SimpleNamespace(ev='Q', symbol='TpC', bid_price=15.72, ask_price=15.74)])
    assert not trades and not quotes
    provider._handle_messages([SimpleNamespace(ev='T', symbol='TPC', price=82.77, size=1),
                              SimpleNamespace(ev='Q', symbol='TPC', bid_price=82.76, ask_price=82.78)])
    assert len(trades) == len(quotes) == 1
    assert trades[0].symbol == quotes[0].symbol == 'TPC'
    assert trades[0].price == 82.77 and quotes[0].ask_price == 82.78


def test_approved_C_explicit_A_drops_mixed_and_preserves_common():
    provider = stream()
    provider._subscriptions = {'TPC'}
    bars = []
    provider._on_agg = bars.append
    provider._handle_messages([aggregate(sym='TpC')])
    assert not bars
    provider._handle_messages([aggregate(sym='TPC')])
    assert len(bars) == 1 and bars[0].symbol == 'TPC'
