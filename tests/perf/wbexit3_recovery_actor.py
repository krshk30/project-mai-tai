"""SOURCE UNRUN: one controlled recovery actor, real Webull read/parser threads.

Separate OMS/private DB/native book from the pending case, SAME event loop and
executor. SDK transport is controlled; no native capability or live result claim.
"""
from contextvars import ContextVar
from datetime import datetime, timedelta
from decimal import Decimal
from threading import get_ident
from time import monotonic
from types import SimpleNamespace

from sqlalchemy import select

from project_mai_tai.broker_adapters import webull_exit_census as census, webull as webull_module
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullAccountConfig
from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget
from project_mai_tai.db.models import BrokerOrder, Fill, OmsManagedPosition, Strategy, TradeIntent, VirtualPosition
from project_mai_tai.oms import wbexit3
from project_mai_tai.oms.bot_control_runtime import ControlledBrokerAdapter
from tests.unit.test_webull_adapter import _HttpResp, _adapter
from tests.unit.test_wbexit2_route import close_case, owner_case
from tests.unit.test_wbattach1_riskoff import closes
from tests.unit.wbexit3_day_history_port import SameDayHistoryBroker

_READ_STAGE = ContextVar("controlled_wb3_perf_read_stage", default="UNBOUND")


class OffloopRecoveryBroker(SameDayHistoryBroker):
    real_reads_enabled = False

    async def read_wbexit2_positions(self, account, *, after):
        if not self.real_reads_enabled:
            return await super().read_wbexit2_positions(account, after=after)
        self.positions_reads += 1
        token = _READ_STAGE.set("positions")
        try:
            return await self.history_adapter.read_wbexit2_positions(account, after=after)
        finally:
            _READ_STAGE.reset(token)

    async def read_wbexit3_day_history(self, account, *, after):
        token = _READ_STAGE.set("recovery_history")
        try:
            return await super().read_wbexit3_day_history(account, after=after)
        finally:
            _READ_STAGE.reset(token)

    async def confirm_exit_pair_from_today_list(self, **binding):
        if not self.real_reads_enabled:
            return await super().confirm_exit_pair_from_today_list(**binding)
        token = _READ_STAGE.set("original_children")
        try:
            return await self.history_adapter.confirm_exit_pair_from_today_list(**binding)
        finally:
            _READ_STAGE.reset(token)


