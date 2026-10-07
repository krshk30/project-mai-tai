"""Recorded prices/probes, real v2 -> OMS -> Schwab serialization, fake transport.

Quotes, database setup, acceptance and strict cancellation answers are controlled
replay inputs. No broker client, service or production ledger is contacted.
"""
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, SNAPSHOT_TYPE
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from tests.unit.test_rpg1_runtime import runtime, feedback
from tests.unit.test_rpgstuck1 import RECORDED, PRIMARY

SEQUENCES = json.loads((Path(__file__).parents[1] / "fixtures/rpgstuck1_sequences.json").read_text())
ACCOUNT = "live:schwab_1m_v2"


def probe_at(symbol, minute):
    return next(probe for probe in SEQUENCES["probes"]
                if probe["fields"]["sym"] == symbol and minute in probe["observed_at"])


def apply_probe(h, probe, *, at=None):
    fields = probe["fields"]
    h.clock[0] = at or datetime.strptime(probe["observed_at"], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)
    h.state.atr_trail = float(fields["trail"])
    h.state.atr_state, h.state.atr_state_age = fields["state"], int(fields["age"])
    # Probe records no OPEN/bid/ask: use CLOSE for the synthetic open and a fresh
    # synthetic quote below the trigger. No market acceptance is inferred.
    close = float(fields["close"])
    h.state.bars.append(OHLCVBar(int(fields["ts_ms"]), close, float(fields["high"]),
                               float(fields["low"]), close, int(fields["vol"])))
    h.state.last_quote = Quote(h.state.symbol, close, close + .01, close, h.strategy._now_ms())


async def wire_harness(monkeypatch, symbol, old, *, initial_wire=False):
    h = await runtime(monkeypatch, "schwab", notional=600)
    h.service.settings.oms_v2_emit_native_oco_bracket_enabled = True
    h.service.settings.schwab_native_bracket_enabled = True
    h.service._cw_target_pct, h.service._cw_stop_pct = 5.0, 8.0
    del h.strategy._symbol_states[h.state.symbol]
    h.state = h.strategy.watchlist_state(symbol)
    h.state.fanout_segment_id = int(old["metadata"]["fanout_segment_id"])
    h.state.atr_short_flip_bar_ts = int(old["metadata"]["rpg_short_segment"])
    adapter = SchwabBrokerAdapter(h.service.settings.model_copy(update={
        "schwab_token_store_path": None, "schwab_access_token": "SIMULATED",
        "schwab_refresh_token": None, "schwab_client_secret": "SIMULATED"}),
        accounts_by_name={ACCOUNT: SchwabAccountConfig("SIMULATED-ACCOUNT")})
    h.wires, h.raw_inputs, h.final_guards, h.cancel_reads = [], [], [], []

    async def transport(method, path, *, body=None):
        assert method == "POST" and path == "/trader/v1/accounts/SIMULATED-ACCOUNT/orders"
        h.wires.append(deepcopy(body))
        identity = old["metadata"]["broker_order_id"] if initial_wire and len(h.wires) == 1 else "SIMULATED-replacement"
        return 201, {"Location": f"/orders/{identity}"}, {}

    async def cancel(account, request):
        assert request.metadata["broker_order_id"] == old["metadata"]["broker_order_id"]
        return []  # Simulated lost ACK; only the next strict answer permits replacement.

    async def readback(request):
        assert request.client_order_id == old["client_order_id"]
        h.cancel_reads.append(request)
        return AtrBuyReadback("cancelled_empty", "SIMULATED exact terminal-zero detail",
            Decimal(0), terminal_cancel=True, broker_order_id=old["metadata"]["broker_order_id"])

    process, guard = h.service.process_trade_intent, h.service._rpg_open_refusal

    async def capture_input(event):
        if event.payload.intent_type == "open" and event.payload.broker_account_name == ACCOUNT:
            h.raw_inputs.append(deepcopy(event.payload.metadata))
        return await process(event)

    def capture_guard(event, **kwargs):
        verdict = guard(event, **kwargs)
        if event.payload.metadata.get("rpg_handoff_token"):
            h.final_guards.append((deepcopy(event.payload.metadata), verdict))
        return verdict

    monkeypatch.setattr(adapter, "_authorized_request_json", transport)
    monkeypatch.setattr(adapter, "_cancel_order", cancel)
    monkeypatch.setattr(h.service, "process_trade_intent", capture_input)
    monkeypatch.setattr(h.service, "_rpg_open_refusal", capture_guard)
    h.service.broker_adapter = SimpleNamespace(submit_order=adapter.submit_order,
                                               read_atr_resting_buy_after_cancel=readback)
    return h


