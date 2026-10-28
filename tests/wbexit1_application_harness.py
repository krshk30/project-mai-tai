"""Controlled external I/O for the real OMS exit owner (no permission mocks)."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from time import monotonic

from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, ExitPairReleaseResult
from project_mai_tai.broker_adapters.webull import WebullPositionsUnavailable
from project_mai_tai.db.models import (
    AccountPosition, BrokerOrder, Fill, OmsManagedPosition, SystemIncident, TradeIntent, VirtualPosition,
)
from project_mai_tai.oms import service as oms
from tests.unit.test_wbattach1_riskoff import Broker, attach, closes
from tests.wbexit1_retained import load_episode

CANONICAL_EXTENDED_SESSION = oms._extended_hours_session


class ExitPort(Broker):
    """The fixture's broker only; all journals/claims/emission remain application code."""

    def __init__(self, service, factory, account):
        super().__init__(service, factory, account)
        self.wire_started = monotonic() - 3
        self.faults = []
        self.observations = []
        self.reject_close = False
        self.unknown_close = False
        self.symbol, self.owned = "ABC", Decimal(10)
        self.quote_bid, self.reference_bid = 10., 9.4
        self.unknown_releases, self.confirmed_children = 0, 0
        self.cached_quantity = None
        self.manual_quantity = Decimal(1000)

    def report(self, *args, **kwargs):
        return replace(super().report(*args, **kwargs), symbol=self.symbol, quantity=self.owned,
                       reported_at=oms.utcnow())

    async def list_account_positions(self, account):
        rows = await super().list_account_positions(account)
        return [replace(row, symbol=self.symbol, quantity=self.cached_quantity
                        if self.cached_quantity is not None else row.quantity) for row in rows]

    async def fetch_order_update(self, request):
        if self.unknown_close and any(previous.client_order_id == request.client_order_id
                and previous.metadata.get("webull_exit_only_pair") != "true"
                for previous in self.requests):
            return None  # A lost ACK must not become a fabricated fill on the next read.
        for pair in self.requests:
            if pair.metadata.get("webull_exit_only_pair") != "true":
                continue
            from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
            children = {WebullBrokerAdapter._combo_leg_coid(pair.client_order_id, suffix)
                        for suffix in ("T", "S")}
            if request.client_order_id in children and pair.client_order_id not in self.cancelled:
                return replace(self.report(request.client_order_id, event="accepted"),
                               quantity=pair.quantity, metadata=dict(request.metadata))
        report = await super().fetch_order_update(request)
        if report is None:
            return None
        if request.side == "buy":
            return replace(report, symbol=self.symbol, quantity=self.owned, filled_quantity=self.owned,
                           reported_at=oms.utcnow())
        return replace(report, symbol=self.symbol, reported_at=oms.utcnow())

    async def fetch_quotes(self, symbols):
        return {symbol: {"bid_price": self.quote_bid, "ask_price": self.quote_bid + .01,
                         "last_price": self.quote_bid}
                for symbol in symbols}

    async def fetch_oco_exit_fill(self, account, symbol, base):
        # Controlled answer: no execution of this exact bound original pair.
        assert (account, symbol) == (self.account, self.symbol)
        return None

    def positions_wire_read_age_seconds(self, account):
        return monotonic() - self.wire_started

    async def read_exit_positions(self, account, *, after):
        # I's 158169977 response-bound API. Preserve UNKNOWN responses;
        # this port cannot modify production admission or grant terminal proof.
        started = monotonic()
        assert started > after
        self.observations.append((started, "positions_wire", account))
        if self.faults:
            fault = self.faults.pop(0)
            if fault in {"429", "unknown"}:
                raise WebullPositionsUnavailable("CONTROLLED " + fault)
            if fault == "flat":
                self.total = Decimal(0)
        rows = [replace(row, symbol=self.symbol)
                for row in await super().list_account_positions(account)]
        self.wire_started = started
        if self.cached_quantity is not None:
            self.cached_quantity = self.total
        self.observations.append((monotonic(), "positions_wire_complete", tuple(rows)))
        return tuple(rows), started

    async def release_exit_pair_for_close(self, **binding):
        await self.cancel_exit_pair(**binding)
        if self.unknown_releases:
            self.unknown_releases -= 1
            from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
            reports = tuple(replace(self.report(WebullBrokerAdapter._combo_leg_coid(
                binding["base_client_order_id"], suffix),
                event="cancelled" if index < self.confirmed_children else "accepted"),
                origin="broker" if index < self.confirmed_children else "unknown",
                metadata={"cancel_outcome": "confirmed" if index < self.confirmed_children else "could_not_tell"})
                for index, suffix in enumerate(("T", "S")))
            self.observations.append((monotonic(), "cancel_unconfirmed", reports))
            return ExitPairReleaseResult(outcome="unanswerable", reports=reports)
        result = await self.confirm_exit_pair_terminal(**binding)
        if result.outcome == "released":
            self.observations.append((monotonic(), "released", result))
        return result

    async def submit_order(self, request):
        pair = request.metadata.get("webull_exit_only_pair") == "true"
        if not pair:
            self.observations.append((monotonic(), "sell_invoked", request))
            assert request.quantity <= max(Decimal(0), self.total - self.manual_quantity), "oversell"
            if self.unknown_close:
                self.requests.append(request)
                raise TimeoutError("CONTROLLED possibly working close")
            if self.reject_close:
                self.requests.append(request)
                return [ExecutionReport("rejected", request.client_order_id, symbol=request.symbol,
                    side="sell", intent_type="close", quantity=request.quantity,
                    origin="broker", reason="CONTROLLED explicit sell rejection",
                    metadata={"webull_http_status": "417", "webull_error_code": "CONTROLLED"})]
        reports = [replace(report, symbol=self.symbol, reported_at=oms.utcnow())
                   for report in await super().submit_order(request)]
        self.observations.append((monotonic(), "pair_ack" if pair else "sell_report", reports))
        return reports


