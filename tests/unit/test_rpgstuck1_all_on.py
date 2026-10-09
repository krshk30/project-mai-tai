"""ALL-ON replay: recorded evidence, controlled tape/acceptance, zero real I/O."""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.atr_buy_readback import schwab_buy_readback
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter
from project_mai_tai.broker_adapters import webull as webull_module
from project_mai_tai.db.models import Fill
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, _request, old_buy_proven_clear
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from tests.unit import test_rpgstuck1_startup as startup
from tests.unit.test_rpgstuck1_later_startup import LATER, LOCAL
from tests.unit.test_rpg1_runtime import begin, feedback, runtime
from tests.unit import test_webull_adapter as webull_tests
from tests.unit.test_rpgstuck1_schwab_sequences import probe_at

ALL_ON = {key: True for key in startup.TONIGHT_FLAGS}


@pytest.fixture
def fake_sdk(monkeypatch):
    return webull_tests.fake_sdk.__wrapped__(monkeypatch)


def all_on(h):
    settings = h.service.settings.model_copy(update={**ALL_ON,
        "strategy_schwab_1m_v2_entry_notional_usd": 600,
        "strategy_schwab_1m_v2_webull_entry_notional_usd": 300,
        "oms_v2_emit_native_oco_bracket_enabled": True,
        "schwab_native_bracket_enabled": True,
    })
    h.service.settings = h.strategy.settings = h.bot.settings = settings
    h.strategy._gap_hold_enabled = True
    for key in ALL_ON:
        assert getattr(settings, key) is True
    assert not h.strategy._flip_owned_first_entry_enabled


def price(h, state, ask):
    state.last_quote = Quote(state.symbol, float(ask), float(ask), float(ask), h.strategy._now_ms())
    h.service._latest_quotes_by_symbol[state.symbol] = {"ask": Decimal(str(ask)), "received_at": h.clock[0]}
    FanoutSegmentIdentityStore(h.factory).record(state.symbol, state.fanout_segment_id,
        True, "CONTROLLED ALL-ON replay active segment", now=h.clock[0])


async def census(monkeypatch, recorded=LATER):
    monkeypatch.setattr(startup, "TONIGHT_FLAGS", ALL_ON)
    h = await startup.startup_harness(monkeypatch, with_deferred=True, recorded=recorded)
    all_on(h)
    h.bot.redis = h.service.redis
    original_submit = h.adapter.submit_order

    async def submit(request):
        # Simulated acceptances need distinct venue IDs just as distinct real orders do.
        return [replace(report, broker_order_id="SIMULATED-" + request.client_order_id)
                if report.event_type == "accepted" else report for report in await original_submit(request)]

    monkeypatch.setattr(h.adapter, "submit_order", submit)
    return h


@pytest.mark.asyncio
@pytest.mark.parametrize("outside", [False, True], ids=["in-window", "after-2000"])
@pytest.mark.parametrize("proof", [True, False], ids=["recorded-proof", "true-unknown"])
async def test_all_on_all14_startup_dispositions_one_buy_or_owned_no_saved_late_buy(monkeypatch, record_property, outside, proof):
    recorded = deepcopy(LATER)
    if not proof:
        for row in recorded["deferred_intents"]:
            if row["payload"].get("refusal_origin") == "skipped_before_submit":
                row["payload"]["refusal_origin"] = "broker"  # CONTROLLED removal of local proof, not a DB repair.
    h = await census(monkeypatch, recorded)
    if outside:
        h.clock[0] = datetime.fromisoformat("2026-10-06T00:05:00+00:00")
    await startup.real_bot_startup(monkeypatch, h)
    old = next(row["payload"]["old"] for row in recorded["tickets"] if row["id"].startswith("ff6464ff"))
    if proof:
        body = next(row["body"] for row in startup.BROKER["orders"] if row["body"]["status"] == "REJECTED")
        h.adapter.override = schwab_buy_readback(_request(old), body)
    with h.factory() as session:
        original_fills = [(row.id, row.order_id, row.quantity, row.price, deepcopy(row.payload))
                          for row in session.scalars(select(Fill))]
    await startup.real_oms_startup(monkeypatch, h)
    for state in h.strategy._symbol_states.values():
        price(h, state, "2.95")  # CONTROLLED eligible future tape, not the recorded market.
    for _ in range(5):
        await feedback(h)
    jobs = dict(HandoffJournal(h.factory).jobs())
    assert len(jobs) == 14
    record_property("dispositions", json.dumps([{ "token": str(token), "symbol": job["old"]["symbol"],
        "account": job["old"]["broker_account_name"], "before": next(row["payload"]["phase"]
            for row in recorded["tickets"] if row["id"] == str(token)), "after": job["phase"],
        "proof": old_buy_proven_clear(job), "owned": h.strategy._rpg_entry_owned(
            h.strategy.watchlist_state(job["old"]["symbol"]), account=job["old"]["broker_account_name"])}
        for token, job in jobs.items()]))
    for token, job in jobs.items():
        prefix = str(token)[:8]
        state = h.strategy.watchlist_state(job["old"]["symbol"])
        if prefix in LOCAL:
            assert job["phase"] == ("refused" if proof else "held_unknown")
            if proof:
                assert job["release_reason"] == "old_local_no_wire_return_to_strategy"
            assert old_buy_proven_clear(job) is proof
            assert h.strategy._rpg_entry_owned(state, account="live:orb") is (not proof)
            assert h.strategy._rpg_leg_owned(state, "live:orb") is (not proof)
        elif prefix == "ff6464ff":
            assert job["phase"] == ("refused" if proof else "held_unknown")
            assert old_buy_proven_clear(job) is proof
        elif prefix == "d86d5d38":
            assert job["phase"] == "filled" and job["replacement_filled"]
        else:
            assert job["phase"] == "refused" and old_buy_proven_clear(job)
    assert not h.adapter.opens  # Even in-window startup must not send a saved no-wire BUY.
    assert len({request.metadata["fanout_slot_id"] for request in h.adapter.opens}) == len(h.adapter.opens)
    assert all(request.broker_account_name == "live:orb" for request in h.adapter.opens)
    assert not h.adapter.cancels and len(h.adapter.reads) == 1
    state = h.strategy.watchlist_state("SCKT")
    assert state.cw_resting_taken
    h.strategy._cw_v2_resting_track(state, None)
    assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()
    with h.factory() as session:
        assert [(row.id, row.order_id, row.quantity, row.price, row.payload)
                for row in session.scalars(select(Fill))] == original_fills


