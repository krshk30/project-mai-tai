"""TOP211: prior age alone never retires an actual HELD confirmation."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.confirmation_exit import ConfirmationEntry, ConfirmationEvaluation
from project_mai_tai.db.models import BrokerOrder, Fill, OmsManagedPosition, VirtualPosition, V2ConfirmationExitEvaluation
from project_mai_tai.services import schwab_1m_v2_bot as module
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2IntentEmitter
from tests.unit.test_confirmation_exit import _confirmation_store, _seed_confirmation_fill


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("state", ["held", "closed_flat", "missing_vp", "open_zero"])
async def test_real_prior_entry_retirement_and_actual_confirmation_emitter(monkeypatch, provider, state):
    now = datetime(2026, 10, 16, 14, tzinfo=UTC)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    monkeypatch.setattr(module, "datetime", Clock)
    factory, strategy_id, accounts = _confirmation_store()
    old = now-timedelta(days=1)
    order_id, fill_id = _seed_confirmation_fill(factory, strategy_id=strategy_id,
        account_id=accounts[provider], provider=provider, symbol="HELD", slot_id="held-slot", filled_at=old)
    with factory.begin() as session:
        order, fill = session.get(BrokerOrder, order_id), session.get(Fill, fill_id)
        account = "live:schwab_1m_v2" if provider == "schwab" else "live:webull"
        entry = ConfirmationEntry(order_id=order_id, fill_id=fill_id, broker_fill_id=fill.broker_fill_id,
            broker_order_id=order.broker_order_id, broker_account_name=account, symbol="HELD", filled_at=old,
            evaluation_bar_start_ms=int(now.timestamp()*1000), confirmation_bars=1, config_id=None,
            config_effective_at=datetime(1970, 1, 1, tzinfo=UTC))
        session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account,
            symbol="HELD", entry_order_id=order_id, entry_client_order_id=order.client_order_id,
            entry_price=Decimal(10), original_quantity=1, current_quantity=1 if state == "held" else 0,
            status="open" if state in {"held", "open_zero"} else "closed"))
        if state != "missing_vp":
            session.add(VirtualPosition(strategy_id=strategy_id, broker_account_id=accounts[provider],
                symbol="HELD", quantity=Decimal(1 if state == "held" else 0), average_price=Decimal(10)))
    settings = Settings(_env_file=None, strategy_schwab_1m_v2_confirmation_exit_enabled=True,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:webull")
    service = module.SchwabV2BotService(settings, session_factory=factory)
    wires = []
    class RedisBoundary:
        async def xadd(self, stream, fields, **kwargs):
            wires.append(json.loads(fields["data"]))
            return "controlled-stream-id"
    service.intent_emitter = SchwabV2IntentEmitter(settings, RedisBoundary(), account)
    service._confirmation_exit.add(entry)
    await service._retire_previous_confirmation_entries()
    assert service._confirmation_exit.pending_count == (0 if state == "closed_flat" else 1)
    evaluation = ConfirmationEvaluation(entry, entry.evaluation_bar_start_ms, "short")
    await service._emit_confirmation_evaluations([evaluation])
    if state == "closed_flat":
        assert not wires
    else:
        # Real publisher and real durable decision writer, not a replaced emitter
        # method/positions object: old HELD/UNKNOWN retains confirmation authority.
        wire, = wires
        assert wire["event_type"] == "v2_confirmation_exit" and wire["should_exit"] is True
        assert wire["source_order_id"] == str(order_id) and wire["source_fill_id"] == str(fill_id)
        assert wire["broker_account_name"] == account and wire["broker_order_id"] == entry.broker_order_id
    with factory() as session:
        row = session.scalar(select(OmsManagedPosition))
        assert row.status == ("open" if state in {"held", "open_zero"} else "closed")
        assert row.current_quantity == (1 if state == "held" else 0)


@pytest.mark.asyncio
async def test_real_decision_write_session_roll_rechecks_flat_before_publication(monkeypatch):
    clock = [datetime(2026, 10, 16, 7, 59, 59, tzinfo=UTC)]
    boundary = datetime(2026, 10, 16, 8, tzinfo=UTC)  # Actual 04 ET.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(module, "datetime", Clock)
    factory, strategy_id, accounts = _confirmation_store()
    stamp = clock[0]-timedelta(minutes=2)
    order_id, fill_id = _seed_confirmation_fill(factory, strategy_id=strategy_id,
        account_id=accounts["schwab"], provider="schwab", symbol="ROLL", slot_id="roll", filled_at=stamp)
    with factory.begin() as session:
        order, fill = session.get(BrokerOrder, order_id), session.get(Fill, fill_id)
        entry = ConfirmationEntry(order_id=order_id, fill_id=fill_id, broker_fill_id=fill.broker_fill_id,
            broker_order_id=order.broker_order_id, broker_account_name="live:schwab_1m_v2", symbol="ROLL",
            filled_at=stamp, evaluation_bar_start_ms=int(clock[0].timestamp()*1000), confirmation_bars=1,
            config_id=None, config_effective_at=datetime(1970, 1, 1, tzinfo=UTC))
        session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=entry.broker_account_name,
            symbol="ROLL", entry_order_id=order_id, entry_client_order_id=order.client_order_id,
            entry_price=Decimal(10), original_quantity=1, current_quantity=0, status="closed"))
        session.add(VirtualPosition(strategy_id=strategy_id, broker_account_id=accounts["schwab"],
            symbol="ROLL", quantity=Decimal(0), average_price=Decimal(0)))
    settings = Settings(_env_file=None, strategy_schwab_1m_v2_confirmation_exit_enabled=True,
        strategy_schwab_1m_v2_account_name=entry.broker_account_name)
    service = module.SchwabV2BotService(settings, session_factory=factory)
    sent = []
    class RedisBoundary:
        async def xadd(self, *args, **kwargs):
            sent.append((args, kwargs))
            return "controlled"
    service.intent_emitter = SchwabV2IntentEmitter(settings, RedisBoundary(), entry.broker_account_name)
    actual_writer = service._record_confirmation_evaluation
    def original_write_then_session_roll(evaluation):
        result = actual_writer(evaluation)
        clock[0] = boundary
        return result
    monkeypatch.setattr(service, "_record_confirmation_evaluation", original_write_then_session_roll)
    await service._emit_confirmation_evaluations([ConfirmationEvaluation(entry, entry.evaluation_bar_start_ms, "short")])
    assert not sent
    with factory() as session:
        row, = session.scalars(select(V2ConfirmationExitEvaluation)).all()
        assert row.source_fill_id == fill_id and row.published_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source_provider", ["schwab", "webull"])
@pytest.mark.parametrize("mirror_state", ["held", "absent_vp", "flat"])
async def test_actual_discovered_fanout_requires_both_aliases_flat(
        monkeypatch, source_provider, mirror_state):
    clock = [datetime(2026, 10, 15, 14, tzinfo=UTC)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(module, "datetime", Clock)
    factory, strategy_id, accounts = _confirmation_store()
    settings = Settings(_env_file=None, strategy_schwab_1m_v2_confirmation_exit_enabled=True,
        strategy_schwab_1m_v2_confirmation_account_neutral_discovery_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:webull")
    service = module.SchwabV2BotService(settings, session_factory=factory)
    source_account = "live:schwab_1m_v2" if source_provider == "schwab" else "live:webull"
    mirror_provider = "webull" if source_provider == "schwab" else "schwab"
    mirror_account = "live:webull" if mirror_provider == "webull" else "live:schwab_1m_v2"
    stamp = clock[0]-timedelta(seconds=30)
    slot = "actual-shared-fanout-slot"
    source_order, source_fill = _seed_confirmation_fill(factory, strategy_id=strategy_id,
        account_id=accounts[source_provider], provider=source_provider, symbol="PAIR", slot_id=slot, filled_at=stamp)
    # Discovery returns the actual durable nonempty fanout identity; it is not
    # supplied only to the seed helper and then silently omitted from the DTO.
    entry, = service._load_confirmation_entries()
    assert entry.fanout_slot_id == slot and entry.order_id == source_order and entry.fill_id == source_fill
    assert entry.broker_account_name == source_account
    mirror_order, _ = _seed_confirmation_fill(factory, strategy_id=strategy_id,
        account_id=accounts[mirror_provider], provider=mirror_provider, symbol="PAIR", slot_id=slot, filled_at=stamp)
    with factory.begin() as session:
        for provider, account, order_id in ((source_provider, source_account, source_order),
                                             (mirror_provider, mirror_account, mirror_order)):
            held = provider == mirror_provider and mirror_state == "held"
            order = session.get(BrokerOrder, order_id)
            session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account,
                symbol="PAIR", entry_order_id=order_id, entry_client_order_id=order.client_order_id,
                entry_price=Decimal(10), original_quantity=1, current_quantity=int(held),
                status="open" if held else "closed"))
            if provider != mirror_provider or mirror_state != "absent_vp":
                session.add(VirtualPosition(strategy_id=strategy_id, broker_account_id=accounts[provider],
                    symbol="PAIR", quantity=Decimal(int(held)), average_price=Decimal(10 if held else 0)))
    clock[0] += timedelta(days=1)
    service._confirmation_exit.add(entry)
    wires = []
    class RedisBoundary:
        async def xadd(self, stream, fields, **kwargs):
            wires.append(json.loads(fields["data"]))
            return "controlled-fanout-stream-id"
    service.intent_emitter = SchwabV2IntentEmitter(settings, RedisBoundary(), source_account)
    await service._retire_previous_confirmation_entries()
    assert service._confirmation_exit.pending_count == (0 if mirror_state == "flat" else 1)
    await service._emit_confirmation_evaluations([
        ConfirmationEvaluation(entry, entry.evaluation_bar_start_ms, "short")])
    if mirror_state == "flat":
        assert not wires
    else:
        wire, = wires
        assert wire["fanout_slot_id"] == slot and wire["should_exit"] is True
        assert wire["source_order_id"] == str(source_order) and wire["source_fill_id"] == str(source_fill)
        assert wire["broker_account_name"] == source_account
    with factory() as session:
        rows = {row.broker_account_name: row for row in session.scalars(select(OmsManagedPosition))}
        assert rows[source_account].status == "closed" and rows[source_account].current_quantity == 0
        assert rows[mirror_account].status == ("open" if mirror_state == "held" else "closed")
        assert rows[mirror_account].current_quantity == int(mirror_state == "held")
        mirror_vp = session.scalar(select(VirtualPosition).where(VirtualPosition.broker_account_id == accounts[mirror_provider]))
        if mirror_state == "absent_vp":
            assert mirror_vp is None
        else:
            assert mirror_vp.quantity == int(mirror_state == "held")
