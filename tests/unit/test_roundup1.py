"""Recorded 179-order inputs; cache/bar/clock interleavings are controlled, not fills."""

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
from copy import deepcopy
from uuid import uuid5, NAMESPACE_URL

import pytest

from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy
from project_mai_tai.strategy_core.v2_entry_sizing import resting_buy_limit, resting_buy_stop
from scripts.roundup1_wire_replay import replay, service
from tests.unit.test_pmprint1_pmflip1 import armed, cross, REAL, STRAYS

FLAG = "strategy_schwab_1m_v2_resting_buy_round_up_enabled"
ROWS = json.loads((Path(__file__).parents[1] / "fixtures/roundup1/orders_179.json").read_text())["queries"]["orders"]
SCKT = next(r for r in ROWS if r["client_order_id"] == "schwab_1m_v2-SCKT-open-9a10bc5b0102")
ACTIVE_FLAGS = {
    "strategy_schwab_1m_v2_pm_print_ask_confirm_enabled": True,
    "strategy_schwab_1m_v2_pm_flip_wait_enabled": True,
    "strategy_schwab_1m_v2_pm_rest_reprice_enabled": True,
    "strategy_schwab_1m_v2_atr_reprice_handoff_enabled": True,
    "oms_v2_webull_mirror_fresh_price_enabled": True,
    "strategy_schwab_1m_v2_gap_hold_enabled": True,
}


@pytest.fixture(autouse=True)
def regular(monkeypatch):
    from project_mai_tai.oms import service as oms
    monkeypatch.setattr(oms, "_is_regular_market_session", lambda now=None: True)


