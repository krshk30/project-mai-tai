"""02:0x: production is deployment environment, never trading execution mode."""
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from project_mai_tai.db.models import (
    BotControlState, BrokerAccount, BrokerOrder, OmsManagedPosition, Strategy,
)
from project_mai_tai.oms.bot_control import ControlConflict
from project_mai_tai.oms.bot_control_runtime import BotControlRuntime, ControlledBrokerAdapter
from project_mai_tai.oms.bot_control_scope import (
    CONTROLLED_STRATEGY_CODES, controlled_live_scope, logical_account_mode,
)
from tests.unit.test_killsw1_boot_capture import inventory as inventory
from tests.unit.test_killsw1_commands import (
    RecordingAdapter, control as control, entry_request, order, request,
    test_runtime_replacement_uses_distinct_durable_intent_and_never_replays as replacement_control,
)


@pytest.mark.parametrize("deployment", ["production", "staging", "live", "paper", "shadow"])
def test_live_alias_and_strategy_not_deployment_environment_define_scope(control, deployment):
    store, sessions, strategy, account = control
    with sessions.begin() as unit:
        unit.get(BrokerAccount, account).environment = deployment
    assert store.seed(strategy).admission_open
    assert request(store, strategy, account).phase == "PENDING_CENSUS"


@pytest.mark.parametrize("code", ["macd_30s", "schwab_1m", "webull_30s", "operator_manual"])
def test_noncontrolled_live_strategy_never_seeded_or_exposed(control, code):
    store, sessions, _, _ = control
    identity = uuid4()
    with sessions.begin() as unit:
        unit.add(Strategy(id=identity, code=code, name=code, execution_mode="live"))
    with pytest.raises(ControlConflict, match="not_a_controlled_live_strategy"):
        store.seed(identity)
    with sessions() as unit:
        assert unit.get(BotControlState, (identity, "live")) is None
    # A stale/accidentally provisioned old state cannot restore control authority.
    with sessions.begin() as unit:
        unit.add(BotControlState(strategy_id=identity, execution_mode="live",
            generation=0, revision=0, desired_state="RUN"))
    for read in (store.hydrate, store.describe):
        with pytest.raises(ControlConflict, match="not_a_controlled_live_strategy"):
            read(identity)


@pytest.mark.parametrize("name", ["paper:orb", "shadow:orb", "unknown:orb", "live:", " live:orb"])
def test_bad_live_scope_is_unknown_even_with_production_and_supported_provider(control, name):
    store, sessions, strategy, account = control
    with sessions.begin() as unit:
        row = unit.get(BrokerAccount, account)
        row.name, row.environment = name, "production"
    store.seed(strategy)
    with pytest.raises(ControlConflict, match="account_scope_not_live"):
        request(store, strategy, account)
    identity = order(sessions, strategy, account)
    with pytest.raises(ControlConflict, match="entry_account_not_live"):
        store.authorize_entry(order_id=identity, strategy_id=strategy, expected_generation=0)
    assert store.hydrate(strategy).generation == 0


@pytest.mark.parametrize("field,value", [("provider", "unknown"), ("is_active", False)])
def test_wire_keeps_provider_and_active_guard(control, field, value):
    store, sessions, strategy, account = control
    with sessions.begin() as unit:
        row = unit.get(BrokerAccount, account)
        row.environment = "production"
        setattr(row, field, value)
    store.seed(strategy)
    identity = order(sessions, strategy, account)
    with pytest.raises(ControlConflict, match="entry_account_not_live"):
        store.authorize_entry(order_id=identity, strategy_id=strategy, expected_generation=0)


def test_actual_strategy_mode_change_invalidates_seeded_live_authority(control):
    store, sessions, strategy, account = control
    store.seed(strategy)
    identity = order(sessions, strategy, account)
    with sessions.begin() as unit:
        unit.get(Strategy, strategy).execution_mode = "paper"
    for action in (lambda: store.hydrate(strategy),
                   lambda: request(store, strategy, account),
                   lambda: store.authorize_entry(order_id=identity, strategy_id=strategy,
                                                 expected_generation=0)):
        with pytest.raises(ControlConflict, match="not_a_live_strategy"):
            action()


@pytest.mark.asyncio
async def test_production_live_wire_permit_and_controlled_unknown_refusal(control):
    store, sessions, strategy, account = control
    with sessions.begin() as unit:
        unit.get(BrokerAccount, account).environment = "production"
    store.seed(strategy)
    identity = order(sessions, strategy, account)
    runtime = BotControlRuntime(sessions)
    await runtime.refresh()
    raw = RecordingAdapter()
    adapter = ControlledBrokerAdapter(raw, runtime)
    value = entry_request(identity)
    assert await adapter.submit_order(value) == []
    assert raw.requests == [value]
    assert "bot_control_permit" in value.metadata
    with pytest.raises(ControlConflict, match="scope_not_running"):
        await adapter.submit_order(entry_request(uuid4(), strategy_code="orb_schwab"))
    with pytest.raises(ControlConflict):
        await adapter.submit_order(entry_request(uuid4(), broker_account_name="unknown:missing"))
    assert raw.requests == [value]


