"""Real complete-list parser -> original ATR owner; SDK page boundary only is synthetic."""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from uuid import UUID

from project_mai_tai.broker_adapters import webull as adapter_module, webull_exit_census as census_module
from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget
from project_mai_tai.db.models import BrokerOrder, Fill, OmsManagedPosition, SystemIncident, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import webull_attach_riskoff as wb
from tests.unit.test_webull_attach_report_seam import _adapter
from tests.unit.test_wbattach1_riskoff import attach, closes, make_case as make_case
from tests.unit.test_q_unknown_ack_software_exit import software_exit


def order(coid, *, status="CANCELLED", filled=0, side="SELL", symbol="ABC", quantity=10):
    item = {"symbol": symbol, "side": side, "order_status": status,
            "qty": str(quantity), "filled_qty": str(filled)}
    if filled:
        item.update(filled_price="11", last_filled_time="2026-10-15T14:00:00+00:00")
    return {"client_order_id": coid, "order_id": "native-" + coid,
            "account_id": "ACC1", "items": [item]}


def children(case, *, filled=0, status="CANCELLED"):
    base = case[2].base
    return [order(wb.WebullBrokerAdapter._combo_leg_coid(base, suffix),
                  status="FILLED" if filled == 10 and suffix == "T" else status,
                  filled=filled if suffix == "T" else 0) for suffix in ("T", "S")]


def install(case, monkeypatch, pages):
    adapter = _adapter()
    ticks, seen = [1000.], []
    budget = QueryBudget(clock=lambda: ticks[0])
    reader = census_module.ExitCensusReader(budget, 2., clock=lambda: ticks[0])
    adapter._query_budget = budget
    adapter._today_reader = SimpleNamespace(cycle_seconds=2.)
    monkeypatch.setattr(census_module, "datetime", wb.datetime)
    monkeypatch.setattr(adapter_module, "datetime", wb.datetime)
    monkeypatch.setattr(census_module, "shared_exit_census", lambda *args: reader)
    def fetch(account, cursor):
        assert account.account_id == "ACC1"
        seen.append(cursor)
        case[4][0] += timedelta(microseconds=1)
        value = pages(cursor)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(adapter, "_today_page", fetch)
    details = []
    adapter._q_detail_calls = details
    def no_detail(*args, **kwargs):
        details.append(args)
        return SimpleNamespace(body=None, status_code=200)
    monkeypatch.setattr(adapter, "_budgeted_detail", no_detail)
    async def list_proof(**kwargs):
        # The deterministic clock must distinguish cancel completion from the
        # subsequent HTTP request START, not merely from its response arrival.
        case[4][0] += timedelta(microseconds=1)
        return await adapter.confirm_exit_pair_from_today_list(**kwargs)
    monkeypatch.setattr(case[2], "confirm_exit_pair_from_today_list", list_proof)
    return adapter, seen, lambda: ticks.__setitem__(0, ticks[0] + 3.)


@pytest.mark.asyncio
@pytest.mark.parametrize("filled", [0, 2, 10])
@pytest.mark.parametrize("terminal", ["CANCELLED", "FAILED"])
async def test_detail_unreadable_valid_list_accounts_children_and_sells_only_owned_residual(make_case, monkeypatch, filled, terminal):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    rows = children(case, filled=filled, status=terminal)
    adapter, seen, advance = install(case, monkeypatch, lambda cursor: (200, {"orders": rows, "has_next": False}))
    case[2].total -= filled
    assert await software_exit(case) == "closed"
    assert adapter._q_detail_calls == []
    assert seen == [""] and len(case[2].cancelled) == 1
    assert [r.quantity for r in closes(case[2])] == ([] if filled == 10 else [Decimal(10 - filled)])
    assert case[2].total == 1000
    with case[1]() as session:
        sold = list(session.scalars(select(Fill).where(Fill.side == "sell")))
        assert sum((row.quantity for row in sold), Decimal(0)) == 10
        assert session.scalar(select(OmsManagedPosition)).status == "closed"