def strategy_for(row=SCKT, *, enabled=True):
    strategy = SchwabV2Strategy(Settings(_env_file=None, **{
        **ACTIVE_FLAGS, FLAG: enabled, "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
        "strategy_schwab_1m_v2_dual_broker_fanout_enabled": True,
        "strategy_schwab_1m_v2_webull_resting_mirror_enabled": True,
        "strategy_schwab_1m_v2_flip_owned_first_entry_enabled": False,
        "strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct": 0.5,
        "strategy_schwab_1m_v2_account_name": "live:schwab_1m_v2",
        "strategy_schwab_1m_v2_webull_account_name": "live:orb",
        "oms_v2_emit_native_oco_bracket_enabled": True,
    }))
    now = int(datetime.fromisoformat(row["submitted_at"]).timestamp() * 1000)
    strategy._now_ms = lambda: now
    window = strategy._resting_in_window
    strategy._resting_in_window = lambda current=None: window(current or datetime.fromtimestamp(now / 1000, UTC))
    strategy._entries_held = False
    strategy._resting_session_is_eh = lambda now=None: False
    state = strategy.watchlist_state(row["symbol"])
    state.fanout_segment_id = state.atr_short_flip_bar_ts = now - 600_000
    state.atr_state, state.atr_state_age = "short", 10
    state.atr_trail = float(row["payload"]["cw_flip_level"])
    state.bars.append(OHLCVBar(now - 60_000, 1.06, 1.06, 1.06, 1.06, 10_000))
    return strategy, state


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r["client_order_id"])
def test_recorded_orders_off_keeps_wire_quantity_and_bracket(row):
    old = replay(row)
    md = row["payload"]
    assert old["shares"] == int(Decimal(row["quantity"]))
    assert old["target"] == md.get("bracket_target_price", "N/A")
    assert old["protection"] == md.get("bracket_stop_price", "N/A")
    if row["order_type"].lower() == "limit":
        assert Decimal(old["limit"]) == Decimal(md["limit_price"])
        assert old["stop"] == "N/A (LIMIT)"
    elif row["account"] == "live:schwab_1m_v2":
        assert Decimal(old["stop"]) == Decimal(md["stop_price"])
        assert Decimal(old["limit"]) == Decimal(md["limit_price"])
    else:
        stop = Decimal(md["stop_price"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        limit = Decimal(md["limit_price"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert Decimal(old["stop"]) == stop
        assert Decimal(old["limit"]) == max(limit, stop + Decimal("0.01"))


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r["client_order_id"])
def test_recorded_orders_on_never_rounds_trigger_down_and_sizes_wire_limit(row):
    new = replay(row, enabled=True)
    assert "refused" not in new
    raw = Decimal(row["payload"]["entry_price"])
    assert Decimal(new["trigger"]) >= raw
    if row["order_type"].lower() != "limit":
        assert Decimal(new["stop"]) == Decimal(new["trigger"])
        assert Decimal(new["limit"]) > Decimal(new["stop"])
    amount = Decimal(row["payload"].get("entry_notional_target_usd", "0"))
    if amount:
        assert new["shares"] == min(1000, max(1, int((amount / Decimal(new["limit"])).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP))))
    else:
        assert new["shares"] == int(Decimal(row["quantity"]))


@pytest.mark.parametrize("raw,expected", [
    ("1.0636", "1.07"), ("4.5100", "4.51"), ("1.0000", "1.00"),
    ("0.8512", "0.8512"), ("0.99999", "1.00"), ("1.00001", "1.01"),
])
def test_decimal_entry_tick_edges_and_crossing_dollar(raw, expected):
    assert resting_buy_stop(Decimal(raw)) == Decimal(expected)


@pytest.mark.parametrize("raw,expected", [(4.510000001, 4.51), (1.06359, 1.07)])
def test_existing_four_decimal_format_precedes_ceiling(raw, expected):
    strategy, _ = strategy_for()
    strategy._resting_trigger_offset_pct = 0
    assert strategy._resting_trigger_for_line(raw) == expected


@pytest.mark.parametrize("slot", ["first", "reclaim"])
def test_sckt_strategy_to_both_wire_limits_sizes_from_108(slot):
    strategy, state = strategy_for()
    strategy._queue_resting_place(state, 1.0583, slot=slot)
    primary, = strategy.drain_pending_intents()
    mirror, = strategy.drain_webull_direct_intents()
    assert state.resting_trigger == state.resting_schwab_wire_stop == state.resting_webull_wire_stop == 1.07
    assert state.resting_wire_cap == 1.08
    for draft, shares in ((primary, 556), (mirror, 278)):
        assert Decimal(draft.metadata["stop_price"]) == Decimal("1.07")
        assert Decimal(draft.metadata["limit_price"]) == Decimal("1.08")
        assert Decimal(draft.metadata["entry_price"]) == Decimal(draft.metadata["reference_price"]) == Decimal("1.07")
        assert draft.quantity == shares
        assert Decimal(draft.metadata["entry_size_price"]) == Decimal("1.08")


def test_pfsa_bracket_uses_the_single_sent_trigger_not_legacy_exit_reference():
    row = next(r for r in ROWS if r["symbol"] == "PFSA" and r["payload"].get("entry_price") == "3.7736")
    old, new = replay(row), replay(row, enabled=True)
    assert (old["target"], old["protection"]) == ("3.96", "3.47")
    assert (new["stop"], new["target"], new["protection"]) == ("3.78", "3.97", "3.48")


@pytest.mark.parametrize("leg", ["schwab", "webull"])
def test_recorded_sckt_strategy_oms_adapter_wire_is_ceiling_not_nearest(leg):
    from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
    from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
    strategy, state = strategy_for()
    strategy._queue_resting_place(state, float(SCKT["payload"]["cw_flip_level"]))
    primary, = strategy.drain_pending_intents()
    mirror, = strategy.drain_webull_direct_intents()
    draft = primary if leg == "schwab" else mirror
    account = "live:schwab_1m_v2" if leg == "schwab" else "live:orb"
    event = TradeIntentEvent(source_service="recorded-sckt-wire-replay", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=account,
        symbol=state.symbol, side=draft.side, intent_type=draft.intent_type,
        quantity=Decimal(draft.quantity), reason=draft.reason, metadata=deepcopy(draft.metadata)))
    service(True)._apply_v2_oco_bracket_entry(event=event)
    request = OrderRequest(client_order_id="sckt-recorded-price-replay", strategy_code="schwab_1m_v2",
        broker_account_name=account, symbol=state.symbol, side="buy", intent_type="open",
        quantity=event.payload.quantity, reason=draft.reason, order_type="STOP_LIMIT", metadata=event.payload.metadata)
    if leg == "schwab":
        wire = object.__new__(SchwabBrokerAdapter)._build_order_payload(request)
        stop, limit = Decimal(str(wire["stopPrice"])), Decimal(str(wire["price"]))
        assert event.payload.metadata["bracket_target_price"] == "1.12"
        assert event.payload.metadata["bracket_stop_price"] == "0.9844"
    else:
        limit, stop, _, refusal = WebullBrokerAdapter._prepare_single_leg_prices(
            request=request, order_type="STOP_LIMIT", limit_price=Decimal(request.metadata["limit_price"]),
            stop_price=Decimal(request.metadata["stop_price"]))
        assert refusal is None
    assert (stop, limit) == (Decimal("1.07"), Decimal("1.08"))
    assert service(True)._rpg_canonical_prices(request.metadata, request.broker_account_name) == (stop, limit)


@pytest.mark.parametrize("stream", [True, False])
def test_recorded_meds_print_below_ceiling_cannot_cross_or_take_slot(stream):
    case = REAL[11]
    strategy, state, clock = armed(case, **{**ACTIVE_FLAGS, FLAG: True})
    state.resting_active = False
    strategy._queue_resting_place(state, state.resting_level)
    assert cross(strategy, state, clock, case[3], case[4], stream=stream) is None
    assert state.resting_active and state.resting_flip_ms == 0
    assert not strategy.drain_webull_fanout_intents()


@pytest.mark.parametrize("leg", ["schwab", "webull"])
def test_five_dollar_line_has_503_wire_and_cent_exact_is_unchanged(leg):
    strategy, state = strategy_for()
    strategy._queue_resting_place(state, 5.00)
    draft, = strategy.drain_pending_intents() if leg == "schwab" else strategy.drain_webull_direct_intents()
    assert Decimal(draft.metadata["stop_price"]) == Decimal("5.03")
    assert strategy._resting_trigger_for_line(4.51 / 1.005) == 4.51


def test_recorded_sckt_1247_1257_tape_never_reaches_new_stop():
    raw = json.loads((Path(__file__).parents[2] / "docs/review-artifacts/roundup1/SCKT_AND_PFSA_EVIDENCE.json").read_text())
    tape, = raw["queries"]["sckt_tape"]
    new = replay(SCKT, enabled=True)
    assert tape["n"] == 262 and tape["at_rule_or_above"] == 0
    assert Decimal(tape["max_price"]) == Decimal("1.06") < Decimal(new["stop"]) == Decimal("1.07")
    assert Decimal(new["limit"]) == Decimal("1.08")


@pytest.mark.parametrize("leg", ["schwab", "webull"])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
def test_recorded_sckt_reprice_authorization_recomputes_ceiling_and_matches_oms(leg, slot):
    from project_mai_tai.market_data.schwab_v2_rest_client import Quote
    strategy, state = strategy_for()
    md = deepcopy(SCKT["payload"])
    md["rpg_short_segment"] = str(state.atr_short_flip_bar_ts)
    account = "live:orb" if leg == "webull" else "live:schwab_1m_v2"
    state.last_quote = Quote(state.symbol, 1.0, 1.0, 1.0, strategy._now_ms())
    # Controlled eligibility around SCKT's recorded old/new reprice geometry.
    # No invented tape or historical partial-fill claim.
    state.bars[-1] = OHLCVBar(strategy._now_ms() - 60000, 1.0, 1.0, 1.0, 1.0, 100000)
    if slot == "reclaim":
        strategy._reactive_entry_enabled = True
        state.cw_armed, state.cw_bars_waited = True, 2
        state.cw_segment_high = float(md["cw_flip_level"])
    job = {"phase": "clear", "slot": slot, "segment_id": state.fanout_segment_id,
        "cleared_at": datetime.fromtimestamp(strategy._now_ms() / 1000).isoformat(),
        "old": {"symbol": state.symbol, "broker_account_name": account, "metadata": md}}
    auth = strategy.rpg_handoff_authorization("recorded-sckt-reprice", job)
    assert auth["verdict"] == "ready", auth
    wire_md = auth["event"]["payload"]["metadata"]
    assert (Decimal(wire_md["stop_price"]), Decimal(wire_md["limit_price"])) == (Decimal("1.07"), Decimal("1.08"))
    assert service(True)._rpg_canonical_prices(wire_md, account) == (Decimal("1.07"), Decimal("1.08"))
    changed = {**wire_md, "stop_price": "1.08", "limit_price": "1.09"}
    assert service(True)._rpg_canonical_prices(changed, account) != service(True)._rpg_canonical_prices(wire_md, account)
    assert Decimal(auth["event"]["payload"]["quantity"]) == (556 if leg == "schwab" else 278)


@pytest.mark.asyncio
async def test_recorded_sckt_pa1_resubmit_recomputes_ceiling_then_serial_lane_sizes_wire(monkeypatch):
    from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
    from tests.unit.test_oms_webull_mirror_deferred_resubmit import (
        _integrated_service, _session_factory,
    )
    factory = _session_factory()
    svc, adapter = _integrated_service(factory, enabled=True, nfq_enabled=True)
    monkeypatch.setattr(svc, "_market_is_fillable", lambda now=None: True)
    svc.settings = svc.settings.model_copy(update={**ACTIVE_FLAGS, FLAG: True,
        "strategy_schwab_1m_v2_webull_entry_notional_usd": Decimal("300")})
    md = deepcopy(SCKT["payload"])
    # Recorded SCKT prices in a controlled PA1 interleaving, not a claim that
    # this already-accepted SCKT order was historically deferred at this time.
    for key in ("rpg_handoff_token", "rpg_event_id", "nfq_retry_token", "nfq_hold_id"):
        md.pop(key, None)
    md.update(webull_shape_market_price="0.97", webull_shape_market_source="ask",
        webull_shape_market_at_utc=datetime.now(UTC).isoformat(), webull_shape_market_max_age_ms="10000")
    event = TradeIntentEvent(source_service="recorded-sckt-pa1-replay", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name="live:orb", symbol="SCKT",
        side="buy", intent_type="open", quantity=Decimal("280"), reason="ATR Flip", metadata=md))
    assert svc._defer_webull_resting_mirror_before_submit(event)
    svc._latest_quotes_by_symbol["SCKT"] = {"ask": Decimal("1.0"), "received_at": datetime.now(UTC)}
    await svc._evaluate_webull_mirror_deferred_resubmits("SCKT")
    retry, = [TradeIntentEvent.model_validate(data) for _, data in svc.redis.entries]
    await svc.process_trade_intent(retry)
    request, = adapter.requests
    assert (Decimal(request.metadata["stop_price"]), Decimal(request.metadata["limit_price"])) == (
        Decimal("1.07"), Decimal("1.08"))
    assert request.quantity == Decimal("278")
    await svc._evaluate_webull_mirror_deferred_resubmits("SCKT")
    assert len(adapter.requests) == 1


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("case,expected", [
    (REAL[10], True), (REAL[11], False), (REAL[12], True), (REAL[13], True),
    (STRAYS[0], False), (("MI", "2026-10-05T13:15:20.309Z", 2.7288, 2.749, 2.75), True),
], ids=["LGHL", "MEDS", "NXL", "AMOD", "SAIQ", "MI"])
def test_six_pm_print_proxies_compare_to_rounded_trigger(case, expected, stream):
    strategy, state, clock = armed(case, **{**ACTIVE_FLAGS, FLAG: True})
    state.resting_active = False
    strategy._queue_resting_place(state, state.resting_level)
    assert not state.resting_is_broker_order
    assert strategy._active_resting_trigger(state) == {
        "LGHL": 7.02, "MEDS": 4.65, "NXL": 7.15, "AMOD": 2.57, "SAIQ": 6.80, "MI": 2.73,
    }[case[0]]
    result = cross(strategy, state, clock, case[3], case[4], stream=stream)
    if case[0] == "MI" and stream:
        expected = False  # 2.75 ask still exceeds the new final 2.73/2.74 pair.
    assert (result is not None) is expected
    if expected:
        assert Decimal(result.metadata["stop_price"]) == Decimal(str(state.resting_trigger))
        mirror, = strategy.drain_webull_fanout_intents()
        assert mirror.metadata["resting_wire_stop_price"] == result.metadata["resting_wire_stop_price"]
    else:
        assert not state.resting_flip_ms
        assert not strategy.drain_webull_fanout_intents()


