"""CONTROLLED log-reconstructed today-list transport; no retained native-history claim."""
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from project_mai_tai.broker_adapters import webull_exit_census as census
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.broker_adapters.webull_order_reads import QueryBudget
from tests.unit.test_wbexit2_route import ControlledOwnerBroker


class SameDayHistoryBroker(ControlledOwnerBroker):
    native_quantity = None  # Normal positive census follows actual controlled account total, including manual shares.
    history_mode = "clean"
    history_calls = 0
    on_history = None
    post_terminal_positions_unknown = False
    originals_already_terminal = False
    on_postterminal = None

    async def confirm_exit_pair_from_today_list(self, **binding):
        result = await super().confirm_exit_pair_from_today_list(**binding)
        if not self.originals_already_terminal:
            return result
        return replace(result, outcome="released", reports=tuple(replace(report, event_type="cancelled",
            metadata={key: value for key, value in report.metadata.items() if key != "wbexit2_geometry"})
            for report in result.reports))

    async def read_wbexit2_positions(self, account, *, after):
        if self.post_terminal_positions_unknown and self.cancelled:
            self.positions_unknown = True
        rows, started, native = await super().read_wbexit2_positions(account, after=after)
        if self.cancelled and self.on_postterminal is not None:
            await self.on_postterminal()
        return [replace(row, quantity=self.total if self.native_quantity is None else self.native_quantity) for row in rows], started, native

    async def read_wbexit3_day_history(self, account, *, after):
        self.history_calls += 1
        result = await self.history_adapter.read_wbexit3_day_history(account, after=after)
        if self.on_history is not None:
            self.on_history()
        return result

    def raw_order(self, coid, native, side, status, filled="0", *, filled_at=None):
        item = {"symbol": self.symbol, "side": side, "status": status,
                "qty": str(self.owned), "filled_qty": filled}
        if Decimal(filled) > 0:
            item.update(filled_price=str(self.entry_price), last_filled_time=filled_at.isoformat())
        return {"client_order_id": coid, "order_id": native,
                "account_id": self.native_account_id, "items": [item]}

    def history_pages(self):
        rows = [self.raw_order("owned-entry", "entry-native", "buy", "FILLED",
                    str(self.owned), filled_at=getattr(self, "controlled_last_buy_at", self.controlled_buy_at))]
        pair = next(r for r in self.requests if r.metadata.get("webull_exit_only_pair") == "true")
        for suffix in ("T", "S"):
            coid = WebullBrokerAdapter._combo_leg_coid(pair.client_order_id, suffix)
            native = self.report(coid).broker_order_id
            original = self.raw_order(coid, native, "sell", "CANCELLED" if self.originals_already_terminal or pair.client_order_id in self.cancelled else "SUBMITTED")
            key, field = (("bracket_target_price", "limit_price") if suffix == "T" else ("bracket_stop_price", "stop_price"))
            original["items"][0][field] = str(WebullBrokerAdapter._round_to_tick(Decimal(pair.metadata[key])))
            rows.append(original)
        if self.history_mode in {"manual_filled", "manual_partial", "working_sell", "missing_client", "missing_fill_time", "manual_between_BUYs", "naive_fill_time"}:
            event = {"manual_filled": "FILLED", "manual_partial": "PARTIALLY_FILLED", "missing_fill_time": "FILLED", "manual_between_BUYs": "FILLED", "naive_fill_time": "FILLED"}.get(self.history_mode, "SUBMITTED")
            amount = str(self.owned) if event == "FILLED" else "1" if event == "PARTIALLY_FILLED" else "0"
            manual = self.raw_order("CONTROLLED-manual-sell", "CONTROLLED-manual-native", "sell", event,
                amount, filled_at=(self.controlled_buy_at + timedelta(seconds=30)
                    if self.history_mode == "manual_between_BUYs" else self.history_clock[0] + timedelta(microseconds=1)))
            if self.history_mode == "missing_client":
                manual.pop("client_order_id")
            if self.history_mode == "naive_fill_time":
                manual["items"][0]["last_filled_time"] = self.history_clock[0].replace(tzinfo=None).isoformat()
            if self.history_mode == "missing_fill_time":
                manual["items"][0].pop("last_filled_time")
                manual["items"][0]["updated_at"] = self.history_clock[0].isoformat()
            rows.append(manual)
        if self.history_mode in {"original_partial", "original_filled"}:
            old = rows[-1]
            rows[-1] = self.raw_order(old["client_order_id"], old["order_id"], "sell",
                "FILLED" if self.history_mode == "original_filled" else "PARTIALLY_FILLED",
                str(self.owned) if self.history_mode == "original_filled" else "1",
                filled_at=self.history_clock[0] + timedelta(microseconds=1))
        if self.history_mode == "original_native_changed":
            rows[-1]["order_id"] = "CONTROLLED-unknown-replacement-native"
        if self.history_mode == "working_original_geometry_changed":
            rows[-1]["items"][0]["stop_price"] = "1.00"
        if self.history_mode in {"envelope_status_unknown", "envelope_status_null", "envelope_status_conflict"}:
            rows[-1]["status"] = {"envelope_status_unknown": "GARBAGE", "envelope_status_null": None,
                                  "envelope_status_conflict": "FILLED"}[self.history_mode]
        if self.history_mode in {"native_alias_null", "native_alias_empty"}:
            rows[-1]["orderId"] = None if self.history_mode == "native_alias_null" else ""
        if self.history_mode == "alias_conflict":
            rows[-1]["filled_qty"] = "1"  # Envelope/item discrepancy may never be ignored.
        pages = [{"orders": rows, "has_next": False, "account_id": self.native_account_id}]
        if self.history_mode == "manual_second_page":
            manual = self.raw_order("CONTROLLED-manual-tail", "CONTROLLED-manual-tail-native", "sell", "FILLED",
                str(self.owned), filled_at=self.history_clock[0] + timedelta(microseconds=1))
            pages = [{"orders": rows, "has_next": True, "account_id": self.native_account_id},
                     {"orders": [manual], "has_next": False, "account_id": self.native_account_id}]
        if self.history_mode == "partial_pages":
            pages[0]["has_next"] = True
        return pages


