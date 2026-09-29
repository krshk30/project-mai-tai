from __future__ import annotations

from decimal import Decimal

import pytest

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata


def test_bkyi_two_share_native_bracket_is_trigger_anchored_and_capped() -> None:
    metadata = build_orb_schwab_bracket_metadata(Decimal("3.42"))
    request = OrderRequest(
        client_order_id="orb-BKYI-open-local-test",
        broker_account_name="live:schwab_1m_v2",
        strategy_code="orb_schwab",
        symbol="BKYI",
        side="buy",
        intent_type="open",
        quantity=Decimal("2"),
        reason="ORB_FIXED_RESTING",
        order_type="stop_limit",
        metadata=metadata,
    )
    adapter = SchwabBrokerAdapter(
        Settings(oms_adapter="schwab", schwab_access_token="fake", schwab_account_hash="fake")
    )
    payload = adapter._build_bracket_payload(request)

    assert payload["session"] == "NORMAL"
    assert payload["orderType"] == "STOP_LIMIT"
    assert payload["orderStrategyType"] == "TRIGGER"
    assert payload["stopPrice"] == 3.42
    assert payload["price"] == 3.43
    assert payload["orderLegCollection"][0]["quantity"] == 2.0
    target, protect = payload["childOrderStrategies"][0]["childOrderStrategies"]
    assert payload["childOrderStrategies"][0]["orderStrategyType"] == "OCO"
    assert target["price"] == 3.59
    assert protect["stopPrice"] == 3.15
    assert target["orderLegCollection"][0]["quantity"] == 2.0
    assert protect["orderLegCollection"][0]["quantity"] == 2.0
    assert target["orderLegCollection"][0]["instruction"] == "SELL"
    assert protect["orderLegCollection"][0]["instruction"] == "SELL"


@pytest.mark.parametrize("level", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_bracket_plan_rejects_invalid_breakout_levels(level: Decimal) -> None:
    with pytest.raises(ValueError):
        build_orb_schwab_bracket_metadata(level)


def test_low_price_bracket_uses_four_decimal_ticks() -> None:
    metadata = build_orb_schwab_bracket_metadata(Decimal("0.50123"))
    assert metadata["stop_price"] == "0.5013"
    assert metadata["limit_price"] == "0.5037"
    assert metadata["bracket_target_price"] == "0.5264"
    assert metadata["bracket_stop_price"] == "0.4612"


@pytest.mark.asyncio
async def test_schwab_adapter_never_submits_orb_as_a_naked_single_leg() -> None:
    request = OrderRequest(
        client_order_id="orb-BKYI-open-local-test",
        broker_account_name="live:schwab_1m_v2",
        strategy_code="orb_schwab",
        symbol="BKYI",
        side="buy",
        intent_type="open",
        quantity=Decimal("2"),
        reason="ORB_FIXED_RESTING",
        order_type="stop_limit",
        metadata=build_orb_schwab_bracket_metadata(Decimal("3.42")),
    )
    adapter = SchwabBrokerAdapter(
        Settings(oms_adapter="schwab", schwab_access_token="fake", schwab_account_hash="fake")
    )
    reports = await adapter.submit_order(request)
    assert reports[0].event_type == "rejected"
    assert reports[0].origin == "client"
    assert "native STOP_LIMIT bracket" in reports[0].reason

    enabled_adapter = SchwabBrokerAdapter(
        Settings(
            oms_adapter="schwab",
            orb_live_schwab_orders_enabled=True,
            schwab_access_token="fake",
            schwab_account_hash="fake",
        )
    )
    request.metadata["exit_only_oco"] = "true"
    reports = await enabled_adapter.submit_order(request)
    assert reports[0].event_type == "rejected"
    assert reports[0].origin == "client"
