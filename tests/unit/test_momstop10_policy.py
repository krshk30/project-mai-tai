"""Stop-10 only: preserve MOMORDER1 target ownership, timer and cleanup gates."""

from decimal import Decimal as D, ROUND_FLOOR
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.momentum_live.exit_policy import STOP_LOSS_MULTIPLIER, STOP_LOSS_PERCENT, stop_loss_level
from project_mai_tai.momentum_live.lifecycle import OwnedPosition
from tests.unit.test_momorder1_protection import protection_case
from tests.unit.test_momstop10_boot import held_native, quote, exits


def owned(session_name):
    return OwnedPosition("owned-episode", "live:orb", "YMAT", "own-entry", "own-native-entry",
        D(50), D(2), 1_000_000_000, protection_session=session_name,
        market_session=session_name, native_confirmed=session_name == "NORMAL",
        reserved=D(50), broker_target_state="working")


@pytest.mark.parametrize("entry", ["3.11", "3.0012", ".9501", "2", "2.9974", "2.98"])
def test_stop_policy_is_exact_decimal_ten_percent_with_legacy_reason_name(entry):
    assert STOP_LOSS_PERCENT == 10 and STOP_LOSS_MULTIPLIER == D(".90")
    assert stop_loss_level(D(entry)) == D(entry) * D(".90")


@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize("session_name", ["AM", "NORMAL", "PM"])
def test_ten_percent_stop_and_exact_300s_boundary_preserve_native_ownership(provider, session_name):
    position = owned(session_name)
    position.account = "live:orb" if provider == "webull" else "live:schwab_1m_v2"
    assert position.exit_reason(D("1.8001"), quote_ns=2_000_000_000, now_ns=2_000_000_000) is None
    expected = None if session_name == "NORMAL" else "hard_stop_15pct"
    assert position.exit_reason(D("1.80"), quote_ns=2_000_000_000, now_ns=2_000_000_000) == expected
    assert position.exit_reason(D(2), quote_ns=300_999_999_999, now_ns=300_999_999_999) is None
    assert position.exit_reason(D(2), quote_ns=301_000_000_000, now_ns=301_000_000_000) == "time_300s"
    if session_name in {"AM", "NORMAL"}:
        # Original sole-target/native-pair policy is NOT superseded by stop-10.
        assert position.exit_reason(D("2.10"), quote_ns=2_000_000_000, now_ns=2_000_000_000) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize("session_name", ["AM", "NORMAL", "PM"])
async def test_native_stop_wire_geometry_is_ten_percent_only_where_supported(provider, session_name):
    position, _, _, ports, native = protection_case(provider, session_name, price=D("3.0012"))
    await native.ensure(position, broker=provider, session=session_name)
    request = ports.submit.await_args.args[0]
    assert request.metadata["limit_price"] == "3.16"
    if session_name != "NORMAL":
        assert "bracket_stop_price" not in request.metadata
        assert "exit_only_oco" not in request.metadata and "webull_exit_only_pair" not in request.metadata
        assert request.order_type == "limit" and position.reserved == 31
        return
    expected = (D("3.0012") * D(".90") / D(".01")).to_integral_value(rounding=ROUND_FLOOR) * D(".01")
    assert D(request.metadata["bracket_stop_price"]) == expected == D("2.70")
    if provider == "webull":
        adapter = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
        payload = adapter._build_exit_only_pair_payload(request)
        stops = [row for row in payload if row["order_type"] == "STOP_LOSS"]
        assert len(stops) == 1 and D(str(stops[0]["stop_price"])) == expected
    else:
        adapter = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
        payload = adapter._build_exit_only_oco_payload(request)
        stops = [row for row in payload["childOrderStrategies"] if row["orderType"] == "STOP"]
        assert len(stops) == 1 and D(str(stops[0]["stopPrice"])) == expected
    assert position.native_confirmed and position.reserved == 31


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize("session_name", ["AM", "PM"])
async def test_ten_percent_software_stop_releases_own_target_before_owned_sell(tmp_path, provider, session_name):
    h = await held_native(tmp_path, provider, session_name)
    try:
        quote(h, "1.80")
        position = (await h.runtime._observe(h.identity)).position
        reason = position.exit_reason(D("1.80"), quote_ns=h.runtime.now_ns(), now_ns=h.runtime.now_ns())
        assert reason == "hard_stop_15pct"
        await h.runtime._exit(position, reason)
        assert len(exits(h)) == 1 and exits(h)[0].quantity == 50
        assert exits(h)[0].metadata["exit_reason"] == "hard_stop_15pct"
        assert D(exits(h)[0].metadata["limit_price"]) == D("1.80")
        assert len(h.retirement_at_sell) == 1 and h.retirement_at_sell[0]
        assert all(status == "cancelled" and quantity == 0 for status, quantity in h.retirement_at_sell[0])
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize("session_name", ["AM", "NORMAL", "PM"])
@pytest.mark.parametrize("reason", ["time_300s", "KILLSW1_STOP"])
async def test_mandatory_time_and_stop_still_preempt_old_target_latch_without_receipt_waiver(
    tmp_path, monkeypatch, provider, session_name, reason,
):
    h = await held_native(tmp_path, provider, session_name)
    try:
        h.runtime.runtime[h.identity]["soft_exit_reason"] = "target_5pct"
        position = (await h.runtime._observe(h.identity)).position
        position.pending_exit_reason = "target_5pct"
        quote(h, "1.99")
        if reason == "KILLSW1_STOP":
            receipt = AsyncMock(return_value=False)
            monkeypatch.setattr(h.runtime, "_control_receipt_ready", receipt)
        await h.runtime._exit(position, reason)
        assert h.runtime.runtime[h.identity]["soft_exit_reason"] == reason
        if reason == "KILLSW1_STOP":
            receipt.assert_awaited()
            assert exits(h) == []
        else:
            assert len(exits(h)) == 1 and exits(h)[0].quantity == 50
            assert exits(h)[0].metadata["exit_reason"] == reason
            assert D(exits(h)[0].metadata["limit_price"]) == D("1.99")
            assert len(h.retirement_at_sell) == 1 and h.retirement_at_sell[0]
            assert all(status == "cancelled" and quantity == 0 for status, quantity in h.retirement_at_sell[0])
    finally:
        h.engine.dispose()