def bind_real_history_reader(case, monkeypatch):
    """Keep real foreground ownership, query budget, full census and normalization.

    Only clock and the existing _today_page transport return controlled values.
    The real same-day list, order IDs and negative execution history were NOT retained.
    """
    service, _, broker, _, clock = case
    broker.history_clock = clock

    class HistoricalClock(datetime):
        @classmethod
        def now(cls, tz=None):
            clock[0] += timedelta(microseconds=1)
            return clock[0].astimezone(tz) if tz is not None else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(census, "datetime", HistoricalClock)
    budget = QueryBudget(clock=lambda: clock[0].timestamp())
    reader = census.ExitCensusReader(budget, 2, clock=lambda: clock[0].timestamp())
    monkeypatch.setattr(census, "shared_exit_census", lambda *args: reader)
    adapter = object.__new__(WebullBrokerAdapter)
    adapter.settings = service.settings
    adapter.accounts_by_name = {broker.account: SimpleNamespace(account_id=broker.native_account_id)}
    adapter.host, adapter.app_key = "CONTROLLED-PRIVATE", "CONTROLLED-PRIVATE"
    adapter._query_budget = budget
    adapter._today_reader = SimpleNamespace(cycle_seconds=2)
    broker.history_page_calls = []

    def page(account, cursor):
        broker.history_page_calls.append(cursor)
        pages = broker.history_pages()
        if broker.history_mode == "unreadable":
            return 429, {"error_code": "CONTROLLED history unreadable"}
        index = 0 if not cursor else 1
        if index >= len(pages):
            return 200, {"orders": [], "has_next": True, "account_id": broker.native_account_id}
        return 200, pages[index]

    adapter._today_page = page
    broker.history_adapter = adapter
    return broker


class NoManualHistoryBroker(SameDayHistoryBroker):
    """Separate minimum-native27 control with manual baseline0, not a mixed-book claim."""
    async def submit_order(self, request):
        if request.metadata.get("webull_exit_only_pair") == "true":
            return await super().submit_order(request)
        from tests.unit.test_wbattach1_riskoff import Broker as TypedProvider
        from project_mai_tai.oms import service as oms
        assert request.quantity <= self.owned and request.quantity <= self.total
        # The same typed external provider send/receipt; application ownership,
        # preparation, O14 and final wire functions remain real and unpatched.
        return [replace(report, symbol=self.symbol, reported_at=oms.utcnow(),
            fill_price=Decimal(str(self.bid)) if report.filled_quantity > 0 else report.fill_price)
            for report in await TypedProvider.submit_order(self, request)]
