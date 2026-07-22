"""Schwab native OCO bracket payload (TRIGGER -> OCO exit pair).

The shape asserted here is BROKER-VALIDATED, not invented: POST /previewOrder on the live
account returned HTTP 200 `status: "ACCEPTED"` with zero rejects on 2026-07-21, and Schwab
echoed it back as `advancedOrderType: "OTOCO"`. Harness: scripts/schwab_oco_preview.py.

These tests pin VALUES (prices, instructions, strategy types), not just "it built something" —
a bracket whose legs are structurally right but priced wrong is a real-money loss, and a green
suite that never pinned the numbers would not catch it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.settings import Settings


def _adapter(*, bracket_enabled: bool) -> SchwabBrokerAdapter:
    return SchwabBrokerAdapter(
        Settings(
            oms_adapter="schwab",
            schwab_access_token="token-123",
            schwab_account_hash="hash-123",
            schwab_native_bracket_enabled=bracket_enabled,
        )
    )


def _bracket_request(**overrides: object) -> OrderRequest:
    metadata = {
        "bracket": "true",
        "stop_price": "10.00",           # entry buy-stop (the break level)
        "bracket_target_price": "10.20",  # +2%
        "bracket_stop_price": "9.50",     # -5%
    }
    metadata.update(overrides.pop("metadata", {}))  # type: ignore[arg-type]
    return OrderRequest(
        client_order_id="schwab_1m_v2-KIDZ-open-abc123",
        broker_account_name="paper:schwab_1m",
        strategy_code="schwab_1m_v2",
        symbol="KIDZ",
        side="buy",
        intent_type="open",
        quantity=Decimal("1"),
        reason="ENTRY_CW",
        order_type="stop",
        metadata=metadata,
        **overrides,  # type: ignore[arg-type]
    )


def test_bracket_payload_matches_the_broker_accepted_shape() -> None:
    payload = _adapter(bracket_enabled=True)._build_bracket_payload(_bracket_request())

    # Parent: the entry, as a TRIGGER so the children arm only once it fills.
    assert payload["orderStrategyType"] == "TRIGGER"
    assert payload["orderType"] == "STOP"
    assert payload["stopPrice"] == 10.00
    assert payload["session"] == "NORMAL"   # RTH-first by design; EH is a separate axis
    assert payload["duration"] == "DAY"
    assert payload["orderLegCollection"][0]["instruction"] == "BUY"
    assert payload["orderLegCollection"][0]["quantity"] == 1.0

    # Child: exactly ONE OCO pair — this is the E5 dissolve. Two children, not two orders.
    children = payload["childOrderStrategies"]
    assert len(children) == 1
    oco = children[0]
    assert oco["orderStrategyType"] == "OCO"

    target, protective = oco["childOrderStrategies"]
    assert target["orderType"] == "LIMIT"
    assert target["price"] == 10.20
    assert target["orderLegCollection"][0]["instruction"] == "SELL"
    assert protective["orderType"] == "STOP"
    assert protective["stopPrice"] == 9.50
    assert protective["orderLegCollection"][0]["instruction"] == "SELL"

    # Both legs sell the FULL position: a partial protective leg leaves a naked remainder.
    assert target["orderLegCollection"][0]["quantity"] == 1.0
    assert protective["orderLegCollection"][0]["quantity"] == 1.0


def test_protective_stop_is_below_entry_and_target_above() -> None:
    """Direction sanity — an inverted bracket would arm a stop that fires instantly."""
    payload = _adapter(bracket_enabled=True)._build_bracket_payload(_bracket_request())
    entry = payload["stopPrice"]
    target, protective = payload["childOrderStrategies"][0]["childOrderStrategies"]
    assert protective["stopPrice"] < entry < target["price"]


def test_flag_off_builds_the_unchanged_single_leg_payload() -> None:
    """Byte-identical-off: bracket metadata present, flag off -> still SINGLE.

    A box where the flag was never flipped must not silently start sending combos.
    """
    adapter = _adapter(bracket_enabled=False)
    request = _bracket_request()
    assert adapter._is_bracket_request(request) is False

    payload = adapter._build_order_payload(request)
    assert payload["orderStrategyType"] == "SINGLE"
    assert "childOrderStrategies" not in payload


def test_flag_on_without_bracket_metadata_stays_single_leg() -> None:
    """Both conditions required — the flag alone must not convert ordinary intents."""
    adapter = _adapter(bracket_enabled=True)
    plain = _bracket_request(metadata={"bracket": "false"})
    assert adapter._is_bracket_request(plain) is False


@pytest.mark.parametrize(
    "missing_key",
    ["stop_price", "bracket_target_price", "bracket_stop_price"],
)
def test_incomplete_bracket_raises_rather_than_emitting_a_naked_entry(missing_key: str) -> None:
    """A half-built bracket is the exact naked-position shape this structure exists to remove,
    so a missing price must fail loudly at build time — never place the entry alone."""
    request = _bracket_request(metadata={missing_key: ""})
    with pytest.raises(RuntimeError) as excinfo:
        _adapter(bracket_enabled=True)._build_bracket_payload(request)
    assert missing_key in str(excinfo.value)


def test_limit_entry_parent_uses_price_not_stopprice() -> None:
    """A marketable LIMIT parent is a valid TRIGGER entry; the OCO exit pair is unchanged.
    Pinned because STEP-1 needs to force a fill on demand without altering what it tests."""
    req = _bracket_request(metadata={"bracket_entry_type": "LIMIT", "limit_price": "10.05"})
    payload = _adapter(bracket_enabled=True)._build_bracket_payload(req)
    assert payload["orderType"] == "LIMIT"
    assert payload["price"] == 10.05
    assert "stopPrice" not in payload
    assert payload["orderStrategyType"] == "TRIGGER"
    # exit pair identical to the STOP-parent case
    oco = payload["childOrderStrategies"][0]
    target, protective = oco["childOrderStrategies"]
    assert target["price"] == 10.20 and protective["stopPrice"] == 9.50


def test_limit_entry_missing_limit_price_raises() -> None:
    req = _bracket_request(metadata={"bracket_entry_type": "LIMIT", "limit_price": ""})
    with pytest.raises(RuntimeError) as e:
        _adapter(bracket_enabled=True)._build_bracket_payload(req)
    assert "limit_price" in str(e.value)


@pytest.mark.asyncio
async def test_fetch_armed_native_oco_needs_two_working_sell_legs(monkeypatch) -> None:
    """Armed = 2 WORKING sell legs at the broker. AWAITING_PARENT_ORDER (entry unfilled, nothing
    held) does NOT count -- that would stand the ladder down before a position even exists."""
    adapter = SchwabBrokerAdapter(
        Settings(oms_adapter="schwab", schwab_access_token="t", schwab_account_hash="h",
                 schwab_native_bracket_enabled=True)
    )

    def orders(sym, target_status, stop_status):
        return [{
            "orderStrategyType": "TRIGGER",
            "status": "FILLED",
            "orderLegCollection": [{"instruction": "BUY",
                                    "instrument": {"symbol": sym, "assetType": "EQUITY"}}],
            "childOrderStrategies": [{
                "orderStrategyType": "OCO",
                "childOrderStrategies": [
                    {"status": target_status,
                     "orderLegCollection": [{"instruction": "SELL",
                                             "instrument": {"symbol": sym}}]},
                    {"status": stop_status,
                     "orderLegCollection": [{"instruction": "SELL",
                                             "instrument": {"symbol": sym}}]},
                ],
            }],
        }]

    async def fake(method, path, body=None):
        return 200, {}, fake.body
    monkeypatch.setattr(adapter, "_authorized_request_json", fake)

    # both legs WORKING -> armed
    fake.body = orders("KIDZ", "WORKING", "WORKING")
    assert await adapter.fetch_armed_native_oco_symbols("paper:schwab_1m", ["KIDZ"]) == {"KIDZ"}

    # legs still AWAITING_PARENT_ORDER (entry not filled) -> NOT armed
    fake.body = orders("KIDZ", "AWAITING_PARENT_ORDER", "AWAITING_PARENT_ORDER")
    assert await adapter.fetch_armed_native_oco_symbols("paper:schwab_1m", ["KIDZ"]) == set()

    # one leg filled (OCO resolved), one working -> only 1 working sell -> NOT armed
    fake.body = orders("KIDZ", "FILLED", "WORKING")
    assert await adapter.fetch_armed_native_oco_symbols("paper:schwab_1m", ["KIDZ"]) == set()


@pytest.mark.asyncio
async def test_fetch_armed_native_oco_raises_on_broker_error(monkeypatch) -> None:
    """Must raise so the OMS caller fails OPEN (loud), never silently returns 'nothing armed'."""
    adapter = SchwabBrokerAdapter(
        Settings(oms_adapter="schwab", schwab_access_token="t", schwab_account_hash="h")
    )

    async def boom(method, path, body=None):
        return 500, {}, {"error": "server"}
    monkeypatch.setattr(adapter, "_authorized_request_json", boom)
    with pytest.raises(RuntimeError):
        await adapter.fetch_armed_native_oco_symbols("paper:schwab_1m", ["KIDZ"])


def test_market_parent_has_no_price_or_stopprice() -> None:
    """The live v2 CW entry is a MARKET order. A MARKET OTOCO parent needs neither price nor
    stopPrice; the exit pair is unchanged. (⚠ preview-validate MARKET in STEP-1 before live.)"""
    req = _bracket_request(metadata={"bracket_entry_type": "MARKET"})
    payload = _adapter(bracket_enabled=True)._build_bracket_payload(req)
    assert payload["orderType"] == "MARKET"
    assert "price" not in payload and "stopPrice" not in payload
    assert payload["orderStrategyType"] == "TRIGGER"
    oco = payload["childOrderStrategies"][0]
    target, protective = oco["childOrderStrategies"]
    assert target["price"] == 10.20 and protective["stopPrice"] == 9.50


def test_market_parent_still_requires_both_exit_prices() -> None:
    """MARKET drops the entry-price requirement but NOT the exits — a naked entry is forbidden."""
    req = _bracket_request(metadata={"bracket_entry_type": "MARKET", "bracket_stop_price": ""})
    with pytest.raises(RuntimeError) as e:
        _adapter(bracket_enabled=True)._build_bracket_payload(req)
    assert "bracket_stop_price" in str(e.value)


def test_bracket_exit_legs_round_to_schwab_tick_rule() -> None:
    """Exit legs round to Schwab's decimal rule (defence in depth alongside the emit)."""
    req = _bracket_request(metadata={"bracket_entry_type": "MARKET",
                                     "bracket_target_price": "11.3322",
                                     "bracket_stop_price": "10.5545"})
    payload = _adapter(bracket_enabled=True)._build_bracket_payload(req)
    target, protective = payload["childOrderStrategies"][0]["childOrderStrategies"]
    assert target["price"] == 11.33
    assert protective["stopPrice"] == 10.55
