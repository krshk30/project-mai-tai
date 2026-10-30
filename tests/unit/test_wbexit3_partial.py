"""SOURCE UNRUN: exact SCALE through real managed/ATR/O14/canonical writers.

Only broker observations and clock are controlled. No helper grants ownership,
terminality, prewire permission, native protection or successful cancellation.
The 10-owned / 1000-manual inputs are controlled, not historical native receipts.
"""
from dataclasses import replace
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import AccountPosition, BrokerOrder, Fill, OmsManagedPosition, TradeIntent, VirtualPosition
from project_mai_tai.oms import service as oms, wbexit2, wbexit3
from project_mai_tai.oms import webull_attach_riskoff as wb
from tests.unit.test_wbattach1_riskoff import Broker, make_case as make_case, closes
from tests.unit.test_wbexit2_route import ControlledOwnerBroker, owner_case, close_case
from tests.unit.test_wbexit3_obligation import enable, obligations, restarted


class ExactPartialPort(ControlledOwnerBroker):
    """External typed replies with a distinct native identity for EACH SELL."""
    def __init__(self, *args):
        super().__init__(*args)
        self.results = {}
        self.cancel_child_fill = Decimal(0)
        self.unknown_scale = False
        self.cancel_fetches = []

    async def cancel_exit_pair(self, **binding):
        # Observe the actual durable partial reservation before physical cancel.
        state = next(row for row in obligations((self.service, self.factory, self, None, None))
                     if row["kind"] == "SCALE" and row.get("active_token"))
        assert binding["base_client_order_id"] in state["partial_cancel_bases"]
        assert state["partial_originals"] and state["requested"] == "3"
        self.child_filled = self.cancel_child_fill
        # Bypass only the inherited TEST port's WB2-only assertion, not any app
        # owner/proof/gate. This remains the controlled external ACK surface.
        return await Broker.cancel_exit_pair(self, **binding)

    async def submit_order(self, request):
        if request.metadata.get("webull_exit_only_pair") == "true":
            return await super().submit_order(request)
        assert 0 < request.quantity <= self.total - Decimal(1000)
        self.requests.append(request)
        if request.intent_type == "scale" and self.unknown_scale:
            self.results[request.client_order_id] = None
            raise TimeoutError("CONTROLLED unknown scale acknowledgment")
        native = "CONTROLLED-" + request.client_order_id
        report = ExecutionReport("filled", request.client_order_id, broker_order_id=native,
            broker_fill_id=native + ":execution", symbol=self.symbol, side="sell", intent_type=request.intent_type,
            quantity=request.quantity, filled_quantity=request.quantity, fill_price=Decimal(str(self.bid)),
            reported_at=oms.utcnow(), origin="broker")
        self.results[request.client_order_id] = report
        self.total -= request.quantity
        return [report]

    async def fetch_order_update(self, request):
        if request.client_order_id in self.results:
            self.cancel_fetches.append(request.client_order_id)
            result = self.results[request.client_order_id]
            return replace(result, reported_at=oms.utcnow()) if result is not None else None
        return await super().fetch_order_update(request)


async def partial_case(make_case, monkeypatch):
    case = await owner_case(make_case, port_type=ExactPartialPort,
        at_clock=datetime(2026, 10, 26, 19, 59, tzinfo=UTC))
    await enable(case)
    case[4][0] = datetime(2026, 10, 26, 21, 0, tzinfo=UTC)
    monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: "PM")
    case[0]._managed_v2_symbols.add((case[2].account, case[2].symbol))
    case[2].bid = 10.2
    return case


async def bare_PM_case(make_case, monkeypatch):
    service, factory, old, entry_id, clock = make_case()
    clock[0] = datetime(2026, 10, 26, 8, 5, tzinfo=UTC)  # 04:05 ET, exit-only admission.
    port = ExactPartialPort(service, factory, old.account)
    service.broker_adapter = port
    for key in ("_v2_eod_handover_ready", "_fetch_quote_for_order"):
        service.__dict__.pop(key, None)
    for flag in ("strategy_schwab_1m_v2_wbexit2_enabled", "strategy_schwab_1m_v2_keepwatch1_enabled",
                 "strategy_schwab_1m_v2_brokerdecides1_enabled"):
        object.__setattr__(service.settings, flag, True)
    with factory.begin() as db:
        entry = db.get(BrokerOrder, entry_id)
        entry.created_at = entry.submitted_at = clock[0]
        db.get(TradeIntent, entry.intent_id).created_at = clock[0]
        db.scalar(select(Fill)).filled_at = clock[0]
        db.scalar(select(OmsManagedPosition)).entry_time = clock[0]
        db.scalar(select(AccountPosition)).source_updated_at = clock[0]
    case = service, factory, port, entry_id, clock
    await wbexit2.hydrate_at_boot(service)
    await enable(case)
    monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: "AM")
    service._managed_v2_symbols.add((port.account, port.symbol))
    port.bid = 10.2
    return case