@pytest.mark.asyncio
async def test_readable_list_working_child_keeps_native_owner_and_no_software_sell(make_case, monkeypatch):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    rows = children(case)
    rows[1]["items"][0]["order_status"] = "WORKING"
    install(case, monkeypatch, lambda cursor: (200, {"orders": rows, "has_next": False}))
    assert await software_exit(case) == "refused"
    assert closes(case[2]) == [] and case[2].total == 1010


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_sell", [False, True])
async def test_more_than_two_pages_resume_without_recancel_and_late_foreign_sell_blocks(make_case, monkeypatch, foreign_sell):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    initial = children(case)
    def pages(cursor):
        if not cursor:
            return 200, {"orders": initial, "has_next": True}
        if cursor == initial[-1]["client_order_id"]:
            return 200, {"orders": [order("page2", side="BUY", status="WORKING")], "has_next": True}
        assert cursor == "page2"
        return 200, {"orders": [order("page3", side="SELL" if foreign_sell else "BUY", status="WORKING")], "has_next": False}
    _, seen, advance = install(case, monkeypatch, pages)
    assert await software_exit(case) == "refused"  # Exactly two bounded pages; not complete proof.
    assert len(seen) == 2 and closes(case[2]) == []
    advance()
    assert await software_exit(case) == ("refused" if foreign_sell else "closed")
    assert len(seen) == 3 and len(case[2].cancelled) == 1
    assert len(closes(case[2])) == (0 if foreign_sell else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_damage", [None, "missing", "changed_native_id"])
async def test_one_complete_census_proves_all_original_attempts_without_repeated_page_reads(make_case, monkeypatch, prior_damage):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    old_base = "CONTROLLED-old-protect"
    old_rows = [order(wb.WebullBrokerAdapter._combo_leg_coid(old_base, suffix)) for suffix in ("T", "S")]
    for row in old_rows:
        row["order_id"] = "broker-" + row["client_order_id"]
    with case[1].begin() as session:
        entry = session.get(BrokerOrder, case[3])
        managed = session.scalar(select(OmsManagedPosition))
        parent = session.get(TradeIntent, UUID(entry.payload[wb.KEY]["attempts"][0]["intent_id"]))
        event = TradeIntentEvent(source_service="CONTROLLED_PRIOR_REFUSAL", payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2", broker_account_name=case[2].account, symbol="ABC", side="sell",
            intent_type="close", quantity=Decimal(10), reason="WBATTACH1_PROTECTION",
            metadata={"entry_order_id": str(entry.id), "managed_row_id": str(managed.id)}))
        prior = case[0].store.create_trade_intent(session, strategy=session.get(wb.Strategy, parent.strategy_id),
            broker_account=session.get(wb.BrokerAccount, parent.broker_account_id), event=event)
        prior.status = "cancelled"
        state = dict(entry.payload[wb.KEY])
        prior_attempt = {"base": old_base, "quantity": "10", "intent_id": str(prior.id),
            "phase": "released", "cancel_requested": True, "terminal_proof": [
                wb.report_payload(case[2].report(row["client_order_id"])) for row in old_rows]}
        state["attempts"] = [prior_attempt, *state["attempts"]]
        entry.payload = {**entry.payload, wb.KEY: state}
    rows = (old_rows[:1] if prior_damage == "missing" else old_rows) + children(case)
    if prior_damage == "changed_native_id":
        rows[0]["order_id"] = "FOREIGN-native-id"
    _, seen, _ = install(case, monkeypatch, lambda cursor: (200, {"orders": rows, "has_next": False}))
    assert await software_exit(case) == ("refused" if prior_damage else "closed")
    assert seen == [""] and len(case[2].cancelled) == 1
    assert len(closes(case[2])) == (0 if prior_damage else 1)


