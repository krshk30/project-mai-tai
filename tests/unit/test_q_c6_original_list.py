"""TOP14:1x: actual paged LIST parser under the real C6 owner; no DETAIL."""

from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
import asyncio
from datetime import UTC, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters import webull as adapter_module, webull_exit_census as census_module
from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot
from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget
from project_mai_tai.db.models import BotControlAttempt, BotControlCommand, BrokerOrder, Fill, OmsManagedPosition, SystemIncident, TradeIntent, VirtualPosition
from project_mai_tai.oms import webull_attach_riskoff as wb, webull_c6_proof as proof_module
from project_mai_tai.oms.service import oco_exit_client_order_id
from tests.unit.test_killsw1_atr_stop import book as book, drive
from tests.unit.test_webull_attach_report_seam import _adapter


def original_case(book, monkeypatch, *, attempts=1, local=True, filled=0, broker_quantity=None):
    assert book.provider == "webull"
    if book.clock.now(UTC).hour == 8:
        # The base fixture's minus-one-minute entry falls at 03:59 ET here.
        # Keep this new episode inside 04:00 without relaxing the session guard.
        entered = book.clock.now(UTC)
        with book.sessions.begin() as unit:
            unit.get(BrokerOrder, book.entry).submitted_at = entered
            unit.get(OmsManagedPosition, book.row).entry_time = entered
            for fill in unit.scalars(select(Fill).where(Fill.order_id == book.entry)):
                fill.filled_at = entered
        book.clock.value = (entered + timedelta(seconds=1)).isoformat()
        book.service._latest_quotes_by_symbol["ABC"]["received_at"] = book.clock.now(UTC)
    for module in (wb, proof_module, adapter_module, census_module):
        monkeypatch.setattr(module, "datetime", book.clock)
    state = SimpleNamespace(trace=[], pages=[], unreadable=False, omit=None, filled=filled,
        broker_quantity=Decimal(10 - filled) if broker_quantity is None else Decimal(broker_quantity),
        originals=[], pages_override=None, after_list=None, after_cancel=None, after_positions=None, working_sibling=False)
    with book.sessions.begin() as unit:
        entry = unit.get(BrokerOrder, book.entry)
        for number in range(attempts):
            base = f"controlled-original-protection-{number}"
            intent = TradeIntent(strategy_id=book.strategy, broker_account_id=book.account,
                symbol="ABC", side="sell", intent_type="close", quantity=Decimal(10),
                reason="WBATTACH1_PROTECTION", status="pending",
                payload={"metadata": {"entry_order_id": str(entry.id), "managed_row_id": str(book.row)}})
            unit.add(intent)
            unit.flush()
            attempt = {"base": base, "intent_id": str(intent.id), "phase": "unknown",
                       "quantity": "10", "cancel_requested": False}
            state.originals.append(attempt)
            for suffix in ("T", "S"):
                coid = wb.WebullBrokerAdapter._combo_leg_coid(base, suffix)
                canonical_coid = oco_exit_client_order_id(entry.client_order_id, "native-" + coid)
                assert canonical_coid == (
                    {"T": "own-parent-ocoexit-ction-0T", "S": "own-parent-ocoexit-ction-0S"},
                    {"T": "own-parent-ocoexit-ction-1T", "S": "own-parent-ocoexit-ction-1S"},
                )[number][suffix]
                assert coid != canonical_coid
                if local:
                    unit.add(BrokerOrder(strategy_id=book.strategy, broker_account_id=book.account,
                        intent_id=intent.id, client_order_id=coid, broker_order_id="native-" + coid,
                        symbol="ABC", side="sell", order_type="market", time_in_force="day",
                        quantity=Decimal(10), status="working", submitted_at=book.clock.now(UTC),
                        payload={"entry_order_id": str(entry.id), "managed_row_id": str(book.row),
                                 "wbattach1_child": "true"}))
        entry.payload = {**(entry.payload or {}), "fanout_leg": "webull", "native_oco_bracket": "false",
            "webull_protect_base_client_order_id": state.originals[-1]["base"],
            wb.KEY: {"entry_id": str(entry.id), "managed_id": str(book.row), "history_proven": True,
                "legacy_base": "", "exit_reason": "attach_ack_unknown", "attempts": state.originals}}

    adapter = _adapter()
    adapter.accounts_by_name[book.account_name] = adapter.accounts_by_name["live:orb"]
    ticks = [1000.]
    budget = QueryBudget(clock=lambda: ticks[0])
    reader = census_module.ExitCensusReader(budget, 2., clock=lambda: ticks[0])
    adapter._query_budget, adapter._today_reader = budget, SimpleNamespace(cycle_seconds=2.)
    monkeypatch.setattr(census_module, "shared_exit_census", lambda *args: reader)

    def rows():
        values = []
        for index, attempt in enumerate(state.originals):
            for suffix in ("T", "S"):
                if state.omit == (index, suffix):
                    continue
                coid = wb.WebullBrokerAdapter._combo_leg_coid(attempt["base"], suffix)
                amount = state.filled if index == 0 and suffix == "T" else 0
                item = {"symbol": "ABC", "side": "SELL", "order_status": "FILLED" if amount == 10 else "CANCELLED",
                        "qty": "10", "filled_qty": str(amount)}
                if suffix == "S" and state.working_sibling:
                    item["order_status"] = "WORKING"
                if amount:
                    item.update(filled_price="10", last_filled_time=book.clock.now(UTC).isoformat())
                values.append({"client_order_id": coid, "order_id": "native-" + coid,
                               "account_id": "ACC1", "items": [item]})
        return values
    state.rows = rows

    def page(account, cursor):
        state.pages.append(cursor)
        state.trace.append(("list", cursor))
        book.clock.value = (book.clock.now(UTC) + timedelta(microseconds=1)).isoformat()
        if state.unreadable:
            return 200, None
        if state.pages_override is not None:
            return state.pages_override(cursor)
        values = rows()
        if not cursor:
            return 200, {"orders": values[:1], "has_next": len(values) > 1}
        assert cursor == values[0]["client_order_id"]
        return 200, {"orders": values[1:], "has_next": False}
    monkeypatch.setattr(adapter, "_today_page", page)

    def no_detail(*args, **kwargs):
        pytest.fail("C6 original proof attempted DETAIL")
    monkeypatch.setattr(adapter, "_budgeted_detail", no_detail)
    monkeypatch.setattr(book.broker, "fetch_order_update", no_detail)

    async def cancel(**binding):
        base = binding["base_client_order_id"]
        assert base in {a["base"] for a in state.originals}
        state.trace.append(("cancel", base))
        if state.after_cancel is not None:
            state.after_cancel()
        return []

    async def list_proof(**binding):
        assert {a["base"] for a in binding["original_attempts"]} == {a["base"] for a in state.originals}
        book.clock.value = (book.clock.now(UTC) + timedelta(microseconds=1)).isoformat()
        result = await adapter.confirm_exit_pair_from_today_list(**binding)
        if state.after_list is not None:
            state.after_list()
        return result

    async def positions(account):
        assert account == book.account_name
        state.trace.append(("positions", state.broker_quantity))
        if state.after_positions is not None:
            state.after_positions()
        return [BrokerPositionSnapshot(broker_account_name=account, symbol="ABC",
            quantity=state.broker_quantity, average_price=Decimal(10))]

    submit = book.broker.submit_order
    async def sell(request):
        assert any(kind == "list" for kind, _ in state.trace)
        assert state.trace[-1][0] == "positions"
        hour = book.clock.now(UTC).hour
        assert request.order_type == ("market" if hour == 14 else "limit")
        assert request.metadata["session"] == ("NORMAL" if hour == 14 else "PM" if hour == 21 else "AM")
        if hour != 14:
            assert request.metadata["extended_hours"] == "true"
            assert 0 < Decimal(request.metadata["limit_price"]) <= Decimal("10.05")
        with book.sessions() as unit:
            sold = sum(unit.scalars(select(Fill.quantity).where(Fill.side == "sell")), Decimal(0))
            assert sold == state.filled  # The native fill ledger precedes this broker await.
            assert unit.get(OmsManagedPosition, book.row).current_quantity == 10 - state.filled
        state.trace.append(("sell", request.quantity))
        return await submit(request)

    monkeypatch.setattr(book.broker, "cancel_exit_pair", cancel, raising=False)
    monkeypatch.setattr(book.broker, "confirm_exit_pair_from_today_list", list_proof, raising=False)
    monkeypatch.setattr(book.broker, "list_account_positions", positions, raising=False)
    monkeypatch.setattr(book.broker, "positions_wire_read_age_seconds", lambda account: 0., raising=False)
    monkeypatch.setattr(book.broker, "submit_order", sell)
    state.advance = lambda: ticks.__setitem__(0, ticks[0] + 3.)
    return state