def assert_wire(body, symbol, stop, limit, quantity):
    assert (body["orderType"], body["orderStrategyType"], body["session"]) == ("STOP_LIMIT", "TRIGGER", "NORMAL")
    assert (Decimal(str(body["stopPrice"])), Decimal(str(body["price"]))) == (Decimal(stop), Decimal(limit))
    leg, = body["orderLegCollection"]
    assert leg == {"instruction": "BUY", "quantity": float(quantity),
                   "instrument": {"symbol": symbol, "assetType": "EQUITY"}}
    oco, = body["childOrderStrategies"]
    assert oco["orderStrategyType"] == "OCO"
    assert [exit_leg["orderType"] for exit_leg in oco["childOrderStrategies"]] == ["LIMIT", "STOP"]
    assert all(exit_leg["orderLegCollection"][0]["quantity"] == float(quantity)
               for exit_leg in oco["childOrderStrategies"])


@pytest.mark.asyncio
@pytest.mark.parametrize("row,minute,stop,limit,quantity", [
    (PRIMARY[0], "13:32", "5.27", "5.30", 113),
    (PRIMARY[1], "13:36", "5.51", "5.54", 108),
], ids=["APUS-0932", "VEEA-0936"])
async def test_recorded_0932_0936_strategy_authorization_to_schwab_wire(monkeypatch, row, minute, stop, limit, quantity):
    job, token = deepcopy(row["payload"]), UUID(row["id"])
    old, recorded_auth = job["old"], deepcopy(job["authorization"])
    h = await wire_harness(monkeypatch, old["symbol"], old)
    apply_probe(h, probe_at(old["symbol"], minute), at=datetime.fromtimestamp(recorded_auth["at"], UTC))
    # Replay from its recorded proven-clear stage, not the later refusal. The new
    # authorization is produced by the real callback, never copied into the journal.
    job.update(phase="clear", clear_recorded=True)
    job.pop("authorization", None)
    with h.factory() as session:
        strategy = h.service.store.ensure_strategy(session, "schwab_1m_v2", name="v2")
        account = h.service.store.ensure_broker_account(session, ACCOUNT, provider="simulated", environment="test")
        event = TradeIntentEvent(source_service="schwab-1m-v2", payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2", broker_account_name=ACCOUNT, symbol=old["symbol"], side="buy",
            intent_type="open", quantity=Decimal(old["quantity"]), reason=old["reason"], metadata=old["metadata"]))
        intent = h.service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
        order = h.service.store.get_or_create_order(session, intent=intent, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id=old["client_order_id"], broker_order_id=old["metadata"]["broker_order_id"],
            symbol=old["symbol"], side="buy", quantity=Decimal(old["quantity"]), metadata=old["metadata"],
            status="cancelled", order_type="STOP_LIMIT", time_in_force="day")
        order.id = UUID(job["original_order_id"])
        session.add(DashboardSnapshot(id=token, snapshot_type=SNAPSHOT_TYPE, payload=job))
        other = next(item for item in RECORDED["tickets"] if item["payload"]["old"]["symbol"] == old["symbol"]
                     and item["payload"]["old"]["broker_account_name"] != ACCOUNT)
        session.add(DashboardSnapshot(id=UUID(other["id"]), snapshot_type=SNAPSHOT_TYPE, payload=deepcopy(other["payload"])))
        session.commit()
    await feedback(h)
    placed = HandoffJournal(h.factory).read(token)
    assert placed["phase"] == "placed"
    actual = placed["authorization"]["event"]["payload"]
    for key in ("stop_price", "limit_price", "cw_flip_level", "fanout_segment_id", "fanout_slot_id"):
        assert actual["metadata"][key] == recorded_auth["event"]["payload"]["metadata"][key]
        assert h.raw_inputs[0][key] == actual["metadata"][key]
    assert actual["quantity"] == str(quantity)
    assert h.final_guards and all(verdict is None for _, verdict in h.final_guards)
    final_md, _ = h.final_guards[-1]
    assert (final_md["stop_price"], final_md["limit_price"]) == (stop, limit)
    body, = h.wires
    assert_wire(body, old["symbol"], stop, limit, quantity)
    await feedback(h)
    await h.service._rpg_advance(token)
    assert len(h.wires) == 1 and not h.cancel_reads
    if old["symbol"] == "VEEA":
        assert h.strategy._rpg_entry_owned(h.state, account="live:orb")