def test_switch_on_alone_preserves_working_sckt_wire_quantity_and_no_buy():
    strategy, state = strategy_for(enabled=False)
    strategy._queue_resting_place(state, 1.0583)
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    # Recorded actual accepted wire, not a line-based recomputation.
    state.resting_schwab_wire_stop = state.resting_webull_wire_stop = 1.06
    state.resting_wire_cap = 1.07
    state.resting_webull_quantity = 280
    before = asdict(state)
    strategy.settings = strategy.settings.model_copy(update={FLAG: True})
    assert strategy._active_resting_trigger(state) == 1.06
    assert strategy._active_resting_cap(state) == 1.07
    assert asdict(state) == before
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
    strategy._queue_resting_cancel(state, reason="reprice")
    mirror, = strategy.drain_webull_direct_intents()
    assert mirror.quantity == 280  # Never re-size a cancel after switch-on.


@pytest.mark.parametrize("leg", ["schwab", "webull"])
def test_small_band_existing_pair_lift_and_dollar_representation(leg):
    # Adversarial boundaries; no sub-dollar historical order exists in the census.
    assert resting_buy_limit(Decimal("1.00"), 0.01, leg=leg) == Decimal("1.01")
    assert str(resting_buy_limit(Decimal("1.00"), 0.5, leg=leg)) == "1.01"
    if leg == "schwab":
        from tests.unit.test_oms_schwab_stop_limit_tick_lift import _wire
        md, _ = _wire("1.0000", "1.0050")
        assert md["stop_price"] == "1.0000"
    else:
        request = OrderRequest(client_order_id="boundary", broker_account_name="live:orb", strategy_code="schwab_1m_v2",
            symbol="BOUNDARY", side="buy", intent_type="open", quantity=Decimal(300), reason="boundary", metadata={})
        limit, stop, _, refusal = WebullBrokerAdapter._prepare_single_leg_prices(request=request,
            order_type="STOP_LIMIT", limit_price=Decimal("1.01"), stop_price=Decimal("1.00"))
        assert refusal is None and str(stop) == "1.00" and str(limit) == "1.01"