@pytest.mark.asyncio
async def test_unreadable_list_opens_visible_incident_then_existing_retry_succeeds(make_case, monkeypatch):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    with case[1].begin() as session:
        first = session.scalar(select(SystemIncident))
        assert first.payload["entry_id"] == str(case[3])
        assert first.payload["entry_client_order_id"] == "owned-entry"
        first_id, first_opened_at = first.id, first.opened_at
        # Exercise adoption of a pre-fix incident keyed by its legacy COID.
        first.payload = {**first.payload, "entry_id": "owned-entry"}
    readable = [False]
    rows = children(case)
    _, seen, advance = install(case, monkeypatch,
        lambda cursor: (200, {"orders": rows, "has_next": False}) if readable[0] else (200, None))
    assert await software_exit(case) == "refused" and closes(case[2]) == []
    with case[1]() as session:
        incidents = list(session.scalars(select(SystemIncident)))
        assert len(incidents) == 1 and "Exit waiting for broker proof: ABC" in incidents[0].title
        assert (incidents[0].id, incidents[0].opened_at) == (first_id, first_opened_at)
        assert incidents[0].payload["entry_id"] == str(case[3])
        assert incidents[0].payload["entry_client_order_id"] == "owned-entry"
    for reference in (str(case[3]), "owned-entry"):
        await wb.critical(case[0], case[2].account, "ABC", "exit waiting for broker proof: ABC", entry_id=reference)
    with case[1]() as session:
        incidents = list(session.scalars(select(SystemIncident)))
        assert len(incidents) == 1
        assert (incidents[0].id, incidents[0].opened_at) == (first_id, first_opened_at)
    readable[0] = True
    advance()
    assert await software_exit(case) == "closed"
    assert len(seen) == 2 and len(case[2].cancelled) == len(closes(case[2])) == 1


@pytest.mark.asyncio
async def test_cancel_refusal_is_not_proof_but_valid_complete_list_still_releases(make_case, monkeypatch):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    rows = children(case)
    install(case, monkeypatch, lambda cursor: (200, {"orders": rows, "has_next": False}))
    cancel = case[2].cancel_exit_pair
    async def refused(**binding):
        await cancel(**binding)
        raise ValueError("CONTROLLED 417 cannot cancel")
    monkeypatch.setattr(case[2], "cancel_exit_pair", refused)
    assert await software_exit(case) == "closed"
    assert len(case[2].cancelled) == len(closes(case[2])) == 1


@pytest.mark.asyncio
async def test_failed_partial_child_is_booked_once_across_list_wait_and_readable_retry(make_case, monkeypatch):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    rows = children(case, filled=2, status="FAILED")
    rows[1]["items"][0]["order_status"] = "WORKING"
    _, seen, advance = install(case, monkeypatch, lambda cursor: (200, {"orders": rows, "has_next": False}))
    case[2].total -= 2
    assert await software_exit(case) == "refused" and closes(case[2]) == []
    rows[1]["items"][0]["order_status"] = "CANCELLED"
    advance()
    assert await software_exit(case) == "closed"
    assert [r.quantity for r in closes(case[2])] == [Decimal(8)] and case[2].total == 1000
    with case[1]() as session:
        children_fills = list(session.scalars(select(Fill).where(Fill.side == "sell", Fill.price == Decimal(11))))
        assert len(children_fills) == 1 and children_fills[0].quantity == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_child", "empty", "scope", "side", "top_symbol",
    "top_side", "top_status", "top_fill", "coid_alias", "quantity", "has_next_conflict", "continuation_missing"])
