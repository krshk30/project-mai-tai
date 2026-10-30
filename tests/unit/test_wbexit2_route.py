"""UNRUN H2 source controls at the SURVIVING WBATTACH broker fixture seam.

No removed helper/module/flag is restored. Inputs marked CONTROLLED are not
historical typed native receipts. Main composes the external hook proposal.
"""
import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from threading import Event

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExitPairReleaseResult
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter, WebullPositionsUnavailable
from project_mai_tai.db.models import AccountPosition, BrokerOrder, Fill, OmsManagedPosition, SystemIncident, TradeIntent, VirtualPosition
from project_mai_tai.oms import service as oms, wbexit2
from project_mai_tai.oms import webull_attach_riskoff as wb
from project_mai_tai.settings import Settings
from tests.unit.test_wbattach1_riskoff import Broker, make_case as make_case, closes, restarted


class ControlledOwnerBroker(Broker):
    """Only external broker replies change; actual DB/owner/STOP decisions stay."""
    def __init__(self, *args):
        super().__init__(*args)
        self.symbol, self.owned, self.entry_price = "ABC", Decimal(10), Decimal(10)
        self.bid, self.lag, self.unknown_ack = 9.4, False, False
        self.working_reads = self.positions_reads = 0
        self.positions_unknown = False
        self.working_block, self.working_entered = None, None
        self.wire_started = monotonic() - 3.  # CONTROLLED stale ordinary cache; never new API authority.
        self.native_account_id = self.positions_account_id = "CONTROLLED-native-account"

    def report(self, *args, **kwargs):
        report = replace(super().report(*args, **kwargs), symbol=self.symbol,
                         quantity=self.owned, reported_at=oms.utcnow())
        if report.filled_quantity > 0:
            for pair in self.requests:
                if pair.metadata.get("webull_exit_only_pair") != "true":
                    continue
                for suffix, key in (("T", "bracket_target_price"), ("S", "bracket_stop_price")):
                    if report.client_order_id == WebullBrokerAdapter._combo_leg_coid(pair.client_order_id, suffix):
                        return replace(report, fill_price=WebullBrokerAdapter._round_to_tick(Decimal(pair.metadata[key])))
        return report

    async def fetch_quotes(self, symbols):
        return {symbol: {"bid_price": self.bid, "ask_price": self.bid + .01, "last_price": self.bid}
                for symbol in symbols}

    async def fetch_oco_exit_fill(self, account, symbol, base):
        assert (account, symbol) == (self.account, self.symbol)
        return None  # CONTROLLED: no original child execution observed.

    async def list_account_positions(self, account):
        return [replace(row, symbol=self.symbol) for row in await super().list_account_positions(account)]

    def positions_wire_read_age_seconds(self, account):
        return monotonic() - self.wire_started

    async def read_wbexit2_positions(self, account, *, after):
        started = monotonic()
        assert after < started
        self.positions_reads += 1
        if self.positions_unknown:
            raise WebullPositionsUnavailable("CONTROLLED unreadable complete census")
        rows = await self.list_account_positions(account)
        self.wire_started = started
        return rows, started, self.positions_account_id

    async def release_exit_pair_for_close(self, **binding):
        # Surviving Broker lacks this external API; provide its actual controlled
        # cancel/read surface for H2 OFF instead of letting AttributeError stand
        # in for the historical refusal. Native chronology remains UNKNOWN.
        await self.cancel_exit_pair(**binding)
        return await self.confirm_exit_pair_terminal(**binding)

    async def submit_order(self, request):
        if request.metadata.get("webull_exit_only_pair") != "true":
            assert request.quantity <= self.total - Decimal(1000), "manual inventory cannot size OUR sell"
            if self.unknown_ack:
                self.requests.append(request)
                raise TimeoutError("CONTROLLED wire may have accepted")
        return [replace(r, symbol=self.symbol, reported_at=oms.utcnow(),
                        fill_price=Decimal(str(self.bid)) if r.filled_quantity > 0 else r.fill_price)
                for r in await super().submit_order(request)]

    async def fetch_order_update(self, request):
        our_close = request.side == "sell" and any(
                r.client_order_id == request.client_order_id and r.metadata.get("webull_exit_only_pair") != "true"
                for r in self.requests)
        if self.unknown_ack and our_close:
            return None
        result = await super().fetch_order_update(request)
        if result is None:
            return None
        if request.side == "buy":
            return replace(result, symbol=self.symbol, quantity=self.owned, filled_quantity=self.owned,
                           fill_price=self.entry_price, reported_at=oms.utcnow())
        return replace(result, symbol=self.symbol, reported_at=oms.utcnow(),
                       fill_price=Decimal(str(self.bid)) if our_close and result.filled_quantity > 0 else result.fill_price)

    async def cancel_exit_pair(self, **binding):
        if wbexit2.enabled(self.service):
            state = saved((self.service, self.factory, self, None, None))
            assert state["invocation"] and state["sell"] is None
            assert binding["base_client_order_id"] in state["cancel_bases"]
            assert state["originals_proven"] is True
        self.last_cancel_at = monotonic()
        return await super().cancel_exit_pair(**binding)

    async def confirm_exit_pair_terminal(self, **binding):
        base = binding["base_client_order_id"]
        pairs = [r for r in self.requests if r.client_order_id == base
                 and r.metadata.get("webull_exit_only_pair") == "true"]
        assert len(pairs) == 1
        pair = pairs[0]
        working = base not in self.cancelled or self.lag
        if base not in self.cancelled:
            self.working_reads += 1
            if self.working_entered is not None:
                self.working_entered.set()
                await self.working_block.wait()
        if not working and not self.child_applied:
            self.total -= self.child_filled
            self.child_applied = True
        reports = []
        for suffix, key, native in (("T", "bracket_target_price", "limit_price"),
                                    ("S", "bracket_stop_price", "stop_price")):
            report = self.report(WebullBrokerAdapter._combo_leg_coid(base, suffix),
                event="accepted" if working else "cancelled", filled=Decimal(0) if working or suffix == "S" else self.child_filled)
            metadata = dict(report.metadata)
            if working:
                metadata["wbexit2_geometry"] = json.dumps({native: str(WebullBrokerAdapter._round_to_tick(
                    Decimal(pair.metadata[key]))), "status": "SUBMITTED"})
            reports.append(replace(report, metadata=metadata))
        return ExitPairReleaseResult(outcome="reserved" if working else "released", reports=tuple(reports))

    async def confirm_exit_pair_from_today_list(self, **binding):
        result = await super().confirm_exit_pair_from_today_list(**binding)
        reports = []
        for report in result.reports:
            proof = json.loads(report.metadata["wbattach1_list_census"])
            proof["account_id"] = self.native_account_id  # CONTROLLED physical request scope.
            reports.append(replace(report, metadata={**report.metadata, "wbattach1_list_census": json.dumps(proof)}))
        return replace(result, reports=tuple(reports))