def test_oms_no_chase_reads_final_wire_cap_not_raw_formula(monkeypatch):
    svc = service(True)
    svc._fresh_ask = lambda symbol, max_age_ms: 1.079
    # SCKT final 1.07/1.08 pair admits the quote that exceeds 1.07535's raw band.
    result = svc._band_capped_marketable_limit(symbol="SCKT", level=1.07, band_pct=0.5,
        max_age_ms=2000, wire_cap=Decimal("1.08"))
    assert result == ("1.07", 1.079, 1.08)
    svc._fresh_ask = lambda symbol, max_age_ms: 1.0801
    assert svc._band_capped_marketable_limit(symbol="SCKT", level=1.07, band_pct=0.5,
        max_age_ms=2000, wire_cap=Decimal("1.08"))[1] == "ASK_PAST_BAND"


def test_flag_rollback_default_and_catalog_requires_on_at_deploy():
    assert getattr(Settings(_env_file=None), FLAG) is False
    catalog = json.loads((Path(__file__).parents[2] / "ops/health/expected_flags.json").read_text())
    assert next(r for r in catalog["flags"] if r["name"] == FLAG)["expected"] is True


@pytest.mark.parametrize("leg,with_proof", [("webull", True), ("webull", False), ("schwab", True)])
@pytest.mark.parametrize("post_flag", [False, True], ids=["pre_flag_wire", "post_flag_wire"])
def test_restart_restores_recorded_accepted_wire_not_new_calculation_and_never_duplicates(leg, with_proof, post_flag):
    from project_mai_tai.db.models import BrokerOrderEvent, DashboardSnapshot
    from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
    from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, SNAPSHOT_TYPE
    from tests.unit.test_oms_webull_mirror_deferred_resubmit import _integrated_service, _session_factory
    row = SCKT if leg == "webull" else next(r for r in ROWS if r["symbol"] == "PFSA"
        and r["payload"].get("entry_price") == "3.7736")
    factory = _session_factory()
    svc, _ = _integrated_service(factory, enabled=True)
    strategy, state = strategy_for(row)
    token = uuid5(NAMESPACE_URL, row["client_order_id"])
    md = deepcopy(row["payload"])
    new = replay(row, enabled=True)
    if post_flag:
        # Controlled acceptance/restart of the new wire built from the recorded
        # order's geometry, not a claim that ROUNDUP was historically enabled.
        md = deepcopy(new["metadata"])
    quantity = Decimal(new["shares"]) if post_flag else Decimal(row["quantity"])
    md.setdefault("rpg_resting_generation", str(token))  # Controlled ticket around the pre-RPG recorded wire.
    job = {"revision": 0, "phase": "placed", "slot": "first", "segment_id": state.fanout_segment_id,
           "old": {"symbol": row["symbol"], "broker_account_name": row["account"], "metadata": md},
           "replacement": {"client_order_id": row["client_order_id"], "metadata": md,
                           "quantity": str(quantity)}}
    event = TradeIntentEvent(source_service="recorded-accepted-replay", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=row["account"], symbol=row["symbol"],
        side="buy", intent_type="open", quantity=quantity, reason="ATR Flip", metadata=md))
    with factory() as session:
        st = svc.store.ensure_strategy(session, "schwab_1m_v2", name="v2")
        account = svc.store.ensure_broker_account(session, row["account"], provider=leg, environment="test")
        intent = svc.store.create_trade_intent(session, strategy=st, broker_account=account, event=event)
        order = svc.store.get_or_create_order(session, intent=intent, strategy_id=st.id, broker_account_id=account.id,
            client_order_id=row["client_order_id"], broker_order_id=row["broker_order_id"], symbol=row["symbol"],
            side="buy", quantity=quantity, metadata=md, status="accepted",
            order_type="STOP_LIMIT", time_in_force="day")
        if leg == "webull" and with_proof:
            capture = json.loads((Path(__file__).parents[1] / "fixtures/roundup1/orders_179.json").read_text())
            recorded = next(e for e in capture["queries"]["sckt_events"] if e["event_type"] == "accepted")
            report = deepcopy(recorded["payload"])
            if post_flag:
                report["metadata"].update(webull_wire_stop_price=new["stop"], webull_wire_limit_price=new["limit"])
            session.add(BrokerOrderEvent(order_id=order.id, event_type="accepted", event_source="broker",
                payload=report))
        session.add(DashboardSnapshot(id=token, snapshot_type=SNAPSHOT_TYPE, payload=job))
        session.commit()
    journal = HandoffJournal(factory)
    job = journal.reconcile_feedback(token, job, include_wire_prices=True)
    result = strategy.rpg_handoff_authorization(str(token), job)
    if with_proof:
        expected = float(new["stop"]) if post_flag else (1.06 if leg == "webull" else 3.77)
        assert strategy._active_resting_trigger(state, leg=leg) == expected
        expected_limit = float(new["limit"]) if post_flag else (1.07 if leg == "webull" else 3.79)
        assert strategy._active_resting_cap(state, leg=leg) == expected_limit
        assert (state.resting_webull_quantity if leg == "webull" else state.resting_schwab_quantity) == int(quantity)
        if not post_flag:
            assert expected != strategy._resting_trigger_for_line(float(md["cw_flip_level"]))
    else:
        assert result["reason"] == "placed_wire_price_unproven"
        assert strategy._rpg_entry_owned(state) and strategy._rpg_leg_owned(state, row["account"])
        assert not strategy._rpg_leg_owned(state, "live:schwab_1m_v2")
        strategy._queue_resting_place(state, float(md["cw_flip_level"]))
        primary, = strategy.drain_pending_intents()
        assert primary.side == "buy" and primary.intent_type == "open"
        assert state.resting_webull_quantity == 0
        assert not strategy.drain_webull_direct_intents()
        return
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()


