from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.momentum_live.admission import Refusal
from project_mai_tai.momentum_live.runtime import OwnedNativeBackend


NOW = datetime(2026, 10, 14, 18, tzinfo=UTC)
NS = int(NOW.timestamp() * 1e9)


def raw_tree(status="CANCELED"):
    return {"orderId": "root", "childOrderStrategies": [
        {"orderId": "target", "status": status, "filledQuantity": 4,
         "orderLegCollection": [{"legId": 1, "instruction": "SELL", "instrument": {"symbol": "SCNI"}}],
         "orderActivityCollection": [{"executionLegs": [
             {"legId": 1, "quantity": 1, "price": "3.15", "time": NOW.isoformat()},
             {"legId": 1, "quantity": 3, "price": "3.16", "time": NOW.isoformat()}]}]},
        {"orderId": "stop", "status": "CANCELED", "filledQuantity": 0,
         "orderLegCollection": [{"legId": 2, "instruction": "SELL", "instrument": {"symbol": "SCNI"}}],
         "orderActivityCollection": [{"executionLegs": [
             {"legId": 2, "quantity": 0, "price": 0}]}]}]}


def handle():
    return SimpleNamespace(symbol="SCNI", quantity=D(10), child_ids=(), account="schwab-account",
                           episode_id="owned-episode", entry_order_id="entry", base_coid="own-root", session="NORMAL")


def parse(raw, owned=None):
    return OwnedNativeBackend.schwab_children(raw, owned or handle(), SchwabBrokerAdapter.__new__(SchwabBrokerAdapter),
        root_id="root", first_fill_ns=NS-1_000_000_000, now_ns=NS)


@pytest.mark.parametrize("status,terminal", [("PARTIALLY_FILLED", False), ("CANCELED", True), ("FILLED", True)])
def test_real_adapter_raw_partial_and_cancelled_execution_vwap(status, terminal):
    rows = parse(raw_tree(status))
    indexed = {row[0]: row for row in rows}
    assert indexed["target"][:5] == ("target", terminal, D(4), D("3.1575"), NOW)
    assert indexed["stop"][2:5] == (D(0), None, None)


def test_documented_positive_quantity_zero_price_cancelled_sibling_is_not_an_execution():
    raw = raw_tree()
    raw["childOrderStrategies"][1]["orderActivityCollection"][0]["executionLegs"][0]["quantity"] = 2
    indexed = {row[0]: row for row in parse(raw)}
    assert indexed["target"][2:4] == (D(4), D("3.1575"))
    assert indexed["stop"][2:5] == (D(0), None, None)


def test_cancelled_partial_with_zero_price_activity_keeps_only_exact_positive_execution_coverage():
    raw = raw_tree()
    target = raw["childOrderStrategies"][0]
    target["orderActivityCollection"][0]["executionLegs"].append({"quantity": 2, "price": 0})
    indexed = {row[0]: row for row in parse(raw)}
    assert indexed["target"][2:4] == (D(4), D("3.1575"))


@pytest.mark.parametrize("change", ["missing", "unknown", "zero_price", "wrong_symbol", "wrong_root",
                                    "missing_execution", "future", "duplicate_child"])
def test_unreadable_or_ambiguous_tree_never_releases_reservation(change):
    raw = raw_tree()
    target = raw["childOrderStrategies"][0]
    if change == "missing":
        raw = None
    elif change == "unknown":
        target["status"] = "UNKNOWN"
    elif change == "zero_price":
        target["orderActivityCollection"][0]["executionLegs"][0]["price"] = 0
    elif change == "wrong_symbol":
        target["orderLegCollection"][0]["instrument"]["symbol"] = "ATR"
    elif change == "wrong_root":
        raw["orderId"] = "foreign-root"
    elif change == "missing_execution":
        target["orderActivityCollection"] = []
    elif change == "future":
        target["orderActivityCollection"][0]["executionLegs"][0]["time"] = "2026-10-14T18:00:01+00:00"
    else:
        raw["childOrderStrategies"].append(deepcopy(target))
    with pytest.raises(Refusal):
        parse(raw)


@pytest.mark.asyncio
async def test_owned_read_books_cancelled_partial_before_terminal_release_without_generic_filled_only_helper():
    backend = OwnedNativeBackend()
    owned = handle()
    leaf = create_autospec(SchwabBrokerAdapter, instance=True)
    leaf.accounts_by_name = {owned.account: object()}
    leaf._fetch_order.return_value = raw_tree()
    leaf._parse_datetime.side_effect = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)._parse_datetime
    leaf.ACCEPTED_STATUSES = SchwabBrokerAdapter.ACCEPTED_STATUSES
    leaf.PARTIAL_FILL_STATUSES = SchwabBrokerAdapter.PARTIAL_FILL_STATUSES
    leaf.CANCELLED_STATUSES = SchwabBrokerAdapter.CANCELLED_STATUSES
    leaf.REJECTED_STATUSES = SchwabBrokerAdapter.REJECTED_STATUSES
    request = OrderRequest(client_order_id="own-root", strategy_code="momentum_warn_live",
        broker_account_name=owned.account, symbol="SCNI", side="sell", quantity=D(10),
        intent_type="close", reason="owned-protection", metadata={})
    snapshot = {"first_fill_ns": NS-1_000_000_000, "exited": D(4)}
    backend.root = AsyncMock(return_value=({"request": request, "broker_order_id": "root"}, snapshot))
    backend._record_child = AsyncMock()
    backend.leaf = lambda account, runtime: leaf
    async def close_after_record(unit):
        assert backend._record_child.await_count == 1
    runtime = SimpleNamespace(service=SimpleNamespace(broker_adapter=leaf, _run_db=close_after_record),
        provider=lambda account: "schwab", now_ns=lambda: NS, _inspect=AsyncMock(return_value=snapshot))
    result = await backend.read(owned, runtime)
    report = backend._record_child.await_args.args[2]
    assert report.filled_quantity == 4 and report.fill_price == D("3.1575")
    assert result.terminal and result.cumulative_exited == 4
    leaf.fetch_oco_exit_fill.assert_not_awaited()