@pytest.mark.asyncio
async def test_production_replacement_retains_original_durable_identity_controls(control):
    _, sessions, _, account = control
    with sessions.begin() as unit:
        unit.get(BrokerAccount, account).environment = "production"
    # Reuse the unchanged original assertions: one permit, no replay, original row retained.
    await replacement_control(control)


@pytest.mark.asyncio
async def test_runtime_hydrates_only_three_and_known_paper_production_bypasses(control):
    store, sessions, strategy, _ = control
    store.seed(strategy)
    with sessions.begin() as unit:
        unit.add(BrokerAccount(name="paper:actual", provider="none", environment="production"))
        unit.add(Strategy(code="macd_30s", name="Legacy", execution_mode="live"))
    runtime = BotControlRuntime(sessions)
    await runtime.refresh()
    assert set(runtime.states) == {"momentum_warn_live"}
    assert runtime.account_modes["paper:actual"] == "paper"
    assert not runtime.controls("momentum_warn_live", "paper:actual")
    assert runtime.controls("momentum_warn_live", "unseen:unknown")
    raw = RecordingAdapter()
    value = entry_request(uuid4(), broker_account_name="paper:actual")
    assert await ControlledBrokerAdapter(raw, runtime).submit_order(value) == []
    assert raw.requests == [value]
    assert value.metadata.get("bot_control_permit") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["macd_30s", "schwab_1m", "webull_30s", "other"])
async def test_noncontrolled_wire_and_replace_delegate_without_control_side_effects(code):
    class ForbiddenControl:
        async def authorize(self, *args):
            pytest.fail("noncontrolled request reached C6 authorization")

    raw = RecordingAdapter()
    adapter = ControlledBrokerAdapter(raw, ForbiddenControl())
    value = entry_request(uuid4(), strategy_code=code)
    assert await adapter.submit_order(value) == []
    assert await adapter.replace_bracket_order(value, "old-root") == "confirmed"
    assert raw.requests == [value, (value, "old-root")]
    assert "bot_control_permit" not in value.metadata


@pytest.mark.asyncio
async def test_noncontrolled_stamp_and_predicates_do_not_require_seed(control):
    _, sessions, _, _ = control
    runtime = BotControlRuntime(sessions)
    data = {}
    runtime.stamp(data, "macd_30s", datetime.now(UTC))
    assert data == {}
    assert runtime.admission_open("macd_30s")
    assert not runtime.stop_requested("macd_30s")
    assert not runtime.controls("macd_30s", "unknown:missing")
    with pytest.raises(ControlConflict, match="scope_not_running"):
        runtime.stamp({}, "momentum_warn_live", datetime.now(UTC))


@pytest.mark.asyncio
@pytest.mark.parametrize("deployment", ["production", "paper"])
async def test_retained_live_alias_boot_row_stays_live_regardless_deployment_label(inventory, deployment):
    runtime, sessions, ids = inventory
    with sessions.begin() as unit:
        unit.get(BrokerAccount, ids["account"]).environment = deployment
    await runtime.refresh()
    assert runtime.boot_managed_ids == frozenset({str(ids["managed"])})
    assert runtime.boot_orb_entry_ids == frozenset(ids["entries"])
    assert runtime.admission_open("schwab_1m_v2")


@pytest.mark.asyncio
async def test_production_paper_invalid_managed_quantity_never_poisons_live_boot(inventory):
    runtime, sessions, ids = inventory
    with sessions.begin() as unit:
        unit.add(BrokerAccount(name="paper:production", provider="schwab", environment="production"))
        unit.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name="paper:production",
            symbol="PAPER", entry_price=Decimal(6), original_quantity=10, current_quantity=-1, status="open"))
    await runtime.refresh()
    assert runtime.boot_managed_ids == frozenset({str(ids["managed"])})
    assert runtime.boot_orb_entry_ids == frozenset(ids["entries"])
    assert not runtime.is_live_account("paper:production")


@pytest.mark.asyncio
async def test_inactive_owned_account_stays_in_boot_inventory_and_cleanup_binding(inventory):
    runtime, sessions, ids = inventory
    with sessions.begin() as unit:
        unit.get(BrokerAccount, ids["account"]).is_active = False
        unit.get(BrokerAccount, ids["account"]).environment = "production"
    await runtime.refresh()
    assert runtime.boot_managed_ids == frozenset({str(ids["managed"])})
    assert runtime.boot_orb_entry_ids == frozenset(ids["entries"])
    with sessions() as unit:
        parent = unit.get(BrokerOrder, UUID(next(iter(ids["entries"]))))
        account = unit.get(BrokerAccount, parent.broker_account_id)
        query = entry_request(parent.id, strategy_code="orb_schwab",
            broker_account_name=account.name, client_order_id=parent.client_order_id,
            symbol=parent.symbol)
    assert await runtime._cleanup_order(query) == parent.id


def test_helper_has_exact_three_no_environment_or_active_shortcuts():
    assert CONTROLLED_STRATEGY_CODES == {"schwab_1m_v2", "orb_schwab", "momentum_warn_live"}
    assert controlled_live_scope("schwab_1m_v2", "live", "live:orb")
    assert not controlled_live_scope("macd_30s", "live", "live:orb")
    assert not controlled_live_scope("schwab_1m_v2", "paper", "live:orb")
    assert logical_account_mode("paper:actual") == "paper"
    assert logical_account_mode(None) == "UNKNOWN"