async def installed_case(make_case, *, attach_pair=True, port_type=ExitPort, d1_enabled=False):
    case = make_case()
    service, factory, old, entry_id, clock = case
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_wbexit1_d1_enabled", d1_enabled)
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_keepwatch1_enabled", True)
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_brokerdecides1_enabled", True)
    # Restore the real method shadowed by the legacy factory. External quotes
    # remain controlled I/O, but a readiness/permission decision is never faked.
    service.__dict__.pop("_v2_eod_handover_ready", None)
    service.__dict__.pop("_fetch_quote_for_order", None)
    port = port_type(service, factory, old.account)
    service.broker_adapter = port
    result = service, factory, port, entry_id, clock
    if attach_pair:
        assert await attach(result)
    assert not closes(port)
    return result


async def retained_case(make_case, symbol, *, port_type=ExitPort, d1_enabled=False):
    """Replay log-bound operands in a CONTROLLED DB preimage, not imported native rows."""
    episode = load_episode(symbol)
    case = make_case()
    service, factory, old, entry_id, clock = case
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_wbexit1_d1_enabled", d1_enabled)
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_keepwatch1_enabled", True)
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_brokerdecides1_enabled", True)
    service.__dict__.pop("_v2_eod_handover_ready", None)
    service.__dict__.pop("_fetch_quote_for_order", None)
    port = port_type(service, factory, old.account)
    port.symbol, port.owned = symbol, episode.quantity
    port.quote_bid = float(episode.entry_price)
    # HTCO reference1.63 is in its actual MANAGED-EXIT line. AIOK's executable
    # quote was not exported: its input is explicitly CONTROLLED at entry.
    port.reference_bid = 1.63 if symbol == "HTCO" else float(episode.entry_price)
    port.total = Decimal(1000) + episode.quantity  # Explicit extra manual-inventory countercase.
    service.broker_adapter = port
    object.__setattr__(service.settings, "oms_v2_cw_target_pct", Decimal(5))
    object.__setattr__(service.settings, "oms_v2_cw_hard_stop_pct", Decimal(8))
    with factory.begin() as session:
        entry = session.get(BrokerOrder, entry_id)
        entry.symbol, entry.quantity = symbol, episode.quantity
        entry.payload = {**entry.payload, "wbexit1_fixture_provenance": {
            "kind": "CONTROLLED_PREIMAGE_FROM_RETAINED_LOG_OPERANDS",
            "native_row_id_reference": episode.row_id, "native_entry_id_reference": episode.entry_id,
            "native_entry_coid_reference": episode.entry_coid,
            "native_database_preimage": "UNKNOWN_PENDING_MAIN_EXPORT"}}
        for intent in session.scalars(select(TradeIntent)):
            intent.symbol, intent.quantity = symbol, episode.quantity
        for fill in session.scalars(select(Fill)):
            fill.symbol, fill.quantity, fill.price = symbol, episode.quantity, episode.entry_price
        for position in session.scalars(select(VirtualPosition)):
            position.symbol, position.quantity, position.average_price = symbol, episode.quantity, episode.entry_price
        for position in session.scalars(select(AccountPosition)):
            position.symbol, position.quantity, position.average_price = symbol, port.total, episode.entry_price
        row = session.scalar(select(OmsManagedPosition))
        row.symbol, row.entry_price = symbol, episode.entry_price
        row.original_quantity = row.current_quantity = int(episode.quantity)
    assert await service._attach_webull_protection(broker_account_name=port.account,
        symbol=symbol, quantity=int(episode.quantity), entry_price=float(episode.entry_price),
        strategy_code="schwab_1m_v2", entry_client_order_id="owned-entry")
    pair = port.requests[-1]
    assert Decimal(pair.metadata["bracket_target_price"]) == episode.target.quantize(Decimal("0.0001"))
    assert Decimal(pair.metadata["bracket_stop_price"]) == episode.stop.quantize(Decimal("0.0001"))
    return service, factory, port, entry_id, clock


