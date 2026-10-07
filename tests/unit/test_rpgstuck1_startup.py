"""Recorded restart ownership; network, future tape and broker answers are simulated.

PMPRINT's implementation is an unmerged composition dependency. Its exact flag
values are carried here, without pretending the standalone RPG source contains it.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import delete, select

from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback, schwab_buy_readback
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, Fill, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, SNAPSHOT_TYPE, _request, old_buy_proven_clear, replacement_terminal_zero
from project_mai_tai.services import schwab_1m_v2_bot as bot_module
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from tests.unit.test_rpg1_runtime import feedback, runtime
from tests.unit.t43_recorded_audit_support import AUDITS, seed_recorded_abort, seed_recorded_intent


TONIGHT_FLAGS = {
    "strategy_schwab_1m_v2_pm_print_ask_confirm_enabled": True,
    "strategy_schwab_1m_v2_pm_flip_wait_enabled": False,
    "strategy_schwab_1m_v2_pm_rest_reprice_enabled": False,
    "strategy_schwab_1m_v2_atr_reprice_handoff_enabled": False,
    "oms_v2_webull_mirror_fresh_price_enabled": True,
    "strategy_schwab_1m_v2_gap_hold_enabled": True,
}

RECORDED = json.loads((Path(__file__).parents[1] / "fixtures/rpgstuck1_startup.json").read_text())
BROKER = json.loads((Path(__file__).parents[1] / "fixtures/rpgstuck1_startup_broker.json").read_text())
DISPOSITIONS = {
    "fbfd692d": (True, False),
    "bd6ac0b9": (True, True),  # This ticket cleared; the later same-account ticket still owns APUS.
    "a007716c": (True, False),
    "faa55c1f": (False, True),
    "7c7c5afb": (True, False),
    "ee3d0d07": (False, True),
    "ff6464ff": (False, True),
    "ba108172": (False, True),  # Eighth ticket discovered in the fresh census.
}


async def startup_harness(monkeypatch, *, with_deferred=False, deferred_rows=None, recorded=RECORDED):
    h = await runtime(monkeypatch, "schwab", notional=600)
    h.recorded = recorded
    h.adapter.override = AtrBuyReadback("unknown", "CONTROLLED strict detail unavailable")
    set_tonight_flags(h)
    settings = h.service.settings.model_copy(update={
        "strategy_schwab_1m_v2_enabled": True,
        "strategy_schwab_1m_v2_tick_capture_enabled": False,
        "strategy_schwab_1m_v2_streamer_enabled": False,
        "market_data_subscription_startup_enabled": False,
        "strategy_schwab_1m_v2_webull_entry_notional_usd": 300,
    })
    h.service.settings = settings
    h.bot = bot_module.SchwabV2BotService(settings, session_factory=h.factory)
    h.strategy = h.bot.strategy
    h.clock[0] = datetime.fromisoformat(recorded["read_at"])
    monkeypatch.setattr(h.strategy, "_now_ms", lambda: int(h.clock[0].timestamp() * 1000))
    for name in ("_resting_in_window", "_resting_session_is_eh", "_entry_window_closed_for_session"):
        method = getattr(h.strategy, name)
        monkeypatch.setattr(h.strategy, name, lambda now=None, fn=method: fn(now or h.clock[0]))
    for symbol in sorted({row["payload"]["old"]["symbol"] for row in recorded["tickets"]}):
        job = max((row["payload"] for row in recorded["tickets"]
                   if row["payload"]["old"]["symbol"] == symbol and
                   row["payload"]["old"]["broker_account_name"] == "live:schwab_1m_v2"),
                  key=lambda job: job["created_at"])
        state = h.strategy.watchlist_state(symbol)
        state.atr_state, state.atr_state_age = "short", 31
        state.fanout_segment_id = job["segment_id"]
        state.atr_short_flip_bar_ts = int(job["old"]["metadata"]["rpg_short_segment"])
        # Today's later line, bar and quote are controlled inputs, not recorded tape.
        state.atr_trail = 3.05
        state.bars.append(OHLCVBar(h.strategy._now_ms() - 60000, 2.9, 3, 2.9, 2.95, 50000))
        state.last_quote = Quote(symbol, 2.94, 2.95, 2.95, h.strategy._now_ms())
    h.state = h.strategy.watchlist_state("APUS")
    with h.factory() as session:
        # Empty replay setup, before restoration. No ticket/order is purged afterward.
        session.execute(delete(BrokerOrder))
        session.execute(delete(TradeIntent))
        for row in recorded["tickets"]:
            session.add(DashboardSnapshot(id=UUID(row["id"]), snapshot_type=SNAPSHOT_TYPE,
                                          payload=deepcopy(row["payload"])))
        for row in recorded["orders"]:
            strategy = h.service.store.ensure_strategy(session, row["strategy"], name="v2")
            account = h.service.store.ensure_broker_account(session, row["account"],
                provider="schwab" if row["account"] == "live:schwab_1m_v2" else "webull", environment="test")
            event = TradeIntentEvent(source_service="schwab-1m-v2", payload=TradeIntentPayload(
                strategy_code=row["strategy"], broker_account_name=row["account"], symbol=row["symbol"],
                side="buy", intent_type="open", quantity=Decimal(row["quantity"]),
                reason="Recorded startup old BUY", metadata=deepcopy(row["payload"])))
            intent = h.service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
            order = h.service.store.get_or_create_order(session, intent=intent, strategy_id=strategy.id,
                broker_account_id=account.id, client_order_id=row["client_order_id"],
                broker_order_id=row["broker_order_id"], symbol=row["symbol"], side="buy",
                quantity=Decimal(row["quantity"]), metadata=deepcopy(row["payload"]),
                status=row["status"], order_type="STOP_LIMIT", time_in_force="day")
            order.id = UUID(row["id"])
        if with_deferred:
            for row in recorded["deferred_intents"] if deferred_rows is None else deferred_rows:
                strategy = h.service.store.ensure_strategy(session, row["strategy"], name="v2")
                account = h.service.store.ensure_broker_account(session, row["account"], provider="webull", environment="test")
                event = TradeIntentEvent(event_id=UUID(row["payload"]["event_id"]),
                    source_service=row["payload"]["source_service"], payload=TradeIntentPayload(
                        strategy_code=row["strategy"], broker_account_name=row["account"],
                        symbol=row["symbol"], side="buy", intent_type="open", quantity=Decimal(row["quantity"]),
                        reason=row["reason"], metadata=deepcopy(row["payload"]["metadata"])))
                intent = h.service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
                intent.id, intent.status, intent.payload = UUID(row["id"]), row["status"], deepcopy(row["payload"])
                intent.created_at = datetime.fromisoformat(row["created_at"])
        session.flush()
        for row in recorded.get("fills", []):
            order = session.get(BrokerOrder, UUID(row["order_id"]))
            session.add(Fill(id=UUID(row["id"]), order_id=order.id,
                strategy_id=order.strategy_id, broker_account_id=order.broker_account_id,
                broker_fill_id=row["broker_fill_id"], symbol=row["symbol"], side=row["side"],
                quantity=Decimal(row["quantity"]), price=Decimal(row["price"]),
                filled_at=datetime.fromisoformat(row["filled_at"]), payload=deepcopy(row["payload"])))
        session.commit()
    for row in recorded["tickets"]:
        job = row["payload"]
        event_id = job.get("replacement", {}).get("metadata", {}).get("rpg_event_id")
        if any(audit["payload"]["event_id"] == event_id for audit in AUDITS["intents"]):
            seed_recorded_abort(h, job)
    assert not h.strategy._rpg_handoffs
    return h


async def real_bot_startup(monkeypatch, h):
    async def idle(*args, **kwargs):
        await asyncio.Event().wait()

    async def noop(*args, **kwargs):
        return None

    async def scanner_after_restore():
        assert set(h.strategy._rpg_handoffs) == {row["id"] for row in h.recorded["tickets"]}
        h.bot._stop_event.set()

    class NoNetworkClient:
        def __init__(self, *args, **kwargs):
            pass

        run, stop = idle, noop

    monkeypatch.setattr(bot_module.Redis, "from_url", lambda *args, **kwargs: h.service.redis)
    monkeypatch.setattr(bot_module, "SchwabV2RestClient", NoNetworkClient)
    monkeypatch.setattr(bot_module, "SchwabV2Streamer", NoNetworkClient)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *args: None)
    monkeypatch.setattr(h.bot, "_publish_heartbeat", noop)
    for name in ("_heartbeat_loop", "_state_publish_loop", "_position_poll_loop", "_fanout_outcome_loop",
                 "_task_liveness_loop", "_rpg_handoff_loop"):
        monkeypatch.setattr(h.bot, name, idle)
    monkeypatch.setattr(h.bot, "_scanner_consumer_loop", scanner_after_restore)
    await asyncio.wait_for(h.bot.run(), timeout=5)


async def real_oms_startup(monkeypatch, h):
    async def noop(*args, **kwargs):
        return None

    async def idle(*args, **kwargs):
        await asyncio.Event().wait()

    async def control(stop_event):
        # Allow the real startup-created worker scan to finish, then process its
        # deliveries through the normal serial consumer before shutting down.
        await asyncio.wait_for(h.service._rpg_retry_started().wait(), timeout=5)
        rows, h.service.redis.entries = h.service.redis.entries, []
        for _, data in rows:
            if data.get("event_type") == "atr_reprice_tick":
                await h.service._handle_stream_message({"data": json.dumps(data)})
        stop_event.set()

    from project_mai_tai.oms import service as oms_module

    monkeypatch.setattr(oms_module, "_install_signal_handlers", lambda stop_event: None)
    monkeypatch.setattr(h.service, "_publish_heartbeat", noop)
    monkeypatch.setattr(h.service, "_rehydrate_armed_hard_stops", noop)
    monkeypatch.setattr(h.service, "_reconcile_protection_before_serving", noop)
    monkeypatch.setattr(h.service, "_run_tick_consumer", idle)
    monkeypatch.setattr(h.service, "_run_control_loop", control)
    await asyncio.wait_for(h.service.run(), timeout=5)


def tokenless_open(symbol, account):
    return TradeIntentEvent(source_service="schwab-1m-v2", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=account, symbol=symbol,
        side="buy", intent_type="open", quantity=Decimal(1), reason="Controlled next placement", metadata={}))


@pytest.mark.asyncio
async def test_real_startup_restores_all_seven_requested_tickets_and_eighth_census_guard(monkeypatch):
    h = await startup_harness(monkeypatch)
    before = dict(HandoffJournal(h.factory).jobs())
    await real_bot_startup(monkeypatch, h)
    for token, job in HandoffJournal(h.factory).jobs():
        original = before[token]
        if job != original:
            assert replacement_terminal_zero(job)
            assert job["revision"] == original["revision"] + 1
            assert {key: value for key, value in job.items() if key not in {
                "revision", "replacement_terminal_report"}} == {
                    key: value for key, value in original.items() if key != "revision"}
    before = dict(HandoffJournal(h.factory).jobs())
    await real_oms_startup(monkeypatch, h)
    for token, restored in HandoffJournal(h.factory).jobs():
        original = before[token]
        assert {key: value for key, value in restored.items()
                if key not in {"revision", "blocked_notice_at", "replacement_proof_edge"}
                and not key.startswith("terminal_rejection_probe_")} == {
            key: value for key, value in original.items() if key not in {"revision", "blocked_notice_at"}}
    assert len(h.strategy._rpg_handoffs) == 8
    early_mirror = h.strategy._rpg_handoffs["bd6ac0b9-727c-500b-8581-aabdd95992d4"]
    assert early_mirror["replacement_reasons"] == ["webull_mirror_precheck_deferred"]
    assert early_mirror["replacement"]["metadata"]["webull_deferred_resubmit_attempt"] == "3"
    assert early_mirror["old"]["metadata"]["broker_order_id"] == "OKH442PUJ5P19N50P4R5V1IFTB"
    with h.factory() as session:
        assert session.scalar(select(BrokerOrder).where(
            BrokerOrder.client_order_id == early_mirror["replacement"]["client_order_id"])) is None
    for row in RECORDED["tickets"]:
        job = h.strategy._rpg_handoffs[row["id"]]
        clear, blocked = DISPOSITIONS[row["id"][:8]]
        assert {key: value for key, value in job.items() if key not in {"revision", "replacement_terminal_report"}} == {
            key: value for key, value in row["payload"].items() if key != "revision"}
        assert old_buy_proven_clear(job) is clear
        state, account = h.strategy.watchlist_state(job["old"]["symbol"]), job["old"]["broker_account_name"]
        assert h.strategy._rpg_entry_owned(state, account=account) is blocked
        refusal = h.service._rpg_open_refusal(tokenless_open(state.symbol, account))
        assert refusal == ("rpg_old_buy_still_owned" if blocked else None)
    for symbol in ("APUS", "VEEA"):
        h.strategy._cw_v2_resting_track(h.strategy.watchlist_state(symbol), None)
    primary = h.strategy.drain_pending_intents()
    assert {draft.symbol for draft in primary} == {"APUS", "VEEA"}
    assert all(draft.intent_type == "open" and "rpg_handoff_token" not in draft.metadata for draft in primary)
    assert not h.strategy.drain_webull_direct_intents()
    h.strategy._cw_v2_resting_track(h.strategy.watchlist_state("RETO"), None)
    assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()
    assert not h.adapter.opens and not h.adapter.cancels
    assert len(h.adapter.reads) == 1 and h.adapter.reads[0].client_order_id == "schwab_1m_v2-RETO-open-7f273d444a66"


@pytest.mark.asyncio
async def test_real_startup_all_eight_and_all_five_deferred_intents_recover_only_proven_local_legs(monkeypatch):
    h = await startup_harness(monkeypatch, with_deferred=True)
    await real_bot_startup(monkeypatch, h)
    await real_oms_startup(monkeypatch, h)
    jobs = {str(token): job for token, job in HandoffJournal(h.factory).jobs()}
    local_clients = {
        "faa55c1f-262b-52e2-adcc-280ab1e5e2ff": "schwab_1m_v2-VEEA-open-2d004a27acd7",
        "ee3d0d07-abea-5fe2-98c9-cace5c08d135": "schwab_1m_v2-APUS-open-38a0c5c6f90d",
        "ba108172-04f6-5659-892b-a0fc10d22b15": "schwab_1m_v2-RETO-open-41cf913d0c96",
    }
    for token, client in local_clients.items():
        assert jobs[token]["phase"] == "clear"
        assert jobs[token]["local_no_wire"] and old_buy_proven_clear(jobs[token])
        assert jobs[token]["old"]["client_order_id"] == client
        assert jobs[token]["reads"] == 0
    assert jobs["ff6464ff-d65e-5657-8f47-1dc8d7b053c3"]["phase"] == "held_unknown"
    assert not h.adapter.opens and not h.adapter.cancels
    assert len(h.adapter.reads) == 1 and h.adapter.reads[0].client_order_id == "schwab_1m_v2-RETO-open-7f273d444a66"
    # A current entry hold ends the proven-local handoffs, not the unknown BUY.
    h.strategy._entries_held = True
    await feedback(h)
    await feedback(h)
    for token in local_clients:
        # Active recovery above remains clear. Only its real terminal expiry
        # permits the additional exact positive-proof release turn.
        await h.service._rpg_advance(UUID(token))
        job = HandoffJournal(h.factory).read(UUID(token))
        assert job["phase"] == "refused"
        assert job["release_reason"] == "old_local_no_wire_return_to_strategy"
    await h.bot._rpg_handoff_pass()
    for symbol in ("APUS", "VEEA", "RETO"):
        # This older five-row audit subset omits bd6's exact final e299 attempt.
        # Its reason string cannot waive replacement ownership; other local
        # targets still recover, and the late complete census supplies e299.
        blocked = symbol == "APUS"
        assert h.strategy._rpg_entry_owned(h.strategy.watchlist_state(symbol), account="live:orb") is blocked
        assert h.service._rpg_open_refusal(tokenless_open(symbol, "live:orb")) == (
            "rpg_old_buy_still_owned" if blocked else None)
    h.strategy._entries_held = False
    # Supply the missing exact final attempt before any new draft. Its later
    # positive proof must not be confused with replaying an old BUY/flip.
    from tests.unit.test_rpgstuck1 import RECORDED as E3
    audit = next(row for row in E3["intents"] if row["id"] == "24a768b5-b985-48df-8dd4-61dd3fd80800")
    seed_recorded_intent(h, {**audit, "strategy": "schwab_1m_v2", "symbol": "APUS",
                            "side": "buy", "intent_type": "open"})
    await h.bot._rpg_handoff_pass()
    for symbol in ("APUS", "VEEA", "RETO"):
        h.strategy._cw_v2_resting_track(h.strategy.watchlist_state(symbol), None)
    assert {draft.symbol for draft in h.strategy.drain_pending_intents()} == {"APUS", "VEEA"}
    assert {draft.symbol for draft in h.strategy.drain_webull_direct_intents()} == {"APUS", "VEEA", "RETO"}
    assert h.service._rpg_open_refusal(tokenless_open("RETO", "live:schwab_1m_v2")) == "rpg_old_buy_still_owned"
    with h.factory() as session:
        intents = list(session.scalars(select(TradeIntent)))
        assert len(intents) == 14  # Five old BUYs, five deferrals, three exact aborts, final E3 audit.
        assert {UUID(row["id"]) for row in AUDITS["intents"]
                if row["symbol"] in {"APUS", "VEEA"}} <= {intent.id for intent in intents}
        assert UUID(audit["id"]) in {intent.id for intent in intents}
        assert len(list(session.scalars(select(BrokerOrder)))) == 5


@pytest.mark.asyncio
async def test_recorded_reto_rejected_database_status_and_age_never_clear_exhausted_ticket(monkeypatch):
    h = await startup_harness(monkeypatch)
    await real_bot_startup(monkeypatch, h)
    token = next(token for token, job in HandoffJournal(h.factory).jobs()
                 if str(token).startswith("ff6464ff"))
    before = HandoffJournal(h.factory).read(token)
    with h.factory() as session:
        old = session.get(BrokerOrder, UUID(before["original_order_id"]))
        assert old.status == "rejected" and old.broker_order_id == "1008171127230"
    for _ in range(3):
        h.clock[0] += timedelta(days=1)
        h.service.__dict__.pop("_atr_reprice_controller", None)
        await h.service._rpg_advance(token)
        await h.bot._rpg_handoff_pass()
        job = HandoffJournal(h.factory).read(token)
        assert job["phase"] == "held_unknown" and job["reads"] == 30
        assert not old_buy_proven_clear(job) and not job["terminal_rejection_probe_proven"]
        assert h.strategy._rpg_entry_owned(h.strategy.watchlist_state("RETO"), account="live:schwab_1m_v2")
    assert before["reads"] == 30 and before["reason"] == "readback_budget_exhausted"
    assert not h.adapter.opens and not h.adapter.cancels and len(h.adapter.reads) == 1


@pytest.mark.parametrize("record", BROKER["orders"], ids=lambda record: str(record["body"]["orderId"]))
def test_actual_four_schwab_bodies_require_exact_terminal_zero_parent(record):
    job = next(row["payload"] for row in RECORDED["tickets"]
               if row["payload"]["old"]["metadata"].get("broker_order_id") == str(record["body"]["orderId"]))
    readback = schwab_buy_readback(_request(job["old"]), record["body"])
    assert readback.can_replace is (record["body"]["status"] == "CANCELED")
    assert readback.outcome == ("cancelled_empty" if record["body"]["status"] == "CANCELED" else "rejected_empty")
    assert readback.cumulative_filled == 0
    assert readback.broker_status == record["body"]["status"]
    assert readback.terminal_cancel is (record["body"]["status"] == "CANCELED")


@pytest.mark.asyncio
async def test_real_oms_startup_exhausted_reto_uses_actual_rejected_zero_readback_once(monkeypatch):
    h = await startup_harness(monkeypatch, with_deferred=True)
    body = next(row["body"] for row in BROKER["orders"] if row["body"]["status"] == "REJECTED")
    adapter = SchwabBrokerAdapter(h.service.settings.model_copy(update={
        "schwab_access_token": "SIMULATED", "schwab_refresh_token": None, "schwab_token_store_path": None}),
        accounts_by_name={"live:schwab_1m_v2": SchwabAccountConfig("SIMULATED-ACCOUNT")})
    gets = []

    async def transport(method, path, **kwargs):
        assert method == "GET" and path == "/trader/v1/accounts/SIMULATED-ACCOUNT/orders/1008171127230"
        gets.append(path)
        return 200, {}, deepcopy(body)

    monkeypatch.setattr(adapter, "_authorized_request_json", transport)
    h.service.broker_adapter = SimpleNamespace(submit_order=h.adapter.submit_order,
        read_atr_resting_buy_after_cancel=adapter.read_atr_resting_buy_after_cancel)
    await real_bot_startup(monkeypatch, h)
    await real_oms_startup(monkeypatch, h)
    token = UUID("ff6464ff-d65e-5657-8f47-1dc8d7b053c3")
    job = HandoffJournal(h.factory).read(token)
    assert len(gets) == 1
    assert job["phase"] == "refused" and old_buy_proven_clear(job)
    assert job["broker_status"] == "REJECTED" and job["reads"] == 30
    assert not job.get("local_no_wire") and job["cleared_at"]
    await h.bot._rpg_handoff_pass()
    state = h.strategy.watchlist_state("RETO")
    assert not h.strategy._rpg_entry_owned(state, account="live:schwab_1m_v2")
    assert h.service._rpg_open_refusal(tokenless_open("RETO", "live:schwab_1m_v2")) is None
    h.service.__dict__.pop("_atr_reprice_controller", None)
    await h.service._rpg_advance(token)
    assert len(gets) == 1 and not h.adapter.opens and not h.adapter.cancels
    with h.factory() as session:
        assert session.get(BrokerOrder, UUID(job["original_order_id"])).status == "rejected"
        assert len(list(session.scalars(select(BrokerOrder)))) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["APUS", "VEEA"])
@pytest.mark.parametrize("counterfactual", ["submitted", "other_abort", "origin", "source", "predecessor",
                                           "attempt", "slot", "segment", "wire_row", "no_initial"])
async def test_recorded_local_retry_chain_never_releases_unproven_dispatch(monkeypatch, symbol, counterfactual):
    rows = deepcopy(RECORDED["deferred_intents"])
    later = next(row for row in rows if row["symbol"] == symbol and row["payload"]["refusal_origin"] == "client_abort")
    if counterfactual == "submitted":
        later["status"] = "submitted"
    elif counterfactual == "other_abort":
        later["payload"]["refusal_code"] = "unclassified_abort"
    elif counterfactual == "origin":
        later["payload"]["refusal_origin"] = "broker"
    elif counterfactual == "source":
        later["payload"]["source_service"] = "unproven-source"
    elif counterfactual in {"predecessor", "attempt", "slot", "segment"}:
        key = {"predecessor": "fanout_predecessor_attempt_id", "attempt": "fanout_attempt_id",
               "slot": "cw_entry_slot", "segment": "fanout_segment_id"}[counterfactual]
        later["payload"]["metadata"][key] += "-changed"
    elif counterfactual == "no_initial":
        rows = [row for row in rows if row["symbol"] != symbol or row["payload"]["refusal_origin"] != "skipped_before_submit"]
    h = await startup_harness(monkeypatch, with_deferred=True, deferred_rows=rows)
    if counterfactual == "wire_row":
        with h.factory() as session:
            intent = session.get(TradeIntent, UUID(later["id"]))
            h.service.store.get_or_create_order(session, intent=intent, strategy_id=intent.strategy_id,
                broker_account_id=intent.broker_account_id, client_order_id=later["payload"]["metadata"]["fanout_attempt_id"],
                symbol=symbol, side="buy", quantity=intent.quantity, metadata=later["payload"]["metadata"],
                status="pending", order_type="STOP_LIMIT", time_in_force="day")
            session.commit()
    token = next(token for token, job in HandoffJournal(h.factory).jobs()
                 if job["old"]["symbol"] == symbol and job["old"]["broker_account_name"] == "live:orb"
                 and job["phase"] == "held_unknown")
    await real_bot_startup(monkeypatch, h)
    await h.service._rpg_advance(token)
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "held_unknown" and not old_buy_proven_clear(job)
    assert h.service._rpg_open_refusal(tokenless_open(symbol, "live:orb")) == "rpg_old_buy_still_owned"
    assert not h.adapter.opens and not h.adapter.cancels and not h.adapter.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("counterfactual", ["http", "identity", "quantity", "symbol", "missing_zero",
                                           "positive_fill", "cancelled_positive_fill", "working", "remaining"])
async def test_reto_terminal_probe_unproven_or_filled_never_releases_or_retries(monkeypatch, counterfactual):
    h = await startup_harness(monkeypatch)
    token = UUID("ff6464ff-d65e-5657-8f47-1dc8d7b053c3")
    body = deepcopy(next(row["body"] for row in BROKER["orders"] if row["body"]["status"] == "REJECTED"))
    if counterfactual == "identity":
        body["orderId"] += 1
    elif counterfactual == "quantity":
        body["quantity"] += 1
    elif counterfactual == "symbol":
        body["orderLegCollection"][0]["instrument"]["symbol"] = "OTHER"
    elif counterfactual == "missing_zero":
        del body["filledQuantity"]
    elif counterfactual in {"positive_fill", "cancelled_positive_fill"}:
        body["filledQuantity"] = 1
        if counterfactual == "cancelled_positive_fill":
            body["status"] = "CANCELED"
    elif counterfactual == "working":
        body["status"] = "WORKING"
    elif counterfactual == "remaining":
        body["remainingQuantity"] = 1

    async def read(request):
        h.adapter.reads.append(request)
        return AtrBuyReadback("unknown", "CONTROLLED HTTP 404") if counterfactual == "http" else schwab_buy_readback(request, body)

    monkeypatch.setattr(h.adapter, "read_atr_resting_buy_after_cancel", read)
    for _ in range(3):
        await h.service._rpg_advance(token)
        h.service.__dict__.pop("_atr_reprice_controller", None)
        h.clock[0] += timedelta(days=1)
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "held_unknown" and not old_buy_proven_clear(job) and job["reads"] == 30
    assert not job["terminal_rejection_probe_proven"]
    assert job.get("no_rebuy", False) is (counterfactual in {"positive_fill", "cancelled_positive_fill"})
    if job.get("no_rebuy"):
        assert job["terminal_rejection_fill_quantity"] == "1"
        assert job["terminal_rejection_fill_accounting"] == "UNMEASURED"
    assert len(h.adapter.reads) == 1 and not h.adapter.cancels and not h.adapter.opens
    await h.bot._rpg_handoff_pass()
    assert h.strategy._rpg_entry_owned(h.strategy.watchlist_state("RETO"), account="live:schwab_1m_v2")


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed_age", [0, 86400, 604800])
async def test_reto_terminal_probe_crash_claim_never_repeats_get_or_releases(monkeypatch, claimed_age):
    h = await startup_harness(monkeypatch)
    token, journal = UUID("ff6464ff-d65e-5657-8f47-1dc8d7b053c3"), HandoffJournal(h.factory)
    job = journal.read(token)
    # Controlled crash immediately after durable claim, before any GET answer.
    journal.change(token, job["revision"], terminal_rejection_probe_started_at=h.clock[0].timestamp() - claimed_age)
    await real_bot_startup(monkeypatch, h)
    await real_oms_startup(monkeypatch, h)
    await h.service._rpg_advance(token)
    assert journal.read(token)["phase"] == "held_unknown" and not old_buy_proven_clear(journal.read(token))
    assert not h.adapter.reads and not h.adapter.opens and not h.adapter.cancels


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["parent", "account", "broker", "generation", "quantity"])
async def test_reto_terminal_probe_requires_exact_committed_old_identity(monkeypatch, mismatch):
    h = await startup_harness(monkeypatch)
    token, journal = UUID("ff6464ff-d65e-5657-8f47-1dc8d7b053c3"), HandoffJournal(h.factory)
    job = journal.read(token)
    old = deepcopy(job["old"])
    if mismatch == "parent":
        old["client_order_id"] += "-other"
    elif mismatch == "account":
        old["broker_account_name"] = "live:orb"
    elif mismatch == "quantity":
        old["quantity"] = "253"
    else:
        old["metadata"]["broker_order_id" if mismatch == "broker" else "rpg_resting_generation"] += "-other"
    journal.change(token, job["revision"], old=old)
    await h.service._rpg_advance(token)
    assert journal.read(token)["phase"] == "held_unknown" and not old_buy_proven_clear(journal.read(token))
    assert not h.adapter.reads and not h.adapter.opens and not h.adapter.cancels


@pytest.mark.asyncio
async def test_reto_terminal_probe_positive_fill_with_price_uses_existing_accounting_no_rebuy(monkeypatch):
    h = await startup_harness(monkeypatch)
    token = UUID("ff6464ff-d65e-5657-8f47-1dc8d7b053c3")
    h.adapter.override = AtrBuyReadback("fills", "CONTROLLED positive race with exact execution price",
        cumulative_filled=Decimal(1), fill_price=Decimal("2.36"), broker_status="REJECTED")
    await h.service._rpg_advance(token)
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "held_unknown" and job["no_rebuy"] and not old_buy_proven_clear(job)
    assert job["terminal_rejection_fill_accounting"] == "recorded"
    with h.factory() as session:
        fills = list(session.scalars(select(Fill)))
        assert len(fills) == 1
        fill = fills[0]
        assert fill.order_id == UUID(job["original_order_id"]) and fill.quantity == 1 and fill.price == Decimal("2.36")
    await h.service._rpg_advance(token)
    assert len(h.adapter.reads) == 1 and not h.adapter.opens and not h.adapter.cancels


@pytest.mark.asyncio
async def test_tonight_off_existing_clear_local_jobs_resume_without_legacy_duplicate(monkeypatch):
    h = await startup_harness(monkeypatch, with_deferred=True)
    await real_bot_startup(monkeypatch, h)
    await real_oms_startup(monkeypatch, h)
    tokens = {token for token, job in HandoffJournal(h.factory).jobs() if job["phase"] == "clear"}
    assert len(tokens) == 3 and not h.service.settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled
    for symbol in ("APUS", "VEEA", "RETO"):
        state = h.strategy.watchlist_state(symbol)
        assert h.strategy._rpg_entry_owned(state, account="live:orb")
        assert h.service._rpg_open_refusal(tokenless_open(symbol, "live:orb")) == "rpg_old_buy_still_owned"
        h.strategy._cw_v2_resting_track(state, None)
        h.service._latest_quotes_by_symbol[symbol] = {"ask": Decimal("3.06"), "received_at": h.clock[0]}
    assert not h.strategy.drain_webull_direct_intents()
    # Only the already-clear primary legs may have ordinary legacy drafts here.
    assert {draft.symbol for draft in h.strategy.drain_pending_intents()} == {"APUS", "VEEA"}
    submit = h.adapter.submit_order

    async def unique_acceptance(request):
        return [replace(report, broker_order_id="SIMULATED-" + request.symbol) for report in await submit(request)]

    monkeypatch.setattr(h.adapter, "submit_order", unique_acceptance)
    await feedback(h)
    await feedback(h)
    for token in tokens:
        job = HandoffJournal(h.factory).read(token)
        assert job["phase"] == "placed" and job["authorization"]["verdict"] == "ready"
    assert len(h.adapter.opens) == 3
    assert {request.symbol for request in h.adapter.opens} == {"APUS", "VEEA", "RETO"}
    assert all(request.broker_account_name == "live:orb" and request.metadata["rpg_handoff_token"] in
               {str(token) for token in tokens} for request in h.adapter.opens)
    for _ in range(3):
        await feedback(h)
        for symbol in ("APUS", "VEEA", "RETO"):
            h.strategy._cw_v2_resting_track(h.strategy.watchlist_state(symbol), None)
        assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()
    assert len(h.adapter.opens) == 3 and not h.adapter.cancels
    assert {token for token, _ in HandoffJournal(h.factory).jobs()} == {
        UUID(row["id"]) for row in RECORDED["tickets"]}


@pytest.mark.asyncio
async def test_reto_terminal_zero_cannot_override_fill_accounted_during_get(monkeypatch):
    h = await startup_harness(monkeypatch)
    token = UUID("ff6464ff-d65e-5657-8f47-1dc8d7b053c3")
    body = next(row["body"] for row in BROKER["orders"] if row["body"]["status"] == "REJECTED")

    async def read(old):
        h.adapter.reads.append(old)
        assert await h.service._rpg_record_fill(old, ExecutionReport(
            "partially_filled", old.client_order_id, broker_order_id=old.metadata["broker_order_id"],
            symbol="RETO", side="buy", intent_type="open", quantity=old.quantity,
            filled_quantity=Decimal(1), fill_price=Decimal("2.36"), origin="broker",
            reason="CONTROLLED concurrent fill accounting"))
        return schwab_buy_readback(old, body)

    monkeypatch.setattr(h.adapter, "read_atr_resting_buy_after_cancel", read)
    await h.service._rpg_advance(token)
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "held_unknown" and job["no_rebuy"] and not old_buy_proven_clear(job)
    assert job["terminal_rejection_fill_accounting"] == "already_recorded"
    assert not job["terminal_rejection_probe_proven"]
    assert len(h.adapter.reads) == 1 and not h.adapter.opens and not h.adapter.cancels


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_available", [True, False], ids=["recorded-proof", "true-unknown"])
async def test_tonight_all_eight_outside_window_startup_expires_only_proven_tickets_no_stored_buys(monkeypatch, proof_available):
    h = await startup_harness(monkeypatch, with_deferred=proof_available)
    h.clock[0] = datetime.fromisoformat("2026-10-06T00:05:00+00:00")  # 20:05 ET, after every configured entry window.
    if proof_available:
        job = next(row["payload"] for row in RECORDED["tickets"] if row["id"].startswith("ff6464ff"))
        body = next(row["body"] for row in BROKER["orders"] if row["body"]["status"] == "REJECTED")
        h.adapter.override = schwab_buy_readback(_request(job["old"]), body)
    await real_bot_startup(monkeypatch, h)
    await real_oms_startup(monkeypatch, h)
    await feedback(h)
    await feedback(h)
    jobs = dict(HandoffJournal(h.factory).jobs())
    assert len(jobs) == 8
    for token, job in jobs.items():
        originally_clear = DISPOSITIONS[str(token)[:8]][0]
        if originally_clear or proof_available:
            assert old_buy_proven_clear(job) and job["phase"] in {"refused", "expired"}
            if not originally_clear and job["old"]["broker_account_name"] == "live:orb":
                assert job["phase"] == "expired" and job["reason"] == "window_closed"
        else:
            assert job["phase"] == "held_unknown" and not old_buy_proven_clear(job)
    assert not h.adapter.opens and not h.adapter.cancels and len(h.adapter.reads) == 1
    h.clock[0] = datetime.fromisoformat("2026-10-06T14:30:00+00:00")  # Next-day ownership check, not a late replay BUY.
    await feedback(h)
    for symbol in ("APUS", "VEEA", "RETO"):
        for account in ("live:schwab_1m_v2", "live:orb"):
            blocked = (not proof_available and (account == "live:orb" or symbol == "RETO")) or (
                symbol == "APUS" and account == "live:orb")
            assert h.strategy._rpg_entry_owned(h.strategy.watchlist_state(symbol), account=account) is blocked
            assert h.service._rpg_open_refusal(tokenless_open(symbol, account)) == (
                "rpg_old_buy_still_owned" if blocked else None)
    assert not h.adapter.opens and not h.adapter.cancels and len(h.adapter.reads) == 1


def set_tonight_flags(h):
    settings = h.service.settings.model_copy(update=TONIGHT_FLAGS)
    h.service.settings = h.strategy.settings = h.bot.settings = settings
    h.strategy._gap_hold_enabled = True
    for key, expected in TONIGHT_FLAGS.items():
        assert getattr(settings, key) is expected
    assert not h.strategy._flip_owned_first_entry_enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_tonight_flags_off_legacy_first_reclaim_cancel_then_next_pass(monkeypatch, slot):
    h = await runtime(monkeypatch, "schwab", slot=slot, notional=600)
    set_tonight_flags(h)
    track = (lambda: h.strategy._cw_v2_resting_track(h.state, None)) if slot == "first" else (
        lambda: h.strategy._cw_v2_reclaim_resting_track(h.state))
    track()
    primary_cancel, = h.strategy.drain_pending_intents()
    mirror_cancel, = h.strategy.drain_webull_direct_intents()
    assert primary_cancel.intent_type == mirror_cancel.intent_type == "cancel"
    for draft in (primary_cancel, mirror_cancel):
        assert "atr_reprice" not in draft.metadata
        assert "rpg_handoff_token" not in draft.metadata
    assert not h.strategy._rpg_handoffs
    assert not HandoffJournal(h.factory).jobs()
    track()
    primary, = h.strategy.drain_pending_intents()
    mirror, = h.strategy.drain_webull_direct_intents()
    assert primary.intent_type == mirror.intent_type == "open"
    assert primary.metadata["cw_entry_slot"] == mirror.metadata["cw_entry_slot"] == slot
    assert primary.quantity > 0 and mirror.quantity > 0
    assert not HandoffJournal(h.factory).jobs()
    track()
    assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_tonight_flags_gap_hold_still_blocks_legacy_replacement(monkeypatch, slot):
    h = await runtime(monkeypatch, "schwab", slot=slot, notional=600)
    set_tonight_flags(h)
    track = (lambda: h.strategy._cw_v2_resting_track(h.state, None)) if slot == "first" else (
        lambda: h.strategy._cw_v2_reclaim_resting_track(h.state))
    h.strategy.begin_gap_hold(h.state.symbol, detected_at_ms=h.strategy._now_ms(),
        last_bar_age_s=100, last_print_age_s=0)
    primary_cancel = h.strategy.drain_pending_intents()
    mirror_cancel = h.strategy.drain_webull_direct_intents()
    assert len(primary_cancel) == len(mirror_cancel) == 1
    assert primary_cancel[0].intent_type == mirror_cancel[0].intent_type == "cancel"
    for _ in range(20):
        h.clock[0] += timedelta(seconds=60)
        track()
        assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()
    assert not HandoffJournal(h.factory).jobs()