AUTH_ROWS = [row for row in LATER["tickets"] if row["payload"].get("authorization")]
LOCAL_AUTH = [(next(row for row in LATER["tickets"] if row["id"].startswith(prefix)),
               next(row for row in AUTH_ROWS if row["id"].startswith(primary)))
              for prefix, primary in (("faa55c1f", "a007716c"), ("ee3d0d07", "7c7c5afb"), ("4be7cb2d", "c539a57f"))]
STAGES = [(row, row) for row in AUTH_ROWS] + LOCAL_AUTH


def wire_adapters(monkeypatch, h):
    class ReplayDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return h.clock[0].astimezone(tz or UTC)

    monkeypatch.setattr(webull_module, "datetime", ReplayDateTime)
    primary = SchwabBrokerAdapter(h.service.settings.model_copy(update={
        "schwab_token_store_path": None, "schwab_access_token": "SIMULATED", "schwab_refresh_token": None}),
        accounts_by_name={"live:schwab_1m_v2": SchwabAccountConfig("SIMULATED")})
    h.wires = []

    async def transport(method, path, *, body=None):
        assert method == "POST" and path == "/trader/v1/accounts/SIMULATED/orders"
        h.wires.append(deepcopy(body))
        return 201, {"Location": "/orders/SIMULATED-new"}, {}

    monkeypatch.setattr(primary, "_authorized_request_json", transport)
    client = webull_tests._FakeClient({"place": {"order_id": "SIMULATED-webull"}})
    mirror = WebullBrokerAdapter(h.service.settings, client=client,
        accounts_by_name={"live:orb": WebullAccountConfig("SIMULATED")})
    mirror._instrument_cache = {symbol: "SIMULATED-instrument" for symbol in ("APUS", "VEEA", "MI", "SCKT")}

    async def submit(request):
        return await (mirror if request.broker_account_name == "live:orb" else primary).submit_order(request)

    h.service.broker_adapter = SimpleNamespace(submit_order=submit,
        read_atr_resting_buy_after_cancel=h.adapter.read_atr_resting_buy_after_cancel)
    h.webull_client = client