def test_subdollar_default_dollars_still_meet_webull_hundred_share_minimum():
    strategy, state = strategy_for()
    strategy._queue_resting_place(state, 0.9950)
    mirror, = strategy.drain_webull_direct_intents()
    assert Decimal(mirror.metadata["stop_price"]) == Decimal("1.0000")
    assert Decimal(mirror.metadata["limit_price"]) == Decimal("1.01")
    assert mirror.quantity == 297 and mirror.quantity >= 100


@pytest.mark.parametrize("wire", [None, {}, {"stop_price": "NaN", "limit_price": "1.07"},
    {"stop_price": "1.07", "limit_price": "1.07"}])
def test_unproven_webull_wire_blocks_only_its_leg_and_preserves_primary_admission(wire):
    strategy, state = strategy_for()
    account = "live:orb"
    strategy._rpg_handoffs["recorded-sckt-restart"] = {
        "phase": "placed", "old": {"symbol": state.symbol, "broker_account_name": account},
        "replacement_wire_prices": wire,
    }
    assert strategy._rpg_entry_owned(state) and strategy._rpg_leg_owned(state, account)
    assert not strategy._rpg_leg_owned(state, "live:schwab_1m_v2")
    webull_generation = state.resting_webull_generation
    strategy._queue_resting_place(state, 1.0583)
    primary, = strategy.drain_pending_intents()
    assert primary.side == "buy" and primary.intent_type == "open"
    assert state.resting_webull_generation == webull_generation
    assert state.resting_webull_quantity == 0
    assert not strategy.drain_webull_direct_intents()