def controlled_pm_preimage(case):
    """Set only this fabricated DB preimage's clocks BEFORE application actions.

    The legacy factory initially seeds 10:00 ET. A PM test cannot rewind the
    callback to 08:00 while leaving a future entry. No native rows/tape are
    retimed: this is an explicitly CONTROLLED software-held PM case.
    """
    from datetime import timedelta
    now = case[4][0].replace(hour=12)
    entered = now - timedelta(seconds=30)
    with case[1].begin() as session:
        for order in session.scalars(select(BrokerOrder)):
            order.created_at = order.submitted_at = entered
        for intent in session.scalars(select(TradeIntent)):
            intent.created_at = entered
        for fill in session.scalars(select(Fill)):
            fill.filled_at = entered
        for row in session.scalars(select(OmsManagedPosition)):
            row.entry_time = entered
        for position in session.scalars(select(AccountPosition)):
            position.source_updated_at = entered
    case[4][0] = now


async def call_exit(case, tag="CW_HARD_STOP"):
    service, factory, port, _, clock = case
    port.quote_bid = port.reference_bid
    with factory() as session:
        row = session.scalar(select(OmsManagedPosition))
        row_id = str(row.id)
    decision = oms._ConfirmationFanoutDecision(symbol=port.symbol, source_fill_id="CONTROLLED:" + tag,
        accounts=(port.account,), exit_tag=tag)
    from datetime import timedelta
    reason = tag if tag == "V2_OVERNIGHT_FLATTEN" else "oms_v2_managed_exit:" + tag
    outcome = await service._webull_cancel_then_sell(port.account, port.symbol, exit_tag=tag,
        reason=reason, kind="HARD", reference_bid=port.reference_bid,
        expected_row_id=row_id, expires_at=clock[0] + timedelta(seconds=60), decision=decision)
    return outcome, decision


async def drain_recovery(service):
    # A bounded functional harness wait, NOT a replacement production scheduler.
    # A surviving worker is a retained assertion failure. Clean up only this
    # controlled fixture's tasks, so failed tests cannot leak DB work onward.
    tasks = tuple(service._confirmation_exit_recovery_tasks)
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=11)
        try:
            assert not pending, "recovery owner failed to reach a terminal within 11 seconds"
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()


def assert_exactly_one_close(case, expected):
    service, factory, port, _, _ = case
    requests = closes(port)
    assert [request.quantity for request in requests] == [Decimal(expected)]
    with factory() as session:
        orders = session.scalars(select(BrokerOrder).where(BrokerOrder.side == "sell",
            BrokerOrder.payload["webull_exit_only_pair"].as_string().is_(None))).all()
        # Inspect the actual canonical order collection, not a synthetic intent counter.
        assert any(order.client_order_id == requests[0].client_order_id for order in orders)
    released = [at for at, action, _ in port.observations if action == "released"]
    sold = [at for at, action, _ in port.observations if action == "sell_report"]
    assert released and sold and 0 <= sold[-1] - released[0] <= 10


async def assert_same_level_resting_replacement(case):
    """An ACK or incident alone cannot satisfy the restored-pair terminal."""
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
    port = case[2]
    pairs = [request for request in port.requests
             if request.metadata.get("webull_exit_only_pair") == "true"]
    assert len(pairs) >= 2, "no protective replacement was actually submitted"
    original, replacement = pairs[0], pairs[-1]
    assert replacement.client_order_id != original.client_order_id
    assert replacement.quantity == original.quantity
    for key in ("bracket_target_price", "bracket_stop_price"):
        assert replacement.metadata[key] == original.metadata[key]
    for suffix in ("T", "S"):
        request = replace(replacement,
            client_order_id=WebullBrokerAdapter._combo_leg_coid(replacement.client_order_id, suffix))
        report = await port.fetch_order_update(request)
        assert report is not None and report.origin == "broker"
        assert report.event_type in {"accepted", "partially_filled"}
        assert report.broker_order_id and report.quantity == replacement.quantity
    start = min(at for at, action, _ in port.observations if action == "released")
    finish = max(at for at, action, _ in port.observations if action == "pair_ack")
    assert 0 <= finish - start <= 10


async def assert_g1_sold_or_reprotected(case, expected):
    """15:50 G1: rejected/unknown SELL attempts are not successful closes."""
    reports = [report for _, action, batch in case[2].observations
               if action == "sell_report" for report in batch]
    if any(report.origin == "broker" and report.event_type in {"accepted", "filled", "partially_filled"}
           for report in reports):
        assert_exactly_one_close(case, expected)
        return "sold"
    await assert_same_level_resting_replacement(case)
    with case[1]() as session:
        assert session.scalar(select(SystemIncident.id)) is not None, "replacement requires PAGE"
    return "reprotected"
