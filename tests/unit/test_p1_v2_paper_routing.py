"""P1 Phase 1 — schwab_1m_v2 deliberate/structural paper routing.

Two structural layers, both v2-scoped, prove v2 cannot reach the real Schwab account:
  (1) default provider flipped "schwab" -> "simulated" (orders route to the sim sink), and
  (2) configured_schwab_accounts refuses to bind a real hash to paper:schwab_1m_v2.
Plus: the simulated sink actually exercises v2's order path (order -> fill -> position).

Scope note: SimulatedBrokerAdapter uses an IDEALIZED fill model (instant full fill at
reference_price, no slippage/partials/rejects). These tests prove the pipe is wired and
v2 is structurally fenced off the real account — NOT that execution is realistic.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.schwab import configured_schwab_accounts
from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.settings import Settings


def test_v2_routes_to_simulated_provider_by_default() -> None:
    # The dangerous "schwab" default is replaced: v2 routes to the simulated provider,
    # so the OMS sends its orders to the simulated sink and never the real Schwab adapter.
    settings = Settings()
    account = settings.strategy_schwab_1m_v2_account_name
    assert settings.strategy_schwab_1m_v2_broker_provider == "simulated"
    assert settings.provider_for_strategy("schwab_1m_v2") == "simulated"
    assert settings.provider_for_account(account) == "simulated"


def test_configured_schwab_accounts_refuses_v2_but_keeps_retired_bots() -> None:
    # v2-scoped hash-guard: even with a real shared hash set AND an add() for v2 present,
    # paper:schwab_1m_v2 is refused -> it can never bind a real Schwab hash.
    settings = Settings(schwab_account_hash="REALHASH-2EE5A4")
    accounts = configured_schwab_accounts(settings)
    assert settings.strategy_schwab_1m_v2_account_name not in accounts
    # ...but the retired bots STAY registered — their position-sync triggers the shared
    # token refresh until the dedicated refresher (P0) lands. Broadening the guard to all
    # paper: accounts before P0 would remove this refresh trigger (P0 SPOF).
    assert settings.strategy_macd_30s_account_name in accounts
    assert settings.strategy_schwab_1m_account_name in accounts


def test_v2_inert_on_sim_even_if_a_real_hash_entry_would_exist() -> None:
    # Two layers make a would-be real-hash entry inert for v2:
    #   (1) the hash-guard prevents the entry being registered at all, and
    #   (2) provider="simulated" routes v2's orders to the sim adapter, so the Schwab
    #       account map is never consulted for v2 regardless of what it contains.
    settings = Settings(schwab_account_hash="REALHASH-2EE5A4")
    assert settings.strategy_schwab_1m_v2_account_name not in configured_schwab_accounts(settings)
    assert settings.provider_for_account(settings.strategy_schwab_1m_v2_account_name) == "simulated"


@pytest.mark.asyncio
async def test_simulated_sink_fills_v2_order_and_opens_position() -> None:
    # Phase 1 finally exercises v2's order path end-to-end on the safe sink:
    # order -> accepted + filled -> position opened.
    adapter = SimulatedBrokerAdapter()
    account = "paper:schwab_1m_v2"
    order = OrderRequest(
        client_order_id="schwab_1m_v2-TEST-open-1",
        broker_account_name=account,
        strategy_code="schwab_1m_v2",
        symbol="TEST",
        side="buy",
        intent_type="open",
        quantity=Decimal("100"),
        reason="schwab_1m_v2 VWAP Breakout",
        metadata={"reference_price": "5.00"},
    )

    reports = await adapter.submit_order(order)

    event_types = {r.event_type for r in reports}
    assert "accepted" in event_types
    assert "filled" in event_types
    filled = next(r for r in reports if r.event_type == "filled")
    assert filled.fill_price == Decimal("5.00")
    assert filled.filled_quantity == Decimal("100")

    positions = await adapter.list_account_positions(account)
    assert len(positions) == 1
    assert positions[0].symbol == "TEST"
    assert positions[0].quantity == Decimal("100")
    assert positions[0].average_price == Decimal("5.00")
