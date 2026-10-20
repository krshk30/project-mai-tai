"""Execution-time controls; session policy is independent of unknown fill time.

candidate-8886-full-unit.log lines 2920-2955 confirms the positive target
fixture supplied no session, so fail-closed target deferral was correct. Supply
NORMAL only for that positive case; the explicit UNKNOWN negative stays closed.
The original protection/send-clock/first-time assertions are retained.
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.momentum_live.runtime import OwnedNativeBackend
from tests.unit.test_momlive1_actor import setup
from tests.unit.test_webull_list_primary import _FakeClient, _order, listed, primary, row
from tests.unit.test_webull_adapter import fake_sdk as _sdk_fixture

fake_sdk = _sdk_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["last_filled_time", "lastFilledTime"])
async def test_actual_adapter_timestamp_alias_is_last_not_proven_first(fake_sdk, alias):
    raw = row(status="FILLED", qty="1", price="1.11")
    stamp = raw["items"][0].pop("last_filled_time")
    raw["items"][0][alias] = stamp
    leaf = primary(_FakeClient({"today": listed(raw), "detail": raw}))
    router = RoutingBrokerAdapter(default_provider="webull", provider_by_account={"live:orb": "webull"},
        factories_by_provider={"webull": lambda: leaf})
    request = replace(_order(client_order_id="owned", symbol="AIFA"), strategy_code="momentum_warn_live")
    runtime = SimpleNamespace(service=SimpleNamespace(broker_adapter=router),
        provider=lambda account: "webull", now_ns=lambda: int(datetime(2026, 10, 14, 18, tzinfo=UTC).timestamp()*1e9))
    report = await OwnedNativeBackend().fetch_entry_update(request, runtime)
    assert report.origin == "broker" and report.filled_quantity == 1 and report.fill_price == D("1.11")
    assert report.metadata["momlive_first_execution_time_status"] == "UNKNOWN"
    assert "momlive_execution_time_source" not in report.metadata
    if alias == "last_filled_time":
        assert "momlive_last_execution_ns" in report.metadata
    else:
        # Existing adapter parses camel-case reported_at, but does not forward
        # that alias into canonical metadata. Never trust receive-time fallback.
        assert "momlive_last_execution_ns" not in report.metadata


@pytest.mark.asyncio
async def test_unknown_first_time_never_strands_verified_owned_quantity_unprotected():
    actor, ports, episode, position = setup(filled=3, terminal=True, now=200_000_000_000)
    # Known policy for the positive case; UNKNOWN remains a separate refusal.
    position.protection_session = position.market_session = "NORMAL"
    position.first_fill_ns = None
    ports.observe.return_value = replace(ports.observe.return_value, first_fill_ns=None)
    ports.quote = lambda symbol: {"bid": D(3), "observed_ns": 200_000_000_000}
    await actor.restore_before_serving()
    assert actor.restored and episode.filled == 3 and episode.first_fill_ns is None
    ports.protect_owned.assert_awaited_once_with(position)
    ports.exit_owned.assert_not_awaited()  # Still before the approved SEND+300s deadline.
    assert position.exit_clock_source == "send" and position.first_fill_ns is None
    ports.quote = lambda symbol: {"bid": D(4), "observed_ns": 200_000_000_000}
    await actor._service_owned(episode.episode_id)
    ports.exit_owned.assert_awaited_once_with(position, "target_5pct")


@pytest.mark.asyncio
async def test_unknown_session_and_first_time_still_protects_but_never_authorizes_target():
    actor, ports, episode, position = setup(filled=3, terminal=True, now=200_000_000_000)
    position.first_fill_ns = None
    assert position.protection_session is None and position.market_session is None
    ports.observe.return_value = replace(ports.observe.return_value, first_fill_ns=None)
    ports.quote = lambda symbol: {"bid": D(4), "observed_ns": 200_000_000_000}
    await actor.restore_before_serving()
    assert actor.restored and episode.filled == 3 and episode.first_fill_ns is None
    ports.protect_owned.assert_awaited_once_with(position)
    ports.exit_owned.assert_not_awaited()
    assert position.exit_clock_source == "send" and position.first_fill_ns is None


@pytest.mark.asyncio
async def test_unknown_first_after_restore_fires_send_deadline_without_minting_first_timestamp():
    actor, ports, episode, position = setup(filled=3, terminal=True, now=301_000_000_000)
    position.first_fill_ns = None
    ports.observe.return_value = replace(ports.observe.return_value, first_fill_ns=None)
    ports.quote = lambda symbol: {"bid": D(3), "observed_ns": 301_000_000_000}
    await actor.restore_before_serving()
    ports.protect_owned.assert_awaited_once_with(position)
    ports.exit_owned.assert_awaited_once_with(position, "time_300s")
    assert position.exit_clock_source == "send" and position.entry_send_ns == episode.first_submit_ns
    assert position.first_fill_ns is None and episode.first_fill_ns is None