def waiting_incident(book):
    with book.sessions() as unit:
        incident = unit.scalar(select(SystemIncident).where(
            SystemIncident.payload["source"].as_string() == wb.KEY,
            SystemIncident.payload["entry_id"].as_string() == str(book.entry),
            SystemIncident.payload["account"].as_string() == book.account_name,
            SystemIncident.payload["symbol"].as_string() == "ABC"))
        assert incident is not None and incident.severity == "critical" and incident.status == "open"
        assert incident.title == "Exit waiting for broker proof: ABC"
        return incident.id


async def original_regression(book, monkeypatch, evidence):
    state = original_case(book, monkeypatch)
    if evidence.startswith("missing-"):
        state.omit = (0, evidence[-1])
    if evidence.startswith("unreadable-"):
        state.unreadable = True
    progress = await drive(book)
    assert ("cancel", state.originals[0]["base"]) in state.trace
    assert state.pages
    if evidence == "terminal":
        assert progress.phase == "EXIT_WORKING"
        assert len(book.submits) == 1 and book.submits[0].quantity == 10
        assert len(state.pages) == 2
        with book.sessions() as unit:
            assert unit.get(OmsManagedPosition, book.row).status == "closed"
            journal = unit.get(BrokerOrder, book.entry).payload[wb.KEY]
            assert all(attempt["phase"] == "released" and len(attempt["terminal_proof"]) == 2
                       for attempt in journal["attempts"])
    else:
        assert progress.phase == "BLOCKED_UNKNOWN" and not book.submits
        waiting_incident(book)
        with book.sessions() as unit:
            assert unit.get(OmsManagedPosition, book.row).current_quantity == 10
            assert unit.scalar(select(Fill.id).where(Fill.side == "sell")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
async def test_only_exact_journal_originals_not_symbol_matching_children(book, monkeypatch):
    state = original_case(book, monkeypatch)
    values = state.rows()
    values[0]["client_order_id"] = "unrelated-same-symbol-target"
    state.pages_override = lambda cursor: (200, {"orders": values, "has_next": False})
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    waiting_incident(book)


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("filled", [2, 10])
async def test_complete_paged_list_native_fills_conserved_before_any_sell(book, monkeypatch, filled):
    state = original_case(book, monkeypatch, filled=filled)
    canonical = book.service._apply_managed_position_after_fill
    transitions = []
    def observe_canonical(**binding):
        if binding["side"] == "sell" and binding["metadata"].get("killsw1_command"):
            row = binding["session"].get(OmsManagedPosition, book.row)
            before = row.current_quantity
            assert before == 10 - filled
            canonical(**binding)
            transitions.append((before, binding["quantity"], row.current_quantity, row.status))
        else:
            canonical(**binding)
    monkeypatch.setattr(book.service, "_apply_managed_position_after_fill", observe_canonical)
    await drive(book)
    assert len(state.pages) == 2
    assert [r.quantity for r in book.submits] == ([] if filled == 10 else [Decimal(10 - filled)])
    assert transitions == ([] if filled == 10 else [(8, Decimal(8), 0, "closed")])
    with book.sessions() as unit:
        assert sum(unit.scalars(select(Fill.quantity).where(Fill.side == "sell")), Decimal(0)) == 10
        managed = unit.get(OmsManagedPosition, book.row)
        assert managed.status == "closed" and managed.current_quantity == 0
        assert unit.scalar(select(VirtualPosition.quantity).where(
            VirtualPosition.strategy_id == book.strategy, VirtualPosition.broker_account_id == book.account,
            VirtualPosition.symbol == "ABC")) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("quantity", [0, 4, 100])
@pytest.mark.parametrize("hours", [8, 11, 14, 21])
async def test_fresh_broker_minimum_and_flat_closes_without_sell(book, monkeypatch, quantity, hours):
    original_case(book, monkeypatch, broker_quantity=quantity)
    await drive(book)
    assert [r.quantity for r in book.submits] == ([] if quantity == 0 else [Decimal(min(10, quantity))])
    if quantity == 0:
        with book.sessions() as unit:
            assert unit.get(OmsManagedPosition, book.row).status == "closed"
            assert unit.scalar(select(Fill.id).where(Fill.side == "sell")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
async def test_unreadable_first_proof_then_existing_retry_reuses_original_cancel(book, monkeypatch):
    state = original_case(book, monkeypatch)
    state.unreadable = True
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    identity = waiting_incident(book)
    state.unreadable = False
    state.advance()
    assert (await drive(book)).phase == "EXIT_WORKING"
    assert len(book.submits) == 1
    assert [kind for kind, _ in state.trace].count("cancel") == 1
    with book.sessions() as unit:
        assert unit.get(SystemIncident, identity).id == identity


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("damage", ["missing-intent", "submitting", "corrupt-journal"])
async def test_early_original_binding_errors_have_exact_visible_row_incident(book, monkeypatch, damage):
    state = original_case(book, monkeypatch)
    with book.sessions.begin() as unit:
        entry = unit.get(BrokerOrder, book.entry)
        journal = dict(entry.payload[wb.KEY])
        if damage == "missing-intent":
            journal["attempts"] = [{**journal["attempts"][0], "intent_id": str(uuid4())}]
        elif damage == "submitting":
            journal["attempts"] = [{**journal["attempts"][0], "phase": "submitting"}]
        else:
            journal = {}
        entry.payload = {**entry.payload, wb.KEY: journal}
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    assert not state.trace
    waiting_incident(book)


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("locally_present", [False, True])
async def test_all_original_attempts_including_locally_missing_rows_are_required(book, monkeypatch, locally_present):
    state = original_case(book, monkeypatch, attempts=2, local=locally_present)
    state.omit = (0, "T")
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    waiting_incident(book)
    state.omit = None
    state.advance()
    assert (await drive(book)).phase == "EXIT_WORKING"
    assert len(book.submits) == 1
    assert [kind for kind, _ in state.trace].count("cancel") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
async def test_cached_legacy_release_never_substitutes_for_fresh_original_census(book, monkeypatch):
    state = original_case(book, monkeypatch)
    book.service._exit_reservation_released.add((book.account_name, "ABC"))
    state.unreadable = True
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    waiting_incident(book)
    state.unreadable = False
    state.advance()
    assert (await drive(book)).phase == "EXIT_WORKING"
    assert len(book.submits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("boundary", ["first-cancel", "list"])
async def test_command_revoked_during_proof_cannot_cancel_next_attempt_or_sell(book, monkeypatch, boundary):
    state = original_case(book, monkeypatch, attempts=2)
    def revoke():
        with book.sessions.begin() as unit:
            unit.get(BotControlCommand, book.lease.command_id).lease_fence += 1
    if boundary == "first-cancel":
        state.after_cancel = revoke
    else:
        state.after_list = revoke
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    assert [kind for kind, _ in state.trace].count("cancel") == (1 if boundary == "first-cancel" else 2)
    waiting_incident(book)


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
async def test_ymat_shape_placed_unknown_cancelled_zero_fill_c6_proceeds_after_list(book, monkeypatch):
    state = original_case(book, monkeypatch, local=False)
    with book.sessions.begin() as unit:
        entry = unit.get(BrokerOrder, book.entry)
        journal = dict(entry.payload[wb.KEY])
        journal["attempts"] = [{**journal["attempts"][0], "cancel_requested": True}]
        entry.payload = {**entry.payload, wb.KEY: journal}
    state.trace.append(("placed-ack-unknown", state.originals[0]["base"]))
    assert (await drive(book)).phase == "EXIT_WORKING"
    assert len(book.submits) == 1 and book.submits[0].quantity == 10
    assert not any(kind == "cancel" for kind, _ in state.trace)
    assert next(i for i, event in enumerate(state.trace) if event[0] == "list") < next(
        i for i, event in enumerate(state.trace) if event[0] == "sell")


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("sibling", ["missing", "working"])
async def test_full_child_fill_closed_episode_still_recovers_original_sibling_without_sell(book, monkeypatch, sibling):
    state = original_case(book, monkeypatch, filled=10)
    state.omit = (0, "S") if sibling == "missing" else None
    state.working_sibling = sibling == "working"
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    waiting_incident(book)
    with book.sessions() as unit:
        assert unit.get(OmsManagedPosition, book.row).status == "closed"
        assert sum(unit.scalars(select(Fill.quantity).where(Fill.side == "sell")), Decimal(0)) == 10
    state.omit, state.working_sibling = None, False
    state.advance()
    assert (await drive(book)).phase == "EXIT_WORKING"
    assert not book.submits
    assert [kind for kind, _ in state.trace].count("cancel") == 1
    with book.sessions() as unit:
        assert unit.get(OmsManagedPosition, book.row).status == "closed"
        assert sum(unit.scalars(select(Fill.quantity).where(Fill.side == "sell")), Decimal(0)) == 10
        journal = unit.get(BrokerOrder, book.entry).payload[wb.KEY]
        assert all(a["phase"] == "released" for a in journal["attempts"])
        assert journal["exit_phase"] == "resolved_by_child_fill"
        assert unit.scalar(select(BotControlAttempt.status).where(
            BotControlAttempt.binding_kind == "managed_native_tree")) == "TERMINAL"


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
async def test_old_original_episode_never_releases_by_age(book, monkeypatch):
    state = original_case(book, monkeypatch)
    with book.sessions.begin() as unit:
        unit.get(BrokerOrder, book.entry).submitted_at -= timedelta(days=1)
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    assert not state.trace
    waiting_incident(book)


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
async def test_cw_target_still_waits_on_unknown_original_pair(book, monkeypatch):
    state = original_case(book, monkeypatch)
    async def handover(*args, **kwargs):
        return True
    monkeypatch.setattr(book.service, "_v2_eod_handover_ready", handover)
    with book.sessions() as unit:
        row = unit.get(OmsManagedPosition, book.row)
        position = book.service._hydrate_v2_position(row)
    assert await book.service._emit_v2_exit_on_loop_claimed(book.account_name, "ABC", position, 10.,
        kind="HARD", reference_price=10., reason="oms_v2_managed_exit:CW_TARGET", bid=10.,
        close_on_fill=True, expected_managed_row_id=str(book.row)) == "refused"
    assert not book.submits and not state.trace


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
async def test_inventory_changed_after_first_cancel_cannot_dispatch_next_original(book, monkeypatch):
    state = original_case(book, monkeypatch, attempts=2)
    def replace_original():
        with book.sessions.begin() as unit:
            entry = unit.get(BrokerOrder, book.entry)
            journal = dict(entry.payload[wb.KEY])
            journal["attempts"] = [{**a, "base": "different-original"} if index == 1 else a
                                   for index, a in enumerate(journal["attempts"])]
            entry.payload = {**entry.payload, wb.KEY: journal}
    state.after_cancel = replace_original
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    assert [kind for kind, _ in state.trace].count("cancel") == 1
    waiting_incident(book)


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("change", ["lease", "inventory"])
async def test_flat_close_transaction_rechecks_current_lease_and_original_inventory(book, monkeypatch, change):
    original_case(book, monkeypatch, broker_quantity=0)
    flat = proof_module.flat
    async def changed_before_flat(*args):
        with book.sessions.begin() as unit:
            if change == "lease":
                unit.get(BotControlCommand, book.lease.command_id).lease_fence += 1
            else:
                entry = unit.get(BrokerOrder, book.entry)
                journal = dict(entry.payload[wb.KEY])
                journal["attempts"] = [{**a, "base": "replaced-flat-original"} for a in journal["attempts"]]
                entry.payload = {**entry.payload, wb.KEY: journal}
        return await flat(*args)
    monkeypatch.setattr(proof_module, "flat", changed_before_flat)
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    with book.sessions() as unit:
        assert unit.get(OmsManagedPosition, book.row).status == "open"
    waiting_incident(book)


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
async def test_known_partial_fill_missing_sibling_retry_does_not_double_account(book, monkeypatch):
    state = original_case(book, monkeypatch, filled=2)
    state.omit = (0, "S")
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    with book.sessions() as unit:
        assert unit.get(OmsManagedPosition, book.row).current_quantity == 8
        assert sum(unit.scalars(select(Fill.quantity).where(Fill.side == "sell")), Decimal(0)) == 2
    state.omit = None
    state.advance()
    assert (await drive(book)).phase == "EXIT_WORKING"
    assert [request.quantity for request in book.submits] == [Decimal(8)]
    with book.sessions() as unit:
        assert sum(unit.scalars(select(Fill.quantity).where(Fill.side == "sell")), Decimal(0)) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
@pytest.mark.parametrize("foreign_sell", [False, True])
async def test_c6_census_resumes_more_than_two_pages_and_checks_later_working_sell(book, monkeypatch, foreign_sell):
    state = original_case(book, monkeypatch)
    originals = state.rows()
    filler = {"client_order_id": "page-two-buy", "order_id": "page-two-native", "account_id": "ACC1",
        "items": [{"symbol": "ABC", "side": "BUY", "order_status": "WORKING", "qty": "10", "filled_qty": "0"}]}
    def pages(cursor):
        if not cursor:
            return 200, {"orders": originals, "has_next": True}
        if cursor == originals[-1]["client_order_id"]:
            return 200, {"orders": [filler], "has_next": True}
        assert cursor == "page-two-buy"
        row = {**filler, "client_order_id": "later-page", "order_id": "later-native",
               "items": [{**filler["items"][0], "side": "SELL" if foreign_sell else "BUY"}]}
        return 200, {"orders": [row], "has_next": False}
    state.pages_override = pages
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    assert len(state.pages) == 2
    waiting_incident(book)
    state.advance()
    assert (await drive(book)).phase == ("BLOCKED_UNKNOWN" if foreign_sell else "EXIT_WORKING")
    assert len(state.pages) == 3
    assert len(book.submits) == (0 if foreign_sell else 1)
    assert [kind for kind, _ in state.trace].count("cancel") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
async def test_c6_and_cw_stop_share_managed_wire_claim_during_original_list_await(book, monkeypatch):
    state = original_case(book, monkeypatch)
    with book.sessions() as unit:
        position = book.service._hydrate_v2_position(unit.get(OmsManagedPosition, book.row))
    entered, release = asyncio.Event(), asyncio.Event()
    read = book.broker.confirm_exit_pair_from_today_list
    async def paused(**binding):
        entered.set()
        await release.wait()
        return await read(**binding)
    monkeypatch.setattr(book.broker, "confirm_exit_pair_from_today_list", paused)
    pending = asyncio.create_task(drive(book))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await book.service._emit_v2_exit_on_loop_claimed(book.account_name, "ABC", position, 10.,
            kind="HARD", reference_price=8., reason="oms_v2_managed_exit:CW_HARD_STOP", bid=8.,
            close_on_fill=True, expected_managed_row_id=str(book.row)) == "refused"
        assert not book.submits
    finally:
        release.set()
        progress = await pending
    assert progress.phase == "EXIT_WORKING" and len(book.submits) == 1
    assert [kind for kind, _ in state.trace].count("cancel") == 1
    assert book.service._control_managed_exit_claims == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
async def test_lease_changed_during_fresh_position_await_is_refused_before_sell(book, monkeypatch):
    state = original_case(book, monkeypatch)
    def revoke():
        with book.sessions.begin() as unit:
            unit.get(BotControlCommand, book.lease.command_id).lease_fence += 1
    state.after_positions = revoke
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    assert any(kind == "positions" for kind, _ in state.trace)
    waiting_incident(book)


@pytest.mark.asyncio
@pytest.mark.parametrize("book", ["webull"], indirect=True)
async def test_terminal_c6_audit_still_requires_new_list_after_position_failure(book, monkeypatch):
    state = original_case(book, monkeypatch)
    def unreadable_position():
        raise OSError("controlled complete positions reader unavailable")
    state.after_positions = unreadable_position
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    waiting_incident(book)
    with book.sessions() as unit:
        audit = unit.scalar(select(BotControlAttempt).where(BotControlAttempt.binding_kind == "managed_native_tree"))
        assert audit.status == "TERMINAL"
        identity = audit.attempt_id
    state.after_positions = None
    state.unreadable = True
    state.advance()
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN" and not book.submits
    state.unreadable = False
    state.advance()
    assert (await drive(book)).phase == "EXIT_WORKING" and len(book.submits) == 1
    assert [kind for kind, _ in state.trace].count("cancel") == 1
    with book.sessions() as unit:
        audits = unit.scalars(select(BotControlAttempt).where(BotControlAttempt.binding_kind == "managed_native_tree")).all()
        assert len(audits) == 1 and audits[0].attempt_id == identity