async def owner_case(make_case, *, switch=True, symbol="ABC", quantity="10", entry="10",
                     port_type=ControlledOwnerBroker, target_pct=None, stop_pct=None, at_clock=None):
    case = make_case()
    service, factory, old, entry_id, clock = case
    if at_clock is not None:
        clock[0] = at_clock  # Controlled fixture timeline BEFORE journal/physical pair creation.
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_wbexit2_enabled", switch)
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_keepwatch1_enabled", True)
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_brokerdecides1_enabled", True)
    service.__dict__.pop("_v2_eod_handover_ready", None)
    service.__dict__.pop("_fetch_quote_for_order", None)
    if target_pct is not None:
        object.__setattr__(service.settings, "oms_v2_cw_target_pct", target_pct)
    if stop_pct is not None:
        object.__setattr__(service.settings, "oms_v2_cw_hard_stop_pct", stop_pct)
    broker = port_type(service, factory, old.account)
    broker.symbol, broker.owned, broker.entry_price = symbol, Decimal(quantity), Decimal(entry)
    broker.bid, broker.total = float(entry), Decimal(1000) + broker.owned
    service.broker_adapter = broker
    with factory.begin() as session:
        order = session.get(BrokerOrder, entry_id)
        order.symbol, order.quantity = symbol, broker.owned
        if at_clock is not None:
            order.created_at = order.submitted_at = at_clock
        for row in session.scalars(select(TradeIntent)):
            row.symbol, row.quantity = symbol, broker.owned
            if at_clock is not None:
                row.created_at = at_clock
        for row in session.scalars(select(Fill)):
            row.symbol, row.quantity, row.price = symbol, broker.owned, broker.entry_price
            if at_clock is not None:
                row.filled_at = at_clock
        for row in session.scalars(select(VirtualPosition)):
            row.symbol, row.quantity, row.average_price = symbol, broker.owned, broker.entry_price
        for row in session.scalars(select(AccountPosition)):
            row.symbol, row.quantity, row.average_price = symbol, broker.total, broker.entry_price
            if at_clock is not None:
                row.source_updated_at = at_clock
        row = session.scalar(select(OmsManagedPosition))
        row.symbol, row.entry_price = symbol, broker.entry_price
        row.original_quantity = row.current_quantity = int(broker.owned)
        if at_clock is not None:
            row.entry_time = at_clock
    case = service, factory, broker, entry_id, clock
    if switch:
        boot = await wbexit2.hydrate_at_boot(service)  # SAME Main production boot hook, DB-only.
        assert boot["status"] == "INVENTORY_READY" and boot["owners"] == []
    assert await service._attach_webull_protection(broker_account_name=broker.account, symbol=symbol,
        quantity=int(broker.owned), entry_price=float(broker.entry_price), strategy_code="schwab_1m_v2",
        entry_client_order_id="owned-entry")
    assert not closes(broker)
    broker.bid = float(broker.entry_price * Decimal(".94"))
    return case


