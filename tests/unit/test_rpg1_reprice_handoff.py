"""Recorded readbacks, synthetic schedules; no real broker writes or latency claim."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.db.models import DashboardSnapshot, Fill, OmsManagedPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.atr_reprice_handoff import (
    AtrRepriceHandoff, HandoffJournal, MAX_READS, ReplacementDecision,
)
from tests.unit.test_rpg1_buy_readback import recorded


@pytest.fixture
def journal(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'handoffs.sqlite'}")
    DashboardSnapshot.__table__.create(engine)
    yield HandoffJournal(sessionmaker(bind=engine))
    engine.dispose()


class Adapter:
    def __init__(self, request, body, decode, clock):
        self.request, self.body, self.decode, self.clock = request, body, decode, clock
        self.cancelled, self.reads = [], []
        self.override = None

    async def submit_order(self, request):
        self.cancelled.append(request)
        return [ExecutionReport("accepted", request.client_order_id, origin="broker")]

    async def read_atr_resting_buy_after_cancel(self, request):
        self.reads.append(self.clock[0])
        return self.override or self.decode(request, self.body)


class Harness:
    def __init__(self, journal, broker):
        self.old, body, decode = recorded(broker)
        self.clock = [100.0]
        self.adapter = Adapter(self.old, body, decode, self.clock)
        self.router = RoutingBrokerAdapter(default_provider=broker,
            provider_by_account={self.old.broker_account_name: broker},
            factories_by_provider={broker: lambda: self.adapter})
        self.replacement = replace(self.old, client_order_id="new-parent", intent_type="open",
            metadata={"resting_entry": "true", "limit_price": "3.07"})
        self.decision = ReplacementDecision("ready", "current_strategy_gates", self.replacement)
        self.submitted, self.fills = [], []
        self.recorded = True
        self.raise_submit = False
        self.reject_submit = False
        self.journal = journal
        self.token = journal.prepare(self.old, slot="first", segment_id=123, now=self.clock[0])
        self.controller = self.restart()

    def restart(self):
        return AtrRepriceHandoff(journal=self.journal, adapter=self.router,
            prepare_replacement=self.prepare, submit_replacement=self.submit,
            record_fill=self.fill, now=lambda: self.clock[0])

    async def prepare(self, job):
        return self.decision

    async def submit(self, request):
        # Real OMS intent-lane/fill integration is separate from this coordinator harness.
        assert self.journal.read(self.token)["phase"] == "submitting"
        self.submitted.append(request)
        if self.raise_submit:
            raise TimeoutError("wire result unknown")
        return [ExecutionReport("rejected" if self.reject_submit else "accepted",
            request.client_order_id, origin="broker", reason="rejected" if self.reject_submit else "")]

    async def fill(self, old, report):
        assert old.client_order_id == report.client_order_id
        assert report.intent_type == "open" and report.side == "buy"
        assert self.journal.read(self.token)["no_rebuy"]
        self.fills.append(report)
        return self.recorded


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_recorded_empty_cancel_immediate_same_turn_and_duplicate_advance_is_single_buy(journal, broker):
    h = Harness(journal, broker)
    result = await h.controller.advance(h.token)
    assert result["phase"] == "placed"
    assert len(h.adapter.cancelled) == len(h.adapter.reads) == len(h.submitted) == 1
    assert result["cancel_started_at"] == result["cleared_at"] == result["submit_started_at"]
    await h.controller.advance(h.token)
    await h.restart().advance(h.token)
    assert len(h.submitted) == len(h.adapter.cancelled) == len(h.adapter.reads) == 1
    assert h.fills == []


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("readback", [
    AtrBuyReadback("unknown", "no_response"),
    AtrBuyReadback("working", "pending_cancel", Decimal(0)),
])
async def test_unknown_and_working_ack_keep_ownership_bounded_one_second_reads(journal, broker, readback):
    h = Harness(journal, broker)
    h.adapter.override = readback
    assert (await h.controller.advance(h.token))["phase"] == "waiting"
    for _ in range(50):
        await h.controller.advance(h.token)
    assert len(h.adapter.reads) == 1 and h.submitted == []
    h.controller = h.restart()
    for _ in range(MAX_READS):
        h.clock[0] += 1
        await h.controller.advance(h.token)
    assert len(h.adapter.cancelled) == 1
    assert len(h.adapter.reads) == MAX_READS
    assert journal.read(h.token)["phase"] == "held_unknown"
    h.adapter.override = None
    h.clock[0] += 300
    await h.restart().advance(h.token)
    assert not h.submitted  # Exhaustion/restoring a process never mints permission.


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_working_then_recorded_clear_replaces_without_another_bar_or_cancel(journal, broker):
    h = Harness(journal, broker)
    h.adapter.override = AtrBuyReadback("working", "pending_cancel", Decimal(0))
    await h.controller.advance(h.token)
    h.clock[0] += 1
    h.adapter.override = None
    assert (await h.controller.advance(h.token))["phase"] == "placed"
    assert len(h.adapter.cancelled) == len(h.submitted) == 1
    assert h.adapter.reads == [100, 101]


@pytest.mark.asyncio
async def test_brokers_advance_independently_when_one_readback_is_unknown(journal):
    primary, mirror = Harness(journal, "schwab"), Harness(journal, "webull")
    primary.adapter.override = AtrBuyReadback("unknown", "broker_timeout")
    left, right = await asyncio.gather(primary.controller.advance(primary.token),
                                      mirror.controller.advance(mirror.token))
    assert left["phase"] == "waiting" and right["phase"] == "placed"
    assert primary.submitted == [] and len(mirror.submitted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("broker,quantity,filled", [("schwab", 197, 31), ("webull", 98, 17)])
@pytest.mark.parametrize("terminal", [False, True])
async def test_synthetic_dollar_partial_books_original_buy_never_rebuys_remainder(journal, broker, quantity, filled, terminal):
    h = Harness(journal, broker)
    h.old = replace(h.old, client_order_id="partial-old", quantity=Decimal(quantity))
    h.token = journal.prepare(h.old, slot="first", segment_id=123, now=100)
    h.adapter.override = AtrBuyReadback("fills", "partial", Decimal(filled), Decimal("3.05"), terminal)
    result = await h.controller.advance(h.token)
    assert result["phase"] == ("filled" if terminal else "fills_waiting")
    assert h.fills[0].quantity == quantity and h.fills[0].filled_quantity == filled
    assert h.fills[0].client_order_id == "partial-old" and h.submitted == []
    h.clock[0] += 1
    h.adapter.override = AtrBuyReadback("cancelled_empty", "contradictory_zero", Decimal(0), terminal_cancel=True)
    await h.restart().advance(h.token)
    assert not h.submitted


@pytest.mark.asyncio
async def test_recorded_full_webull_fill_is_position_not_replacement(journal):
    h = Harness(journal, "webull")
    old, body, decode = recorded("webull", filled=True)
    h.token = journal.prepare(old, slot="first", segment_id=123, now=100)
    h.adapter.body, h.adapter.decode = body, decode
    result = await h.controller.advance(h.token)
    assert result["phase"] == "filled"
    assert h.fills[0].fill_price == Decimal("3.41") and h.fills[0].filled_quantity == 1
    assert not h.submitted


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["price", "accounting"])
async def test_fill_without_price_or_committed_accounting_retains_hold(journal, missing):
    h = Harness(journal, "schwab")
    h.adapter.override = AtrBuyReadback("fills", "partial", Decimal(1),
                                      None if missing == "price" else Decimal("3.05"), True)
    h.recorded = False
    result = await h.controller.advance(h.token)
    assert result["phase"] == "fills_waiting" and not h.submitted
    assert bool(h.fills) == (missing == "accounting")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["buy_flip", "window_1545", "watchlist_removed", "liquidity_floor", "segment_ended"])
async def test_cancel_clear_then_expiry_has_no_new_buy(journal, reason):
    h = Harness(journal, "schwab")
    h.decision = ReplacementDecision("expired", reason)
    result = await h.controller.advance(h.token)
    assert result["phase"] == "expired" and result["reason"] == reason and h.submitted == []


@pytest.mark.asyncio
async def test_clear_then_stale_quote_wait_resumes_latest_line_only(journal):
    h = Harness(journal, "schwab")
    h.decision = ReplacementDecision("wait", "stale_quote")
    assert (await h.controller.advance(h.token))["phase"] == "clear"
    assert not h.submitted
    h.decision = ReplacementDecision("ready", "fresh_quote",
        replace(h.replacement, metadata={"resting_entry": "true", "limit_price": "3.11"}))
    assert (await h.controller.advance(h.token))["phase"] == "placed"
    assert h.submitted[0].metadata["limit_price"] == "3.11"
    assert len(h.adapter.reads) == 1


@pytest.mark.asyncio
async def test_submit_unknown_after_restart_is_never_retried(journal):
    h = Harness(journal, "schwab")
    h.raise_submit = True
    assert (await h.controller.advance(h.token))["phase"] == "submit_unknown"
    await h.restart().advance(h.token)
    assert len(h.submitted) == 1


@pytest.mark.asyncio
async def test_crash_after_submit_claim_is_unknown_not_a_fresh_attempt(journal):
    h = Harness(journal, "schwab")
    job = journal.read(h.token)
    journal.change(h.token, job["revision"], phase="submitting")
    assert (await h.restart().advance(h.token))["phase"] == "submitting"
    assert not h.adapter.cancelled and not h.submitted


@pytest.mark.asyncio
async def test_competing_coordinators_cannot_send_two_buys(journal):
    h = Harness(journal, "schwab")
    await asyncio.gather(h.controller.advance(h.token), h.restart().advance(h.token))
    assert journal.read(h.token)["phase"] == "placed"
    assert len(h.submitted) == len(h.adapter.cancelled) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("broker_account_name", "live:another"), ("symbol", "OTHER"),
    ("strategy_code", "orb_schwab"), ("side", "sell"), ("quantity", Decimal(0)),
])
async def test_replacement_cannot_escape_old_order_scope(journal, field, value):
    h = Harness(journal, "schwab")
    h.decision = ReplacementDecision("ready", "bad_scope", replace(h.replacement, **{field: value}))
    result = await h.controller.advance(h.token)
    assert result["phase"] == "clear" and result["reason"] == "replacement_scope_invalid"
    assert not h.submitted


@pytest.mark.asyncio
async def test_submit_rejection_is_persisted_not_reported_as_resting(journal):
    h = Harness(journal, "webull")
    h.reject_submit = True
    result = await h.controller.advance(h.token)
    assert result["phase"] == "refused" and result["replacement_reasons"] == ["rejected"]


@pytest.mark.asyncio
async def test_persistence_failure_prevents_cancel_and_new_buy(journal, monkeypatch):
    h = Harness(journal, "schwab")
    def fail(*args, **kwargs):
        raise OSError("database unavailable")
    monkeypatch.setattr(journal, "change", fail)
    with pytest.raises(OSError):
        await h.controller.advance(h.token)
    assert not h.adapter.cancelled and not h.submitted


@pytest.mark.asyncio
async def test_router_cannot_fall_back_to_generic_cancel_confirmation(journal):
    h = Harness(journal, "webull")
    h.router._adapters_by_provider["webull"] = object()
    result = await h.router.read_atr_resting_buy_after_cancel(h.old)
    assert result.outcome == "unknown" and not result.can_replace


def test_restore_same_old_order_keeps_durable_ticket_and_rejects_different_slot(journal):
    h = Harness(journal, "schwab")
    assert journal.prepare(h.old, slot="first", segment_id=123, now=1000) == h.token
    with pytest.raises(ValueError, match="different reprice"):
        journal.prepare(h.old, slot="reclaim", segment_id=123, now=1000)


@pytest.mark.asyncio
@pytest.mark.parametrize("broker,quantity,filled", [("schwab", 197, 31), ("webull", 98, 17)])
async def test_coordinator_partial_callback_uses_original_oms_fill_and_managed_exit_path(tmp_path, broker, quantity, filled):
    from tests.unit.test_v2_managed_exit import _make_sf, _svc

    factory = _make_sf(tmp_path / "oms.sqlite")
    service = _svc(factory)
    h = Harness(HandoffJournal(factory), broker)
    metadata = {**h.old.metadata, "path": "ATR Flip", "atr_variant": "CW-v2-resting",
                "resting_entry": "true", "stop_price": "3.05", "limit_price": "3.07"}
    h.old = replace(h.old, quantity=Decimal(quantity), metadata=metadata)
    # A distinct original id avoids reusing the harness's already prepared fixture ticket.
    h.old = replace(h.old, client_order_id="dollar-partial-parent")
    h.token = h.journal.prepare(h.old, slot="first", segment_id=123, now=100)
    event = TradeIntentEvent(source_service="schwab-1m-v2", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=h.old.broker_account_name,
        symbol=h.old.symbol, side="buy", intent_type="open", quantity=Decimal(quantity),
        reason="schwab_1m_v2 ATR Flip CW-v2-resting", metadata=metadata))
    with factory() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2", name="v2")
        account = service.store.ensure_broker_account(session, h.old.broker_account_name,
                                                      provider="simulated", environment="test")
        intent = service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
        intent_id, strategy_id, account_id = intent.id, strategy.id, account.id
        original_open = replace(h.old, intent_type="open")
        await service._record_order_reports(session=session, intent=intent, strategy_id=strategy_id,
            broker_account_id=account_id, intent_event=event, request=original_open,
            reports=[ExecutionReport("accepted", h.old.client_order_id, broker_order_id=metadata["broker_order_id"],
                quantity=Decimal(quantity), symbol=h.old.symbol, origin="broker")])
        session.commit()

    async def account_fill(old, report):
        from project_mai_tai.db.models import TradeIntent
        with factory() as session:
            intent = session.get(TradeIntent, intent_id)
            # Exactly the existing accounting path, under the original OPEN intent, not the cancel.
            await service._record_order_reports(session=session, intent=intent, strategy_id=strategy_id,
                broker_account_id=account_id, intent_event=event, request=original_open, reports=[report])
            session.commit()
        return True

    h.controller.record_fill = account_fill
    h.adapter.override = AtrBuyReadback("fills", "synthetic_partial", Decimal(filled), Decimal("3.05"), False)
    await h.controller.advance(h.token)
    h.clock[0] += 1
    h.adapter.override = replace(h.adapter.override, terminal_cancel=True)
    assert (await h.controller.advance(h.token))["phase"] == "filled"
    with factory() as session:
        fills = list(session.scalars(select(Fill)))
        positions = list(session.scalars(select(OmsManagedPosition)))
        assert len(fills) == 1 and fills[0].quantity == filled
        assert len(positions) == 1 and positions[0].current_quantity == filled
        assert positions[0].status == "open"
        assert positions[0].broker_account_name == h.old.broker_account_name
    assert (h.old.broker_account_name, h.old.symbol) in service._managed_v2_symbols
    assert not h.submitted