@pytest.mark.parametrize("wire", [None, {}, {"stop_price": "NaN", "limit_price": "3.79"}])
def test_unproven_primary_wire_blocks_only_its_leg_and_preserves_webull_admission(wire):
    strategy, state = strategy_for()
    account = "live:schwab_1m_v2"
    strategy._rpg_handoffs["recorded-pfsa-restart"] = {
        "phase": "placed", "old": {"symbol": state.symbol, "broker_account_name": account},
        "replacement_wire_prices": wire,
    }
    assert strategy._rpg_entry_owned(state, account=account)
    assert not strategy._rpg_entry_owned(state, account="live:orb")
    primary_generation = state.resting_schwab_generation
    strategy._queue_resting_place(state, 1.0583)
    assert not strategy.drain_pending_intents()
    mirror, = strategy.drain_webull_direct_intents()
    assert mirror.side == "buy" and mirror.intent_type == "open"
    assert state.resting_schwab_generation == primary_generation
    assert state.resting_schwab_quantity == 0


@pytest.mark.parametrize("webull", [True, False])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
def test_partial_fill_feedback_never_resizes_or_rebuys_remainder(webull, slot):
    strategy, state = strategy_for()
    account = "live:orb" if webull else "live:schwab_1m_v2"
    # Controlled partial-fill interleaving around recorded SCKT's price/quantity.
    # This is not a claim that SCKT actually partially filled.
    state.position_qty_held = 1
    job = {"phase": "placed", "slot": slot, "segment_id": state.fanout_segment_id,
           "old": {"symbol": state.symbol, "broker_account_name": account, "metadata": SCKT["payload"]},
           "replacement_filled": True, "no_rebuy": True}
    strategy.rpg_handoff_authorization("sckt-controlled-partial", job)
    assert state.cw_resting_taken if slot == "first" else state.cw_reclaim_taken
    assert not state.resting_active
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()