async def stage(monkeypatch, row, auth_row, *, market=None):
    recorded = deepcopy(LATER)
    selected = deepcopy(row)
    job = selected["payload"]
    if job["phase"] != "held_unknown":
        assert old_buy_proven_clear(job)
        job.update(phase="clear", replacement_filled=False)  # CONTROLLED rewind to proven pre-placement stage.
        job.pop("authorization", None)
        job.pop("replacement", None)
    companion = next(item for item in LATER["tickets"] if item["payload"]["old"]["symbol"] == job["old"]["symbol"]
        and item["payload"]["old"]["broker_account_name"] == "live:schwab_1m_v2")
    recorded["tickets"] = [selected] if selected["id"] == companion["id"] else [selected, deepcopy(companion)]
    order_ids = {item["payload"].get("original_order_id") for item in recorded["tickets"]}
    recorded["orders"] = [item for item in recorded["orders"] if item["id"] in order_ids]
    recorded["fills"] = []  # Independent pre-fill replay; accounted census above retains the actual Fill.
    h = await census(monkeypatch, recorded)
    payload = auth_row["payload"]["authorization"]["event"]["payload"]
    md = payload["metadata"]
    h.clock[0] = datetime.fromtimestamp(auth_row["payload"]["authorization"]["at"], UTC)
    state = h.strategy.watchlist_state(job["old"]["symbol"])
    h.state = state
    state.fanout_segment_id = job["segment_id"]
    state.atr_short_flip_bar_ts = int(job["old"]["metadata"]["rpg_short_segment"])
    minute = h.clock[0].strftime("%H:%M")
    if (state.symbol, minute) in {("APUS", "13:32"), ("APUS", "14:27"), ("VEEA", "13:36")}:
        state.atr_trail = float(probe_at(state.symbol, minute)["fields"]["trail"])
    else:
        # MI/SCKT have stored four-decimal authorization, not retained full ATR tape.
        # Use a CONTROLLED line consistent with all three stored rounded fields.
        line, stop, limit = (float(md[key]) for key in ("cw_flip_level", "stop_price", "limit_price"))
        low = max(line - .00005, (stop - .00005) / 1.005, (limit - .00005) / 1.005 ** 2)
        high = min(line + .00005, (stop + .00005) / 1.005, (limit + .00005) / 1.005 ** 2)
        assert low < high
        state.atr_trail = (low + high) / 2
    state.bars.clear()
    close = state.atr_trail * .99
    state.bars.append(OHLCVBar(h.strategy._now_ms() - 60000, close, close, close, close, 50000))
    price(h, state, market if market is not None else close)
    token = UUID(selected["id"])
    if job["phase"] == "held_unknown":
        await h.service._rpg_advance(token)
        assert HandoffJournal(h.factory).read(token)["phase"] == "refused"
        assert HandoffJournal(h.factory).read(token)["release_reason"] == "old_local_no_wire_return_to_strategy"
        assert old_buy_proven_clear(HandoffJournal(h.factory).read(token))
    return h, token, md


@pytest.mark.asyncio
@pytest.mark.parametrize("row,auth_row", STAGES, ids=[row["id"][:8] for row, _ in STAGES])
async def test_all_on_recorded_authorization_stage_actual_adapter_wire_once_with_controlled_eligible_quote(
        monkeypatch, fake_sdk, row, auth_row):
    h, token, md = await stage(monkeypatch, row, auth_row)
    wire_adapters(monkeypatch, h)
    await feedback(h)
    job = HandoffJournal(h.factory).read(token)
    local = job.get("release_reason") == "old_local_no_wire_return_to_strategy"
    if local:
        assert job["phase"] == "refused" and not h.strategy._rpg_entry_owned(h.state, account="live:orb")
        assert not h.wires and not h.webull_client.calls.get("place", 0)
        h.strategy._queue_resting_place(h.state, h.state.atr_trail, slot="first")
        mirror, = h.strategy.drain_webull_direct_intents()
        h.strategy.drain_pending_intents()
        assert "rpg_handoff_token" not in mirror.metadata
        from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
        event = TradeIntentEvent(source_service="schwab-1m-v2", produced_at=h.clock[0],
            payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name="live:orb",
                symbol=h.state.symbol, side="buy", intent_type="open", quantity=mirror.quantity,
                reason=mirror.reason, metadata=mirror.metadata))
        await h.service._handle_stream_message({"data": event.model_dump_json()})
        actual, quantity = event.payload.metadata, event.payload.quantity
    else:
        assert job["phase"] == "placed"
        actual = job["authorization"]["event"]["payload"]["metadata"]
        quantity = Decimal(job["replacement"]["quantity"])
    assert (actual["stop_price"], actual["limit_price"], actual["cw_flip_level"]) == (
        md["stop_price"], md["limit_price"], md["cw_flip_level"])
    webull = row["payload"]["old"]["broker_account_name"] == "live:orb"
    assert actual["entry_notional_target_usd"] == ("300" if webull else "600")
    assert len(h.wires) == (0 if webull else 1)
    assert h.webull_client.calls.get("place", 0) == (1 if webull else 0)
    if webull:
        wire = h.webull_client.last["place"].values
        assert wire["side"] == "BUY"
        assert Decimal(wire["qty"]) == quantity
        assert (Decimal(wire["stop_price"]), Decimal(wire["limit_price"])) == tuple(
            Decimal(md[key]).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
            for key in ("stop_price", "limit_price"))
    else:
        wire, = h.wires
        assert (Decimal(str(wire["stopPrice"])), Decimal(str(wire["price"]))) == tuple(
            Decimal(md[key]).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
            for key in ("stop_price", "limit_price"))
        assert wire["orderLegCollection"][0]["instruction"] == "BUY"
        assert Decimal(str(wire["orderLegCollection"][0]["quantity"])) == quantity
    for _ in range(4):
        await feedback(h)
        await h.service._rpg_advance(token)
    assert len(h.wires) == (0 if webull else 1)
    assert h.webull_client.calls.get("place", 0) == (1 if webull else 0)
    assert not h.adapter.cancels