async def close_case(case, tag="CONFIRMATION_EXIT"):
    service, factory, broker, _, clock = case
    with factory() as session:
        row_id = str(session.scalar(select(OmsManagedPosition)).id)
    decision = oms._ConfirmationFanoutDecision(symbol=broker.symbol, source_fill_id="CONTROLLED:" + tag,
        accounts=(broker.account,), exit_tag=tag)
    outcome = await service._webull_cancel_then_sell(broker.account, broker.symbol, exit_tag=tag,
        reason="oms_v2_managed_exit:" + tag, kind="HARD", reference_bid=broker.bid,
        expected_row_id=row_id, expires_at=clock[0] + timedelta(seconds=60), decision=decision)
    tasks = tuple(service._confirmation_exit_recovery_tasks)
    if tasks:
        # Same existing recovery-task conservation boundary used by the old
        # fixture. Do not dispose its DB while an original OFF actor still runs.
        done, pending = await asyncio.wait(tasks, timeout=11)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert not pending, "original controlled recovery failed to join in its existing fixture bound"
        for task in done:
            task.result()
    return outcome


def saved(case):
    with case[1]() as session:
        rows = session.scalars(select(SystemIncident).where(SystemIncident.payload["source"].as_string() == "wbexit2")).all()
        assert len(rows) == 1
        return dict(rows[0].payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,quantity,entry", [("HTCO", "175", "1.6976"), ("AIOK", "27", "10.91")])
@pytest.mark.parametrize("switch", [False, True], ids=["H2-OFF", "WBEXIT2-ON"])
async def test_log_operand_context_terminal_lag_then_resume_original(make_case, record_property, symbol, quantity, entry, switch):
    # Numeric operands above come from retained OMS MANAGED-OPEN lines only.
    # DB/native children/quotes/terminal chronology here remain CONTROLLED.
    case = await owner_case(make_case, switch=switch, symbol=symbol, quantity=quantity, entry=entry)
    case[2].lag = True
    first_outcome = await close_case(case)
    if not switch:
        # Actual H2 OFF behavior is a characterization, not a manufactured
        # historical failure. Main still needs SAME-input baseline parity.
        with case[1]() as session:
            assert session.scalar(select(SystemIncident).where(SystemIncident.payload["source"].as_string() == "wbexit2")) is None
        record_property("controlled_H2_OFF_outcome", first_outcome)
        record_property("native_historical_counterfactual", "UNKNOWN_NO_TYPED_NATIVE_PREIMAGE")
        return
    assert not closes(case[2])
    original = saved(case) if switch else None
    if switch:
        assert original["invocation"] is None and original["scan_exception"] is True
        assert original["original_attempt_outcome"] == "FAIL" and original["sell"] is None
    case[4][0] += timedelta(seconds=12)  # MODELED clock, not physical native elapsed-time proof.
    case[2].lag = False
    await close_case(case)
    if switch:
        state = saved(case)
        assert len(closes(case[2])) == 1 and closes(case[2])[0].quantity == Decimal(quantity)
        assert case[2].total == 1000 and state["phase"] == "RESOLVED"
        assert state["owner_id"] == original["owner_id"] and state["originals"] == original["originals"]
        assert state["first_cancel_at"] == original["first_cancel_at"] and state["original_attempt_outcome"] == "FAIL"
        assert len(case[2].cancelled) == 1 and case[2].working_reads == 1
    else:
        with case[1]() as session:
            assert session.scalar(select(SystemIncident).where(SystemIncident.payload["source"].as_string() == "wbexit2")) is None
    record_property("native_historical_counterfactual", "UNKNOWN_NO_TYPED_NATIVE_PREIMAGE")


@pytest.mark.asyncio
async def test_joined_checkpoint_restart_uses_original_terminal_NOT_new_working(make_case):
    case = await owner_case(make_case)
    case[2].lag = True
    await close_case(case)
    first = saved(case)
    assert first["invocation"] is None
    case = restarted(case)  # Existing reconstructed service, NOT abrupt process-kill proof.
    case[2].lag = False
    await wbexit2.hydrate_at_boot(case[0])
    await close_case(case)
    assert len(closes(case[2])) == 1 and len(case[2].cancelled) == 1
    assert case[2].working_reads == 1 and saved(case)["originals"] == first["originals"]


@pytest.mark.asyncio
async def test_unknown_ACK_reserves_OUR_size_no_second_sell_or_stacked_pair(make_case):
    case = await owner_case(make_case)
    case[2].unknown_ack = True
    await close_case(case, "CW_HARD_STOP")
    original = saved(case)
    assert original["sell"]["ack"] == "UNKNOWN"
    await wbexit2.refresh_pending(case[0])  # Actual record-only path has no invocation deadline/task lease.
    assert saved(case)["sell"] == original["sell"]
    await close_case(case, "CW_HARD_STOP")
    assert len(closes(case[2])) == 1 and saved(case)["sell"] == original["sell"]
    assert len([r for r in case[2].requests if r.metadata.get("webull_exit_only_pair") == "true"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["CW_FLIP", "CONFIRMATION_EXIT", "CW_TARGET", "CW_HARD_STOP"])
async def test_each_RTH_route_one_owned_sell_after_original_terminal(make_case, tag):
    case = await owner_case(make_case)
    if tag == "CW_TARGET":
        case[2].bid = 12.
    await close_case(case, tag)
    assert len(closes(case[2])) == 1 and closes(case[2])[0].quantity == 10
    assert len(case[2].cancelled) == 1 and saved(case)["sell"] is not None


@pytest.mark.asyncio
async def test_terminal_but_fresh_positions_UNKNOWN_retains_owner_no_wire(make_case):
    case = await owner_case(make_case)
    case[2].positions_unknown = True
    await close_case(case)
    state = saved(case)
    assert state["sell"] is None and Decimal(state["remaining"]) == 10 and state["scan_exception"] is True
    assert not closes(case[2]) and len(case[2].cancelled) == 1


class AlreadyTerminalOwnerBroker(ControlledOwnerBroker):
    """Controlled external terminal receipt BEFORE our first cancel; no history claim."""
    async def confirm_exit_pair_from_today_list(self, **binding):
        result = await super().confirm_exit_pair_from_today_list(**binding)
        return replace(result, outcome="released", reports=tuple(replace(r,
            event_type="cancelled", metadata={k: v for k, v in r.metadata.items()
                                             if k != "wbexit2_geometry"}) for r in result.reports))


@pytest.mark.asyncio
async def test_positive_original_already_terminal_needs_no_new_WORKING_or_cancel(make_case):
    case = await owner_case(make_case, port_type=AlreadyTerminalOwnerBroker)
    await close_case(case)
    assert not case[2].cancelled
    assert [r.quantity for r in closes(case[2])] == [Decimal(10)]
    state = saved(case)
    assert state["phase"] == "RESOLVED" and state["terminal_receipts"]
    assert state["first_cancel_at"] is None
    assert all(c["price"] is None for p in state["originals"] for c in p["children"])


class UnenteredCancelRecheckBroker(ControlledOwnerBroker):
    recheck = "same"

    async def confirm_exit_pair_from_today_list(self, **binding):
        result = await super().confirm_exit_pair_from_today_list(**binding)
        if self.working_reads < 2 or self.cancelled:
            return result
        if self.recheck == "unknown":
            return replace(result, outcome="unanswerable", reports=())
        if self.recheck == "missing_child":
            return replace(result, reports=(result.reports[0], None))
        if self.recheck == "terminal":
            return replace(result, outcome="released", reports=tuple(replace(r,
                event_type="cancelled", metadata={k: v for k, v in r.metadata.items()
                                                 if k != "wbexit2_geometry"}) for r in result.reports))
        reports = []
        for report in result.reports:
            if self.recheck == "native_drift":
                report = replace(report, broker_order_id="REPLACEMENT-" + report.broker_order_id)
            elif self.recheck == "geometry_drift":
                geometry = json.loads(report.metadata["wbexit2_geometry"])
                for field in ("limit_price", "stop_price"):
                    if field in geometry:
                        geometry[field] = str(Decimal(geometry[field]) + Decimal("0.01"))
                report = replace(report, metadata={**report.metadata, "wbexit2_geometry": json.dumps(geometry)})
            elif self.recheck == "stale":
                census = json.loads(report.metadata["wbattach1_list_census"])
                census["started_at"] = census["acquired_at"] = (oms.utcnow() - timedelta(seconds=3)).isoformat()
                report = replace(report, metadata={**report.metadata, "wbattach1_list_census": json.dumps(census)})
            elif self.recheck in {"incomplete", "physical_account_drift"}:
                census = json.loads(report.metadata["wbattach1_list_census"])
                if self.recheck == "incomplete":
                    census["complete"] = False
                else:
                    census["account_id"] = "CONTROLLED-NOT-ORIGINAL-native-account"
                report = replace(report, metadata={**report.metadata, "wbattach1_list_census": json.dumps(census)})
            reports.append(report)
        return replace(result, reports=tuple(reports))


@pytest.mark.asyncio
@pytest.mark.parametrize("recheck", ["same", "terminal", "native_drift", "geometry_drift", "unknown", "stale",
                                    "missing_child", "incomplete", "physical_account_drift", "aged_after_read"])
async def test_joined_unentered_cancel_resumes_ONLY_fresh_exact_original_tree(make_case, monkeypatch, recheck):
    case = await owner_case(make_case, port_type=UnenteredCancelRecheckBroker)
    service, _, broker, _, _ = case
    actual_checkpoint = wbexit2.checkpoint
    injected = False

    async def stop_before_adapter(service, call, operation, **evidence):
        nonlocal injected
        result = await actual_checkpoint(service, call, operation, **evidence)
        if operation == "cancel" and not injected:
            injected = True
            raise ValueError("CONTROLLED_cancel_committed_adapter_never_entered")
        if operation == "cancel" and injected and broker.recheck == "aged_after_read":
            # NEGATIVE clock input only: model time consumed by the joined DB
            # boundary AFTER the fresh census. No positive physical timing
            # claim, no reset/backdating of the native receipt or deadline.
            case[4][0] += timedelta(seconds=3)
        return result

    monkeypatch.setattr(wbexit2, "checkpoint", stop_before_adapter)
    await close_case(case)
    first = saved(case)
    assert injected and not broker.cancelled and not closes(broker)
    assert first["originals_proven"] is True and first["invocation"] is None
    assert first["cancel_bases"] == []
    assert first["last_unentered_cancel"]["source"] == "actual_invocation_control_flow"
    assert first["original_attempt_outcome"] == "FAIL" and broker.working_reads == 1
    broker.recheck = recheck  # Only the next controlled native reply varies.
    await close_case(case)
    final = saved(case)
    assert broker.working_reads == 2, "must physically re-read SAME originals before first cancel"
    assert final["originals"] == first["originals"] and final["owner_id"] == first["owner_id"]
    assert final["first_cancel_at"] == first["first_cancel_at"]
    assert final["original_attempt_outcome"] == "FAIL"
    if recheck in {"same", "terminal"}:
        assert len(broker.cancelled) == (1 if recheck == "same" else 0)
        assert [r.quantity for r in closes(broker)] == [Decimal(10)]
        assert final["phase"] == "RESOLVED" and broker.total == 1000
        assert broker.positions_reads == 1
    else:
        assert not broker.cancelled and not closes(broker) and final["sell"] is None
        assert final["invocation"] is None and final["scan_exception"] is True
        assert final["cancel_bases"] == []
        # No adapter cancel or sell was issued: don't infer a currently working
        # native book from an unreadable receipt; our original pair is untouched.
        assert len([r for r in broker.requests if r.metadata.get("webull_exit_only_pair") == "true"]) == 1
        assert broker.positions_reads == 0


@pytest.mark.asyncio
async def test_original_child_partial_then_sell_only_OUR_exact_remaining_seven(make_case):
    case = await owner_case(make_case)
    case[2].child_filled = Decimal(3)
    await close_case(case)
    assert [r.quantity for r in closes(case[2])] == [Decimal(7)]
    assert case[2].total == 1000
    state = saved(case)
    assert sum(map(Decimal, state["child_fill_highwater"].values())) == 3
    assert Decimal(state["sell"]["filled_highwater"]) == 7 and Decimal(state["remaining"]) == 0
    with case[1]() as session:
        assert sum((f.quantity for f in session.scalars(select(Fill).where(Fill.side == "sell"))), Decimal(0)) == 10


@pytest.mark.asyncio
async def test_flag_OFF_during_original_read_refuses_before_FIRST_cancel(make_case):
    case = await owner_case(make_case)
    broker = case[2]
    broker.working_entered, broker.working_block = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(close_case(case))
    try:
        async with asyncio.timeout(2):
            await broker.working_entered.wait()
        object.__setattr__(case[0].settings, "strategy_schwab_1m_v2_wbexit2_enabled", False)
        broker.working_block.set()
        await task
        assert not broker.cancelled and not closes(broker)
        assert saved(case)["sell"] is None and saved(case)["invocation"] is None
    finally:
        broker.working_block.set()
        await asyncio.gather(task, return_exceptions=True)  # Physical ownership, no detached fixture IO.


@pytest.mark.asyncio
async def test_same_alias_different_physical_positions_account_never_sizes_sell(make_case):
    case = await owner_case(make_case)
    case[2].positions_account_id = "CONTROLLED-FOREIGN-native-account"
    await close_case(case)
    assert not closes(case[2]) and len(case[2].cancelled) == 1
    assert saved(case)["native_account_id"] == "CONTROLLED-native-account"
    assert saved(case)["sell"] is None and saved(case)["scan_exception"] is True


@pytest.mark.asyncio
async def test_first_LIST_UNKNOWN_still_commits_close_responsibility_before_any_cancel(make_case):
    class FirstUnknown(ControlledOwnerBroker):
        missing = True
        async def confirm_exit_pair_from_today_list(self, **binding):
            if self.missing:
                return ExitPairReleaseResult(outcome="unanswerable", reports=())
            return await super().confirm_exit_pair_from_today_list(**binding)
    case = await owner_case(make_case, port_type=FirstUnknown)
    await close_case(case, "CW_FLIP")
    original = saved(case)
    assert not case[2].cancelled and not closes(case[2])
    assert original["invocation"] is None and original["originals_proven"] is False
    assert original["scan_exception"] is True and original["decision"]["reference_price"]
    assert all(c["native_id"] is None for pair in original["originals"] for c in pair["children"])
    case[2].missing = False
    await close_case(case, "CW_FLIP")
    assert saved(case)["owner_id"] == original["owner_id"] and len(closes(case[2])) == 1


@pytest.mark.asyncio
async def test_native_child_ID_drift_after_cancel_keeps_owner_no_wire(make_case):
    class Drift(ControlledOwnerBroker):
        async def confirm_exit_pair_from_today_list(self, **binding):
            result = await super().confirm_exit_pair_from_today_list(**binding)
            if self.cancelled:
                return replace(result, reports=tuple(replace(r, broker_order_id="FOREIGN-" + r.broker_order_id)
                                                     for r in result.reports))
            return result
    case = await owner_case(make_case, port_type=Drift)
    await close_case(case)
    assert not closes(case[2]) and saved(case)["scan_exception"] is True
    assert Decimal(saved(case)["remaining"]) == 10 and saved(case)["invocation"] is None


@pytest.mark.asyncio
async def test_orphan_persisted_nonce_is_named_HOLD_not_restart_permission(make_case):
    case = await owner_case(make_case)
    case[2].lag = True
    await close_case(case)
    old = saved(case)
    with case[1].begin() as session:
        wbexit2.transition(session, old["identity"], old["generation"], operation="acquire",
            now=case[4][0], invocation="CONTROLLED-prior-unjoined")
    before = len(case[2].requests), len(case[2].cancelled)
    case = restarted(case)
    case[2].lag = False
    await wbexit2.hydrate_at_boot(case[0])
    await close_case(case)
    assert before == (len(case[2].requests), len(case[2].cancelled))
    assert saved(case)["invocation"] == "CONTROLLED-prior-unjoined"
    assert saved(case)["owner_id"] == old["owner_id"] and saved(case)["scan_exception"] is True


@pytest.mark.asyncio
async def test_working_target_ACK_is_not_permission_for_later_second_stop_sell(make_case):
    case = await owner_case(make_case)
    case[2].bid, case[2].close_pending = 12., True
    await close_case(case, "CW_TARGET")
    old = saved(case)
    assert old["phase"] == "SELL_WORKING" and old["sell"]["ack"] == "accepted"
    assert len(closes(case[2])) == 1
    await close_case(case, "CW_HARD_STOP")
    assert len(closes(case[2])) == 1 and saved(case)["sell"] == old["sell"]
    # A resting target is NOT stop protection: responsibility/PAGE remains.


@pytest.mark.asyncio
async def test_full_original_child_fill_resolves_from_ledger_no_extra_SELL(make_case):
    case = await owner_case(make_case)
    case[2].child_filled = Decimal(10)
    await close_case(case)
    assert not closes(case[2]) and case[2].total == 1000
    assert saved(case)["phase"] == "RESOLVED" and Decimal(saved(case)["remaining"]) == 0


@pytest.mark.asyncio
async def test_held_physical_claim_join_keeps_original_FAIL_then_new_invocation_resumes(make_case, monkeypatch, record_property):
    case = await owner_case(make_case)
    service, _, broker, _, _ = case
    original = service._run_db
    entered, release, finished = Event(), Event(), Event()
    handles = []
    async def held_run_db(fn, *, commit=True):
        captured = dict(zip(getattr(getattr(fn, "__code__", None), "co_freevars", ()),
                            (cell.cell_contents for cell in getattr(fn, "__closure__", None) or ())))
        if getattr(captured.get("mutate"), "__name__", "") != "claim" or entered.is_set():
            return await original(fn, commit=commit)
        # Existing fixture's external DB latency seam ONLY. The actual function,
        # actual worker/session/commit and real current-task guards still run.
        handles.append(asyncio.get_running_loop().call_later(max(0., broker.last_cancel_at + 9.25 - monotonic()), release.set))
        def held(session):
            result = fn(session)
            entered.set()
            assert release.wait(12), "CONTROLLED commit failed to release"
            return result
        result = await original(held, commit=commit)
        finished.set()
        return result
    monkeypatch.setattr(service, "_run_db", held_run_db)
    try:
        await close_case(case, "CW_FLIP")
        assert entered.is_set() and finished.is_set(), "physical DB must JOIN before nonce/registry release"
        first = saved(case)
        assert not closes(broker) and first["invocation"] is None
        assert first["original_attempt_outcome"] == "FAIL" and first["scan_exception"] is True
        assert not service.__dict__.get("_wbattach_exit_owners")
        await close_case(case, "CW_FLIP")
        last = saved(case)
        assert len(closes(broker)) == 1 and len(broker.cancelled) == 1
        assert last["phase"] == "RESOLVED" and last["originals"] == first["originals"]
        assert last["first_cancel_at"] == first["first_cancel_at"] and last["original_attempt_outcome"] == "FAIL"
        record_property("first_attempt", "FAIL_NOT_RETROACTIVE_TIMING_PASS")
    finally:
        release.set()
        for handle in handles:
            handle.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("time_utc", [datetime(2026, 10, 26, 11, 53, tzinfo=UTC), datetime(2026, 10, 26, 21, 0, tzinfo=UTC)])
async def test_PM_AH_not_selected_no_native_0753_coverage_claim(make_case, time_utc, record_property):
    case = make_case()
    object.__setattr__(case[0].settings, "strategy_schwab_1m_v2_wbexit2_enabled", True)
    case[4][0] = time_utc
    assert not wbexit2.selected(case[0], "oms_v2_managed_exit:CONFIRMATION_EXIT", case[2].account)
    record_property("SPRC_0753_actual_PM_chronology", "NOT_COVERED_NORMAL_PM_SELECTORS_STILL_REQUIRED")


def test_Schwab_not_selected_and_default_false(make_case):
    case = make_case()
    assert Settings.model_fields["strategy_schwab_1m_v2_wbexit2_enabled"].default is False
    object.__setattr__(case[0].settings, "strategy_schwab_1m_v2_wbexit2_enabled", True)
    assert not wbexit2.selected(case[0], "oms_v2_managed_exit:CONFIRMATION_EXIT", "live:schwab_1m_v2")