@pytest.mark.asyncio
async def test_recorded_apus_1025_1026_1027_place_keep_reprice_to_schwab_wire(monkeypatch):
    row = next(row for row in SEQUENCES["late"]["queries"]["tickets"] if row["payload"]["old"]["broker_account_name"] == ACCOUNT)
    old = row["payload"]["old"]
    h = await wire_harness(monkeypatch, "APUS", old, initial_wire=True)
    monkeypatch.setattr("project_mai_tai.strategy_core.schwab_1m_v2.uuid4", lambda: UUID(old["metadata"]["rpg_resting_generation"]))
    apply_probe(h, probe_at("APUS", "14:25"))
    h.strategy._cw_v2_resting_track(h.state, None)
    initial, = h.strategy.drain_pending_intents()
    mirror, = h.strategy.drain_webull_direct_intents()
    assert (initial.metadata["stop_price"], initial.metadata["limit_price"], initial.quantity) == ("5.3220", "5.3486", Decimal(112))
    source = SEQUENCES["late"]["queries"]["intents"][0]
    event = TradeIntentEvent(event_id=UUID(source["payload"]["event_id"]), source_service="schwab-1m-v2",
        produced_at=h.clock[0], payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=ACCOUNT,
            symbol="APUS", side="buy", intent_type="open", quantity=initial.quantity, reason=initial.reason, metadata=initial.metadata))
    await h.service.process_trade_intent(event)
    assert_wire(h.wires[0], "APUS", "5.32", "5.35", 112)
    held_source = next(row for row in SEQUENCES["late"]["queries"]["intents"] if row["account"] == "live:orb")
    held_md = {**mirror.metadata, **{key: value for key, value in held_source["payload"]["metadata"].items()
                                   if key.startswith("webull_shape_market_")}}
    h.clock[0] = datetime.fromisoformat(held_source["created_at"])
    h.service._latest_quotes_by_symbol["APUS"] = {
        "ask": Decimal(held_md["webull_shape_market_price"]),
        "received_at": datetime.fromisoformat(held_md["webull_shape_market_at_utc"]),
    }
    mirror_event = event.model_copy(deep=True)
    mirror_event.event_id = UUID(held_source["payload"]["event_id"])
    mirror_event.payload.broker_account_name, mirror_event.payload.quantity = "live:orb", mirror.quantity
    mirror_event.payload.metadata = held_md
    await h.service.process_trade_intent(mirror_event)
    assert h.service._webull_mirror_deferred_by_slot[held_md["fanout_slot_id"]].local_no_wire
    assert len(h.wires) == 1
    apply_probe(h, probe_at("APUS", "14:26"))
    h.strategy._cw_v2_resting_track(h.state, None)
    assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()
    assert len(h.wires) == 1
    apply_probe(h, probe_at("APUS", "14:27"))
    h.strategy._cw_v2_resting_track(h.state, None)
    primary_cancel, = h.strategy.drain_pending_intents()
    mirror_cancel, = h.strategy.drain_webull_direct_intents()
    for account, draft in ((ACCOUNT, primary_cancel), ("live:orb", mirror_cancel)):
        cancel_event = TradeIntentEvent(source_service="schwab-1m-v2", produced_at=h.clock[0], payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2", broker_account_name=account, symbol="APUS", side="buy", intent_type="cancel",
            quantity=draft.quantity, reason=draft.reason, metadata=draft.metadata))
        await h.service._handle_stream_message({"data": cancel_event.model_dump_json()})
    journal, token = HandoffJournal(h.factory), UUID(row["id"])
    assert len(h.cancel_reads) == 1 and len(h.wires) == 1
    local_job = next(job for _, job in journal.jobs() if job["old"]["broker_account_name"] == "live:orb")
    assert local_job["local_no_wire"] and local_job["phase"] == "clear"
    h.clock[0] = datetime.fromtimestamp(row["payload"]["authorization"]["at"], UTC)
    h.state.last_quote = Quote("APUS", 4.62, 4.63, 4.62, h.strategy._now_ms())
    # Exercise the real bot callback/persistence. Only the Schwab serial delivery
    # is selected here; mirror price-wait/retry is covered in the companion module.
    await h.bot._rpg_handoff_pass()
    await h.service._rpg_advance(token)
    placed = journal.read(token)
    assert placed["phase"] == "placed"
    assert (h.raw_inputs[-1]["stop_price"], h.raw_inputs[-1]["limit_price"]) == ("5.2391", "5.2653")
    assert placed["authorization"]["event"]["payload"]["quantity"] == "114"
    assert all(verdict is None for _, verdict in h.final_guards)
    final_md, _ = h.final_guards[-1]
    assert (final_md["stop_price"], final_md["limit_price"]) == ("5.24", "5.27")
    assert len(h.wires) == 2
    assert_wire(h.wires[-1], "APUS", "5.24", "5.27", 114)
    await h.service._rpg_advance(token)
    assert len(h.wires) == 2 and len(h.cancel_reads) == 1
    with h.factory() as session:
        orders = list(session.scalars(select(BrokerOrder).where(BrokerOrder.symbol == "APUS")))
        assert sorted((order.status, int(order.quantity)) for order in orders) == [("accepted", 114), ("cancelled", 112)]