@pytest.mark.asyncio
async def test_all_on_actual_apus0932_distance_stays_no_wire_not_literal_both_placed(monkeypatch, fake_sdk):
    row = next(row for row in AUTH_ROWS if row["id"].startswith("bd6ac0b9"))
    source = next(row for row in LATER["deferred_intents"] if row["symbol"] == "APUS"
        and row["payload"]["metadata"]["stop_price"] == "5.2720")
    market = source["payload"]["metadata"]["webull_shape_market_price"]
    h, token, md = await stage(monkeypatch, row, row, market=market)
    wire_adapters(monkeypatch, h)
    await feedback(h)
    job = HandoffJournal(h.factory).read(token)
    assert market == "4.78" and md["stop_price"] == "5.2720"
    assert job["phase"] == "refused" and "webull_mirror_precheck_deferred" in job["replacement_reasons"]
    assert not h.wires and not h.webull_client.calls.get("place", 0)
    assert old_buy_proven_clear(job)
    await feedback(h)
    assert not h.strategy._rpg_entry_owned(h.state, account="live:orb")


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_all_on_first_reclaim_serial_cancel_proof_current_authorization_one_buy(monkeypatch, broker, slot):
    h = await runtime(monkeypatch, broker, slot=slot, notional=600,
        strategy_overrides={**ALL_ON, "strategy_schwab_1m_v2_entry_notional_usd": 600,
                            "strategy_schwab_1m_v2_webull_entry_notional_usd": 300})
    all_on(h)
    price(h, h.state, "3.06")
    token, _ = await begin(h, broker)
    assert old_buy_proven_clear(HandoffJournal(h.factory).read(token))
    await feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == "placed"
    assert len(h.adapter.opens) == len(h.adapter.reads) == len(h.adapter.cancels) == 1
    assert h.adapter.opens[0].metadata["cw_entry_slot"] == slot
    for _ in range(4):
        await feedback(h)
        await h.service._rpg_advance(token)
    assert len(h.adapter.opens) == 1


@pytest.mark.asyncio
async def test_all_on_recorded_apus1025_1026_1027_actual_strategy_schwab_rounding_chain(monkeypatch):
    from tests.unit import test_rpgstuck1_schwab_sequences as sequences
    original = sequences.wire_harness

    async def harness(*args, **kwargs):
        h = await original(*args, **kwargs)
        all_on(h)
        return h

    monkeypatch.setattr(sequences, "wire_harness", harness)
    await sequences.test_recorded_apus_1025_1026_1027_place_keep_reprice_to_schwab_wire(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["rejected-zero", "unknown"])
@pytest.mark.parametrize("eligibility", ["eligible", "already-claimed", "accepted-row", "no-rebuy"])
async def test_all_on_r20_recorded_runtime_edge_without_restart_one_probe_then_quiet(monkeypatch, answer, eligibility):
    from tests.unit.test_rpgstuck1_runtime_exhaustion import (
        test_r20_recorded_reto_runtime_exhaustion_wakes_one_proof_then_quiet,
    )
    monkeypatch.setattr(startup, "TONIGHT_FLAGS", ALL_ON)
    await test_r20_recorded_reto_runtime_exhaustion_wakes_one_proof_then_quiet(monkeypatch, answer, eligibility)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_evidence", [False, True], ids=["unknown", "unnotified-evidence"])
async def test_all_on_b5_real_loop180_quiet_turns_never_scans_unknown_without_wake(monkeypatch, late_evidence):
    from tests.unit.test_rpgstuck1_review_completion import (
        test_b5_startup_then180_empty_turns_never_reconciles_unknown_without_notification,
    )
    monkeypatch.setattr(startup, "TONIGHT_FLAGS", ALL_ON)
    await test_b5_startup_then180_empty_turns_never_reconciles_unknown_without_notification(monkeypatch, late_evidence)