def test_cancelled_partial_raw_cumulative_replay_books_once_and_only_remaining_owned_quantity():
    from tests.unit.test_momlive1_persistence import setup
    from project_mai_tai.db.models import Fill
    from sqlalchemy import select
    factory, store, _, episode, entry = setup()
    raw = raw_tree()
    raw["childOrderStrategies"][0]["filledQuantity"] = 1
    raw["childOrderStrategies"][0]["orderActivityCollection"][0]["executionLegs"] = [
        {"legId": 1, "quantity": 1, "price": "3.15", "time": NOW.isoformat()}]
    owned = handle()
    owned.quantity = D(3)
    child_row = next(item for item in parse(raw, owned) if item[0] == "target")
    sell = OrderRequest(client_order_id="own-target", strategy_code="momentum_warn_live",
        broker_account_name=entry.broker_account_name, symbol="SCNI", side="sell", quantity=D(3),
        intent_type="close", reason="owned-target", metadata={"episode_id": episode.episode_id})
    with factory() as session:
        store.prepare_order(session, episode.episode_id, entry, provider="schwab")
        store.record(session, entry, [ExecutionReport("filled", entry.client_order_id,
            filled_quantity=D(3), fill_price=D(3), origin="broker", reported_at=NOW,
            metadata={"momlive_execution_time_source": "broker_execution_time"})])
        store.prepare_order(session, episode.episode_id, sell, provider="schwab")
        report = ExecutionReport(child_row[5], sell.client_order_id, broker_order_id="target",
            filled_quantity=child_row[2], fill_price=child_row[3], reported_at=child_row[4], origin="broker",
            metadata={"momlive_execution_time_source": "broker_execution_time"})
        store.record(session, sell, [report, report])
        session.commit()
        snapshot = store.inspect_episode(session, episode.episode_id)
        assert snapshot["exited"] == 1 and snapshot["quantity"] == 3
        assert len(session.scalars(select(Fill)).all()) == 2
        remaining = OrderRequest(client_order_id="soft-remaining", strategy_code="momentum_warn_live",
            broker_account_name=entry.broker_account_name, symbol="SCNI", side="sell", quantity=D(2),
            intent_type="close", reason="time-exit", metadata={})
        store.prepare_order(session, episode.episode_id, remaining, provider="schwab")
        from dataclasses import replace
        with pytest.raises(Refusal, match="owned_quantity"):
            store.prepare_order(session, episode.episode_id,
                replace(remaining, client_order_id="unsafe-all", quantity=D(3)), provider="schwab")


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", [None, "id", "symbol", "side", "quantity", "price", "leg", "future"])
async def test_actual_schwab_entry_mapper_requires_raw_identity_and_cumulative_execution_proof(defect):
    leaf = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    leaf.accounts_by_name = {"schwab-account": object()}
    raw = deepcopy(raw_tree("PARTIALLY_FILLED")["childOrderStrategies"][0])
    raw["orderId"] = "OWN"
    raw["orderLegCollection"][0]["instruction"] = "BUY"
    if defect == "id":
        raw["orderId"] = "OTHER"
    elif defect == "symbol":
        raw["orderLegCollection"][0]["instrument"]["symbol"] = "OTHER"
    elif defect == "side":
        raw["orderLegCollection"][0]["instruction"] = "SELL"
    elif defect == "quantity":
        raw["filledQuantity"] = 5
    elif defect == "price":
        raw["orderActivityCollection"][0]["executionLegs"][0]["price"] = 0
    elif defect == "leg":
        raw["orderActivityCollection"][0]["executionLegs"][0]["legId"] = 2
    elif defect == "future":
        raw["orderActivityCollection"][0]["executionLegs"][0]["time"] = "2026-10-14T18:00:01+00:00"
    leaf._fetch_order = AsyncMock(return_value=raw)
    request = OrderRequest(client_order_id="own-entry", strategy_code="momentum_warn_live",
        broker_account_name="schwab-account", symbol="SCNI", side="buy", quantity=D(10),
        intent_type="open", reason="sealed", metadata={"broker_order_id": "OWN"})
    runtime = SimpleNamespace(service=SimpleNamespace(broker_adapter=leaf),
        provider=lambda account: "schwab", now_ns=lambda: NS)
    if defect:
        with pytest.raises(Refusal):
            await OwnedNativeBackend().fetch_entry_update(request, runtime)
    else:
        report = await OwnedNativeBackend().fetch_entry_update(request, runtime)
        assert report.broker_order_id == "OWN" and report.origin == "broker"
        assert report.filled_quantity == 4 and report.fill_price == D("3.1575")
        assert report.reported_at == NOW