async def build_recovery_case(make_case, monkeypatch, clock):
    """Setup only; all actual SDK/read/parser calls are deferred to measured actor."""
    baseline = clock[0]
    case = await owner_case(lambda: make_case(control=True), port_type=OffloopRecoveryBroker,
        symbol="AIOK", quantity="27", entry="10.91", at_clock=baseline)
    service, factory, broker, entry_id, same_clock = case
    assert same_clock is clock and clock[0] == baseline
    object.__setattr__(service.settings, "oms_webull_exit3_enabled", True)
    assert "orb_country_semantic_admission_enabled" in type(service.settings).model_fields
    object.__setattr__(service.settings, "orb_country_semantic_admission_enabled", True)
    broker.bid = float(broker.entry_price * Decimal("1.20"))
    broker.controlled_buy_at = baseline - timedelta(minutes=1)
    broker.history_clock = clock
    with factory.begin() as db:
        entry = db.get(BrokerOrder, entry_id)
        entry.created_at = entry.submitted_at = broker.controlled_buy_at
        db.get(TradeIntent, entry.intent_id).created_at = broker.controlled_buy_at
        for fill in db.scalars(select(Fill)):
            fill.filled_at = broker.controlled_buy_at
        row = db.scalar(select(OmsManagedPosition))
        row.entry_time = broker.controlled_buy_at
        db.scalar(select(VirtualPosition)).quantity = Decimal(0)
        strategy_id = db.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
    runtime = service._bot_control_runtime
    runtime.store.seed(strategy_id)
    await runtime.refresh()
    await wbexit3.hydrate_at_boot(service)
    service._rehydrate_managed_v2_symbols()
    assert (broker.account, broker.symbol) in service._managed_v2_symbols

    loop_thread = get_ident()
    evidence = {"loop_thread_id": loop_thread, "sdk_entries": [], "parser_entries": [],
                "synchronous_loop_reads": [], "actor_start": None, "actor_end": None}

    class HistoricalCensusClock(datetime):
        @classmethod
        def now(cls, tz=None):
            # Business clock only; real rate pacing/stall/actor wall timers stay monotonic.
            clock[0] += timedelta(microseconds=1)
            return clock[0].astimezone(tz) if tz is not None else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(census, "datetime", HistoricalCensusClock)
    monkeypatch.setattr(webull_module, "datetime", HistoricalCensusClock)  # Same controlled receipt/session clock as OMS, not a wall-rate clock.

    broker.native_account_id = broker.positions_account_id = "CONTROLLED-PERF-RECOVERY-ACCOUNT"
    broker.originals_already_terminal = True  # No second cancel; real complete LIST proves terminal IDs.
    class ControlledSDK:
        def get_response(self, request):
            kind, thread = request._kind, get_ident()
            entry = {"kind": kind, "stage": _READ_STAGE.get(), "thread_id": thread,
                "entered_at": monotonic(), "request_account": request.values.get("account_id")}
            evidence["sdk_entries"].append(entry)
            if thread == loop_thread:
                evidence["synchronous_loop_reads"].append(entry)
                raise AssertionError("CONTROLLED SDK read ran synchronously on OMS event loop")
            assert entry["request_account"] == broker.native_account_id
            if kind == "positions":
                body = {"account_id": broker.native_account_id, "has_next": False,
                    "holdings": [{"account_id": broker.native_account_id, "symbol": broker.symbol,
                                  "quantity": str(broker.total)}]}
            elif kind == "today":
                assert not request.values.get("last_client_order_id")
                body = broker.history_pages()[0]  # CONTROLLED complete account raw rows, no permission stub.
            else:
                raise AssertionError("unexpected controlled SDK endpoint: " + kind)
            entry["returned_at"] = monotonic()
            return _HttpResp(200, body)

    adapter = _adapter(ControlledSDK(), settings=service.settings,
        accounts_by_name={broker.account: WebullAccountConfig(account_id=broker.native_account_id)},
        host="CONTROLLED-WB3-PERF", app_key="CONTROLLED-WB3-PERF")
    adapter._query_budget = QueryBudget()  # Real shared primitive/clock; no permit inflation.
    adapter._today_reader = SimpleNamespace(cycle_seconds=2)
    reader = census.ExitCensusReader(adapter._query_budget, 2)
    monkeypatch.setattr(census, "shared_exit_census", lambda *_args: reader)

    def observed_parser(name, actual):
        def run(*args):
            entry = {"parser": name, "stage": _READ_STAGE.get(), "thread_id": get_ident(), "entered_at": monotonic(),
                     "foreground_reads_at_entry": adapter._query_budget.foreground_reads}
            evidence["parser_entries"].append(entry)
            if entry["thread_id"] == loop_thread:
                evidence["synchronous_loop_reads"].append(entry)
                raise AssertionError("real Webull blocking parser entered on OMS event loop")
            try:
                return actual(*args)
            finally:
                entry["returned_at"] = monotonic()
        return run
    for name in ("_wbexit2_positions_blocking", "_wbexit3_day_history_blocking", "_exit_pair_list_proof"):
        monkeypatch.setattr(adapter, name, observed_parser(name, getattr(adapter, name)))
    broker.history_adapter = adapter
    broker.real_reads_enabled = True
    router = RoutingBrokerAdapter(default_provider="webull", provider_by_account={broker.account: "webull"},
        factories_by_provider={"webull": lambda: broker})
    service.broker_adapter = ControlledBrokerAdapter(router, runtime)
    assert broker.positions_reads == broker.history_calls == 0 and evidence["sdk_entries"] == []
    return case, evidence


async def run_recovery(case, evidence):
    evidence["actor_start"] = monotonic()
    try:
        await close_case(case, "CW_TARGET")  # Actual recovered cap/named prepare/O14/final route.
        assert [request.quantity for request in closes(case[2])] == [Decimal(27)]
        assert case[2].total == 1000 and not evidence["synchronous_loop_reads"]
        assert case[2].positions_reads == 2 and case[2].history_calls == 1
    finally:
        evidence["actor_end"] = monotonic()


def recovery_summary(case, evidence):
    service, factory, broker, _, _ = case
    return {"actor_wall_ms": ((evidence["actor_end"] - evidence["actor_start"]) * 1000
            if evidence["actor_start"] is not None and evidence["actor_end"] is not None else None),
        "positions_reads": broker.positions_reads, "history_calls": broker.history_calls,
        "loop_thread_id": evidence["loop_thread_id"], "sdk_entries": evidence["sdk_entries"],
        "parser_entries": evidence["parser_entries"], "synchronous_loop_reads": evidence["synchronous_loop_reads"],
        "instance_scope": "SECOND_OMS_SEPARATE_PRIVATE_DB_NATIVE_BOOK_SAME_LOOP_EXECUTOR",
        "single_DB_contention": "NOT_COVERED", "native": "CONTROLLED_SDK_NOT_NATIVE_PASS"}