async def scale(case):
    service, factory, port, _, _ = case
    with factory() as db:
        row = db.scalar(select(OmsManagedPosition))
        position, row_id = service._hydrate_v2_position(row), str(row.id)
    return await service._emit_v2_exit_on_loop_claimed(port.account, port.symbol, position,
        float(port.entry_price), kind="SCALE", reference_price=port.bid,
        reason="oms_v2_managed_exit:SCALE_PCT2", bid=port.bid, close_on_fill=True,
        sell_qty=3, level="PCT2", expected_managed_row_id=row_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["PM0405_bare", "AH1700_original_pair"])
async def test_exact_three_retains_seven_then_later_hard_stop_sells_seven(make_case, monkeypatch, record_property, period):
    case = await (bare_PM_case(make_case, monkeypatch) if period == "PM0405_bare" else partial_case(make_case, monkeypatch))
    pair_count = int(period == "AH1700_original_pair")
    await scale(case)
    sells = closes(case[2])
    assert len(sells) == 1 and sells[0].intent_type == "scale" and sells[0].quantity == 3
    assert sells[0].order_type == "limit" and sells[0].metadata["extended_hours"] == "true"
    assert case[2].total == Decimal(1007)
    with case[1]() as db:
        row = db.scalar(select(OmsManagedPosition))
        assert row.status == "open" and row.current_quantity == 7 and "PCT2" in row.scales_done
        assert db.scalar(select(VirtualPosition.quantity)) == 7
        assert (case[2].account, case[2].symbol) in case[0]._managed_v2_symbols
        assert not db.get(BrokerOrder, case[3]).payload[wb.KEY].get("exit_phase")
    partial = next(row for row in obligations(case) if row["kind"] == "SCALE")
    assert partial["phase"] == "RESOLVED" and partial["residual_quantity"] == "7"
    assert len(case[2].cancelled) == pair_count
    # A separate real HARD invocation uses actual canonical prior-scale fills.
    case[4][0] += timedelta(seconds=11)
    await close_case(case, "CW_HARD_STOP")
    sells = closes(case[2])
    assert [(r.intent_type, r.quantity) for r in sells] == [("scale", Decimal(3)), ("close", Decimal(7))]
    assert case[2].total == Decimal(1000) and len(case[2].cancelled) == pair_count
    with case[1]() as db:
        row = db.scalar(select(OmsManagedPosition))
        assert row.status == "closed" and row.current_quantity == 0
        assert db.scalar(select(VirtualPosition.quantity)) == 0
    record_property("exact_partial", json.dumps({"requested": 3, "owned": 10, "manual": 1000,
        "scale_sent": 3, "residual_monitored": 7, "later_hard_sent": 7,
        "residual_cover": "existing_software_ladder", "native_protection": "UNKNOWN_UNAVAILABLE_PM_AH",
        "label": "CONTROLLED_NOT_NATIVE_PASS", "period": period}))


@pytest.mark.asyncio
async def test_two_actual_native_fills_after_zero_baseline_leave_exact_one_software(make_case, monkeypatch):
    case = await partial_case(make_case, monkeypatch)
    case[2].cancel_child_fill = Decimal(2)
    await scale(case)
    assert [r.quantity for r in closes(case[2])] == [Decimal(1)]
    assert case[2].total == Decimal(1007)
    partial = obligations(case)[0]
    assert partial["phase"] == "RESOLVED" and Decimal(partial["partial_native_progress"]) == 2
    with case[1]() as db:
        assert db.scalar(select(OmsManagedPosition.current_quantity)) == 7
        sells = list(db.scalars(select(Fill).where(Fill.side == "sell")))
        assert sorted(f.quantity for f in sells) == [Decimal(1), Decimal(2)]


@pytest.mark.asyncio
async def test_initial_partial_native_timing_unknown_never_guesses_subtraction(make_case, monkeypatch):
    case = await partial_case(make_case, monkeypatch)
    # Original pair was already terminal with a partial fill BEFORE the scale
    # duty observed a zero baseline; actual fill is attributed, timing UNKNOWN.
    case[2].child_filled = Decimal(2)
    case[2].cancelled.append(case[2].requests[0].client_order_id)
    await scale(case)
    assert not closes(case[2])
    assert obligations(case)[0]["phase"] == "RETRY"
    for _ in range(2):
        case[4][0] += timedelta(seconds=61)
        await scale(case)
    assert obligations(case)[0]["phase"] == "PAGED" and not closes(case[2])
    with case[1]() as db:
        assert db.scalar(select(OmsManagedPosition.status)) == "open"