async def test_incomplete_or_conflicting_list_never_becomes_absence_proof(make_case, monkeypatch, damage):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    rows = children(case)
    body = {"orders": rows, "has_next": False}
    if damage == "missing_child":
        rows.pop()
    elif damage == "empty":
        rows.clear()
    elif damage == "scope":
        rows[0]["items"][0].pop("symbol")
    elif damage == "side":
        rows[0]["items"][0].pop("side")
    elif damage == "top_symbol":
        rows[0]["symbol"] = "OTHER"
    elif damage == "top_side":
        rows[0]["side"] = "BUY"
    elif damage == "top_status":
        rows[0]["status"] = "WORKING"
    elif damage == "top_fill":
        rows[0]["filledQty"] = "2"
    elif damage == "coid_alias":
        rows[0]["clientOrderId"] = "foreign"
    elif damage == "quantity":
        rows[0]["items"][0]["qty"] = "1000"
    elif damage == "has_next_conflict":
        body["hasNext"] = True
    else:
        body.pop("has_next")
    install(case, monkeypatch, lambda cursor: (200, body))
    assert await software_exit(case) == "refused"
    assert closes(case[2]) == [] and case[2].total == 1010


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["claim_commit", "protect_persist"])
@pytest.mark.parametrize("owner", ["software", "attach_riskoff"])
async def test_cancel_after_claim_commit_cleans_exact_unsent_owner_then_retry(make_case, monkeypatch, point, owner):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    service = case[0]
    entered, release = asyncio.Event(), asyncio.Event()
    state, persist = wb._atr_state, service._persist_webull_protect_state
    async def held_state(*args, **kwargs):
        result = await state(*args, **kwargs)
        mutate = args[4] if len(args) > 4 else kwargs.get("mutate")
        if point == "claim_commit" and mutate is not None and mutate.__name__ == "claim":
            entered.set()
            await release.wait()
        return result
    async def held_persist(*args, **kwargs):
        entered.set()
        await release.wait()
        return await persist(*args, **kwargs)
    monkeypatch.setattr(wb, "_atr_state", held_state)
    if point == "protect_persist":
        monkeypatch.setattr(service, "_persist_webull_protect_state", held_persist)
    async def drive():
        if owner == "software":
            return await software_exit(case)
        return await wb.request_atr_exit(service, case[2].account, "ABC", "owned-entry", "CONTROLLED_REAL_EXIT")
    task = asyncio.create_task(drive())
    await asyncio.wait_for(entered.wait(), 3)
    with case[1]() as session:
        assert session.get(BrokerOrder, case[3]).payload[wb.KEY]["exit_phase"] == "claimed"
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with case[1]() as session:
        assert not session.get(BrokerOrder, case[3]).payload[wb.KEY].get("exit_phase")
    assert not service.__dict__.get("_wbattach_exit_owners") and closes(case[2]) == []
    monkeypatch.setattr(wb, "_atr_state", state)
    monkeypatch.setattr(service, "_persist_webull_protect_state", persist)
    await drive()
    assert len(closes(case[2])) == len(case[2].cancelled) == 1


@pytest.mark.asyncio
async def test_q_corrected_retry_cannot_reuse_first_wire_proof_after_rejection(make_case, monkeypatch):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    service, broker = case[0], case[2]
    async def rejected(request):
        broker.requests.append(request)
        broker.total = Decimal(0)  # Inventory changed after the first, proof-checked wire.
        return [broker.report(request.client_order_id, event="rejected")]
    monkeypatch.setattr(broker, "submit_order", rejected)
    monkeypatch.setattr(service, "_webull_confirmation_close_can_retry_corrected", lambda reports: True)
    def forbidden(request):
        pytest.fail("Q cannot construct a corrected retry from a consumed first-wire proof")
    monkeypatch.setattr(service, "_corrected_webull_confirmation_close_request", forbidden)
    await software_exit(case, "oms_v2_managed_exit:CONFIRMATION_EXIT",
                        confirmation_context={"cw_exit_tag": "CONFIRMATION_EXIT"})
    assert len(closes(broker)) == 1 and broker.total == 0


@pytest.mark.asyncio
async def test_failed_real_exit_does_not_rearm_native_writer_before_next_exit_trigger(make_case):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    case[2].known = False
    assert await software_exit(case) == "refused"
    case[2].known = True
    assert await attach(case) is False
    assert len(case[2].requests) == 1 and len(case[2].cancelled) == 1
    assert await software_exit(case) == "closed"
    assert len(case[2].requests) == 2 and len(case[2].cancelled) == 1