def test_reprice_threshold_crossed_but_proven_wire_pair_unchanged_sends_nothing():
    strategy, state = strategy_for()
    # Recorded GIPR 09-18 trigger 1.2166. The 0.51% line move is a controlled
    # boundary, not a claim that this unchanged-wire sequence was recorded.
    old_line = 1.2166 / 1.005
    strategy._queue_resting_place(state, old_line)
    assert state.resting_trigger == 1.22 and state.resting_wire_cap == 1.23
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    qty = state.resting_schwab_quantity, state.resting_webull_quantity
    generation = state.resting_schwab_generation, state.resting_webull_generation
    new_line = old_line * (1 - 0.0051)
    assert abs(new_line - old_line) / old_line > 0.005
    strategy._reprice_resting(state, new_line)
    assert state.resting_active and state.resting_trigger == 1.22
    assert (state.resting_schwab_quantity, state.resting_webull_quantity) == qty
    assert (state.resting_schwab_generation, state.resting_webull_generation) == generation
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()


def test_rounded_stop_at_or_below_current_ask_cannot_place(monkeypatch):
    from project_mai_tai.market_data.schwab_v2_rest_client import Quote
    strategy, state = strategy_for()
    # SCKT recorded fill price 1.06 is below the new 1.07 stop. The equality
    # boundary is controlled; it must remain non-placeable, without an epsilon.
    state.last_quote = Quote(state.symbol, 1.06, 1.07, 1.06, strategy._now_ms())
    assert not strategy._resting_stop_ask_allows(state, 1.0583, slot="first")
    state.last_quote = Quote(state.symbol, 1.06, 1.06, 1.06, strategy._now_ms())
    assert strategy._resting_stop_ask_allows(state, 1.0583, slot="first")