@pytest.mark.asyncio
async def test_unknown_partial_ACK_then_next_day_boot_keeps_same_order_no_second_sell(make_case, monkeypatch):
    case = await partial_case(make_case, monkeypatch)
    case[2].unknown_scale = True
    await scale(case)
    assert len(closes(case[2])) == 1
    before = obligations(case)[0]
    assert before["phase"] == "RETRY" and before["partial_sell"] is not None
    second = restarted(case)
    case[4][0] += timedelta(days=1)
    await wbexit2.hydrate_at_boot(second[0])
    await wbexit3.hydrate_at_boot(second[0])
    await scale(second)
    assert len(closes(case[2])) == 1 and case[2].total == Decimal(1010)
    after = obligations(second)[0]
    assert after["owner_id"] == before["owner_id"] and after["opened_at"] == before["opened_at"]
    assert after["partial_sell"]["coid"] == before["partial_sell"]["coid"]
    assert after["phase"] in {"RETRY", "PAGED"}
    assert case[2].cancel_fetches == [before["partial_sell"]["coid"]]


@pytest.mark.asyncio
async def test_full_actual_native_exit_displaces_partial_without_any_software_sell(make_case, monkeypatch):
    case = await partial_case(make_case, monkeypatch)
    case[2].cancel_child_fill = Decimal(10)
    await scale(case)
    assert not closes(case[2]) and case[2].total == Decimal(1000)
    partial = obligations(case)[0]
    assert partial["phase"] == "RESOLVED" and Decimal(partial["partial_native_progress"]) == 10
    with case[1]() as db:
        assert db.scalar(select(OmsManagedPosition.current_quantity)) == 0
        assert db.scalar(select(VirtualPosition.quantity)) == 0


@pytest.mark.asyncio
async def test_actual_cancelled_one_fill_retries_only_two_after_original_read(make_case, monkeypatch):
    case = await partial_case(make_case, monkeypatch)
    case[2].unknown_scale = True
    await scale(case)
    request = closes(case[2])[0]
    native = "CONTROLLED-" + request.client_order_id
    # Actual native terminal READ has one fill and a cancelled two-share
    # remainder. The real canonical writer must attribute that ONE first.
    case[2].results[request.client_order_id] = ExecutionReport("cancelled", request.client_order_id,
        broker_order_id=native, broker_fill_id=native + ":one", symbol=case[2].symbol,
        side="sell", intent_type="scale", quantity=Decimal(3), filled_quantity=Decimal(1),
        fill_price=Decimal(str(case[2].bid)), reported_at=case[4][0], origin="broker")
    case[2].total -= 1  # Controlled broker inventory moves with the actual external execution.
    case[2].unknown_scale = False
    case[4][0] += timedelta(seconds=11)
    await scale(case)
    assert case[2].cancel_fetches == [request.client_order_id]
    assert [r.quantity for r in closes(case[2])] == [Decimal(3), Decimal(2)]
    assert case[2].total == Decimal(1007) and len(case[2].cancelled) == 1
    assert obligations(case)[0]["phase"] == "RESOLVED"
    with case[1]() as db:
        assert db.scalar(select(OmsManagedPosition.current_quantity)) == 7
        assert db.scalar(select(VirtualPosition.quantity)) == 7


@pytest.mark.asyncio
async def test_real_preparation_commit_after_deadline_is_joined_and_proven_unsent(make_case, monkeypatch):
    case = await partial_case(make_case, monkeypatch)
    original = case[0]._run_db
    delayed = []
    async def delayed_commit_receipt(fn, *, commit=True):
        result = await original(fn, commit=commit)
        if commit and isinstance(result, UUID) and not delayed:
            with case[1]() as db:
                intent = db.get(TradeIntent, result)
                prepared_scale = intent is not None and intent.intent_type == "scale"
            if prepared_scale:
                delayed.append(result)
                # Real nonblocking elapsed delay at the committed private DB
                # port. Do not mock prewire proof, ownership or timeout success.
                await asyncio.sleep(6.05)
        return result
    monkeypatch.setattr(case[0], "_run_db", delayed_commit_receipt)
    with pytest.raises(TimeoutError):
        await scale(case)
    assert len(delayed) == 1 and not closes(case[2])
    assert obligations(case)[0]["active_token"] is None
    with case[1]() as db:
        order = db.scalar(select(BrokerOrder).where(BrokerOrder.intent_id == delayed[0]))
        assert order.status == "aborted" and order.submitted_at is None and not order.broker_order_id
        assert not list(db.scalars(select(Fill).where(Fill.order_id == order.id)))
        assert not db.get(BrokerOrder, case[3]).payload[wb.KEY].get("exit_phase")
    assert not case[0].__dict__.get("_wbattach_exit_owners")
    case[4][0] += timedelta(seconds=11)
    await scale(case)
    assert [r.quantity for r in closes(case[2])] == [Decimal(3)]
    assert obligations(case)[0]["phase"] == "RESOLVED"