def test_exit_census_nonprogress_and_page_cap_never_prove_complete():
    for failure in ("nonprogress", "pagecap"):
        ticks = [1000.]
        reader = census_module.ExitCensusReader(QueryBudget(clock=lambda: ticks[0]), 2., clock=lambda: ticks[0])
        calls = []
        def page(cursor):
            calls.append(cursor)
            coid = "same" if failure == "nonprogress" else "row-" + str(len(calls))
            return 200, {"orders": [order(coid, side="BUY")], "has_next": True}
        with pytest.raises(ValueError, match="nonprogress|page cap"):
            for _ in range(10):
                assert reader.read("ACC1", page) is None
                ticks[0] += 3.
        assert len(calls) == (2 if failure == "nonprogress" else 20)


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["prepare", "last_positions"])
async def test_list_proof_ages_during_preparation_and_cannot_reach_sell(make_case, monkeypatch, point):
    from project_mai_tai.oms import managed_sell_prewire
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    rows = children(case)
    install(case, monkeypatch, lambda cursor: (200, {"orders": rows, "has_next": False}))
    prepare = managed_sell_prewire.prepare_managed_sell
    async def delayed(*args, **kwargs):
        result = await prepare(*args, **kwargs)
        case[4][0] += timedelta(seconds=3)
        return result
    if point == "prepare":
        monkeypatch.setattr(managed_sell_prewire, "prepare_managed_sell", delayed)
    else:
        positions, calls = case[2].list_account_positions, [0]
        async def delayed_positions(account):
            rows = await positions(account)
            calls[0] += 1
            if calls[0] == 2:
                case[4][0] += timedelta(seconds=3)
            return rows
        monkeypatch.setattr(case[2], "list_account_positions", delayed_positions)
    assert await software_exit(case) == "refused" and closes(case[2]) == []
    with case[1]() as session:
        state = session.get(BrokerOrder, case[3]).payload[wb.KEY]
        assert not state.get("exit_phase") and state["unsent_handoffs"]


def test_slow_first_http_counts_from_request_start_not_response_arrival(monkeypatch):
    ticks, stamps = [1000.], [datetime(2026, 10, 15, 14, tzinfo=UTC)]
    started_at = stamps[0]
    monkeypatch.setattr(census_module, "datetime", SimpleNamespace(now=lambda tz: stamps[0]))
    reader = census_module.ExitCensusReader(QueryBudget(clock=lambda: ticks[0]), 2., clock=lambda: ticks[0])
    def slow_first(cursor):
        assert cursor == ""
        ticks[0] += 31.
        stamps[0] += timedelta(seconds=31)
        return 200, {"orders": [], "has_next": False}
    with pytest.raises(ValueError, match="acquisition stale"):
        reader.read("ACC1", slow_first)
    assert reader.scans["ACC1"].first_at == started_at
    assert reader.scans["ACC1"].first_clock == 1000.
    assert reader.scans["ACC1"].failed is True


def test_cancel_during_existing_request_discards_pre_cancel_partial_census(monkeypatch):
    ticks, stamps = [1000.], [datetime(2026, 10, 15, 14, tzinfo=UTC)]
    cancelled_at, seen = [], []
    monkeypatch.setattr(census_module, "datetime", SimpleNamespace(now=lambda tz: stamps[0]))
    reader = census_module.ExitCensusReader(QueryBudget(clock=lambda: ticks[0]), 2., clock=lambda: ticks[0])
    def page(cursor):
        seen.append(cursor)
        if len(seen) == 1:
            ticks[0] += 1.
            stamps[0] += timedelta(seconds=1)
            cancelled_at.append(stamps[0])  # Another owner cancelled while this request was in flight.
            ticks[0] += 1.
            stamps[0] += timedelta(seconds=1)  # First response arrived AFTER that cancel.
            return 200, {"orders": [order("first", side="BUY")], "has_next": True}
        if len(seen) == 2:
            assert cursor == "first"
            return 200, {"orders": [order("second", side="BUY")], "has_next": True}
        assert cursor == "", "a request begun before cancel cannot resume as post-cancel proof"
        return 200, {"orders": [order("fresh", side="BUY")], "has_next": False}
    assert reader.read("ACC1", page) is None
    ticks[0] += 3.
    stamps[0] += timedelta(seconds=3)
    proof = reader.read("ACC1", page, after=cancelled_at[0])
    assert seen == ["", "first", ""]
    assert proof["pages"] == 1 and len(proof["orders"]) == 1
    assert proof["orders"][0]["client_order_id"] == "fresh"
    assert datetime.fromisoformat(proof["started_at"]) > cancelled_at[0]
