"""AIXI Oct8 durable soft-rest replay; broker responses are explicit proof controls."""
import asyncio
import json
import math
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import (
    Base, BrokerOrder, DashboardSnapshot, Fill, OmsManagedPosition, TradeIntent, VirtualPosition,
)
from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipRecord
from tests.unit.test_v2_flip_owned_first_entry import _strategy, _book, PRIMARY, WEBULL

EVIDENCE = json.loads((Path(__file__).parents[2] /
    "docs/review-artifacts/slotclear1-soft-rest-boot/aixi-1008.json").read_text())
OPP = 1791462062227
BOOT = 1791462615200
SEGMENT = 1791461820000
BEGIN = next(r["payload"] for r in EVIDENCE["journal"] if r["snapshot_type"] == "v2_wait_dispatch")


def boot(*, phase="resting", **changes):
    strategy, clock, identities, owners = _strategy(dual=True, retry_one=True)
    clock[0] = BOOT
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_flip_enabled = True
    strategy.settings.strategy_schwab_1m_v2_resting_buy_round_up_enabled = True
    strategy._resting_trigger_offset_pct = 0.5
    strategy._boot_ms = BOOT
    record = FlipEntryOwnershipRecord(symbol="AIXI", opportunity_id=OPP, phase=phase,
        flip_bar_ts=0, provisional_started_ms=0, fill_accounts=(), position_ids={},
        position_entry_ms={}, retry_segment_id=SEGMENT, retry_closes_at_place=0)
    record = replace(record, **changes)
    strategy.configure_fanout_identity_persistence(
        lambda *args: identities.append(args), restored={"AIXI": OPP})
    strategy.configure_flip_entry_ownership(
        lambda *args: owners.append(args), active_segments={"AIXI": OPP},
        restored={"AIXI": record}, retry_budget_persist=lambda *args: None,
        restored_retry_budgets={"AIXI": (SEGMENT, 0)})
    proof = {"AIXI": dict(opportunity_id=OPP, never_dispatched=True,
        flat_accounts=[PRIMARY, WEBULL], observed_at_ms=BOOT, reason="positive_begin_no_attempt")}
    return strategy, clock, identities, owners, proof


@pytest.mark.parametrize("seed_path", ["math_seed", "historical_callback"])
def test_recorded_aixi_ghost_retired_before_seed_cap_live_buy_is_first_not_retry(seed_path):
    strategy, clock, identities, owners, proof = boot()
    candidates = strategy.soft_rest_boot_candidates()
    assert candidates == {"AIXI": OPP}
    strategy.recover_soft_rest_boot(candidates, proof)
    state = strategy.watchlist_state("AIXI")
    assert state.flip_owner_phase == "idle" and state.flip_owner_opportunity_id == 0
    assert "AIXI" not in strategy._restored_fanout_segment_ids
    assert identities[-1][1:3] == (OPP, False)
    assert owners[-1][1] is False
    bars = [OHLCVBar(int(datetime.fromisoformat(r["bar_time"]).timestamp() * 1000),
        *(float(r[k]) for k in ("open_price", "high_price", "low_price", "close_price")), r["volume"])
        for r in EVIDENCE["aixi_bars"]]
    oracle = compute_atr_trail([Bar(b.timestamp_ms, b.open, b.high, b.low, b.close, b.volume) for b in bars])
    prefix = [b for b in bars if b.timestamp_ms + 60000 <= BOOT]
    if seed_path == "math_seed":
        strategy.seed_atr_state("AIXI", prefix)
    else:
        for bar in prefix:
            assert strategy.on_observed_bar("AIXI", bar, observation_phase="replay") is None
            assert strategy.drain_pending_intents() == strategy.drain_webull_direct_intents() == []
    assert state.atr_state == "short" and state.atr_short_flip_bar_ts == SEGMENT
    assert strategy.drain_pending_intents() == strategy.drain_webull_direct_intents() == []
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot._watch_start_ms = strategy, {"AIXI": BOOT}
    bot._consume_reconstructed_slots(state, BOOT)
    assert state.cw_seed_cap_watch_start_ms == BOOT
    strategy._entries_held = False
    target = next(r for r in oracle if r["et"] == "08:39")
    assert target["flip"] == "BUY"
    previous = oracle[[r["ts"] for r in oracle].index(target["ts"]) - 1]
    for bar in bars:
        if bar.timestamp_ms + 60000 <= BOOT or bar.timestamp_ms > target["ts"]:
            continue
        clock[0] = bar.timestamp_ms + 61000
        _book(strategy, clock, "AIXI")
        assert strategy.on_observed_bar("AIXI", bar, observation_phase="live") is None
        assert strategy.drain_pending_intents() == strategy.drain_webull_direct_intents() == []
    assert state.cw_flip_level == pytest.approx(previous["trail"], abs=0.00005)
    assert state.atr_state == target["state"] == "long"
    assert not state.cw_resting_taken and not state.cw_reclaim_taken
    assert state.retry_one_closes_in_segment == 0
    # Decision-cache quotes at 08:40 are UNMEASURED. This is an explicit in-band
    # quote control at the computed trigger, NOT an invented recorded trade.
    trigger = strategy._resting_trigger_for_line(state.cw_flip_level)
    confirming_ask = math.ceil(trigger * 100) / 100
    quote = Quote(symbol="AIXI", last_price=confirming_ask, ask_price=confirming_ask,
                  bid_price=confirming_ask, quote_time_ms=clock[0])
    primary = strategy.on_quote("AIXI", quote)
    assert primary is not None
    mirrors = strategy.drain_webull_fanout_intents()
    assert len(mirrors) == 1
    assert primary.metadata["cw_entry_slot"] == mirrors[0].metadata["cw_entry_slot"] == "first"
    assert primary.metadata["fanout_slot_id"] == mirrors[0].metadata["fanout_slot_id"]
    assert primary.quantity == 295 and mirrors[0].quantity == 147
    assert strategy.on_quote("AIXI", quote) is None
    assert strategy.drain_webull_fanout_intents() == []


@pytest.mark.parametrize("field,value", [
    ("never_dispatched", False), ("opportunity_id", OPP + 1),
    ("flat_accounts", [PRIMARY]), ("flat_accounts", [PRIMARY, "foreign"]),
    ("observed_at_ms", BOOT - 15001), ("observed_at_ms", BOOT + 1),
])
def test_unproven_stale_or_foreign_boot_evidence_keeps_owner(field, value):
    strategy, _, identities, owners, proof = boot()
    proof["AIXI"][field] = value
    strategy.recover_soft_rest_boot(strategy.soft_rest_boot_candidates(), proof)
    assert strategy.watchlist_state("AIXI").flip_owner_phase == "resting"
    assert identities == owners == []


@pytest.mark.parametrize("changes", [
    {"phase": "awaiting_fill"}, {"phase": "bound"}, {"phase": "consumed"},
    {"fill_accounts": (WEBULL,)}, {"position_ids": {WEBULL: "recorded-fill"}},
    {"provisional_started_ms": BOOT - 1}, {"flip_bar_ts": SEGMENT},
    {"retry_closes_at_place": 1},
])
def test_filled_consumed_or_provisional_owner_never_candidate(changes):
    strategy, *_ = boot(**changes)
    assert strategy.soft_rest_boot_candidates() == {}


def sql_bot():
    strategy, clock, *_ = boot()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    store = OmsStore()
    with sessions() as session:
        sid = store.ensure_strategy(session, "schwab_1m_v2").id
        aids = {name: store.ensure_broker_account(session, name,
            provider="schwab" if name == PRIMARY else "webull", environment="live").id
            for name in (PRIMARY, WEBULL)}
        session.add(DashboardSnapshot(snapshot_type="v2_wait_dispatch", payload=BEGIN))
        session.commit()
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot.session_factory = strategy, strategy.settings, sessions
    bot._soft_rest_boot_positions = AsyncMock(return_value={PRIMARY: (BOOT, set()), WEBULL: (BOOT, set())})
    return bot, sessions, sid, aids, clock


def test_recorded_begin_without_attempt_plus_fresh_direct_books_proves_no_wire():
    bot, *_ = sql_bot()
    proof = bot._soft_rest_boot_proofs({"AIXI": OPP})
    assert proof["AIXI"]["never_dispatched"] is True
    assert set(proof["AIXI"]["flat_accounts"]) == {PRIMARY, WEBULL}
    bot.strategy.recover_soft_rest_boot({"AIXI": OPP}, proof)
    assert bot.strategy.watchlist_state("AIXI").flip_owner_phase == "idle"


@pytest.mark.parametrize("barrier", ["missing_begin", "attempt", "wrong_accounts", "inflight",
    "working_protection", "real_broker_rest", "held_primary", "held_mirror"])
def test_real_broker_keeprest_or_unresolved_dispatch_stays_owned(barrier):
    bot, sessions, sid, aids, _ = sql_bot()
    with sessions() as session:
        row = session.query(DashboardSnapshot).one()
        if barrier == "missing_begin":
            session.delete(row)
        elif barrier == "wrong_accounts":
            row.payload = {**BEGIN, "account_names": [PRIMARY]}
        elif barrier == "attempt":
            session.add(DashboardSnapshot(snapshot_type="v2_wait_dispatch",
                payload={**BEGIN, "kind": "attempt", "attempt_token": "published-before-xadd", "account_name": WEBULL}))
        elif barrier == "inflight":
            session.add(TradeIntent(strategy_id=sid, broker_account_id=aids[PRIMARY],
                symbol="AIXI", side="buy", intent_type="open", quantity=1, reason="pending",
                status="pending", created_at=datetime.fromtimestamp(OPP/1000, UTC)))
        elif barrier in {"working_protection", "real_broker_rest"}:
            session.add(BrokerOrder(strategy_id=sid, broker_account_id=aids[PRIMARY],
                symbol="AIXI", side="sell" if barrier == "working_protection" else "buy",
                quantity=1, client_order_id="real-wire", broker_order_id="exact-broker-order",
                order_type="limit", time_in_force="day", status="accepted"))
        session.commit()
    if barrier.startswith("held_"):
        account = PRIMARY if barrier == "held_primary" else WEBULL
        bot._soft_rest_boot_positions.return_value[account] = (BOOT, {"AIXI"})
    proof = bot._soft_rest_boot_proofs({"AIXI": OPP})
    assert proof["AIXI"]["never_dispatched"] is False
    bot.strategy.recover_soft_rest_boot({"AIXI": OPP}, proof)
    assert bot.strategy.watchlist_state("AIXI").flip_owner_phase == "resting"


def test_unknown_broker_or_sql_read_keeps_owner(monkeypatch):
    strategy, _, identities, owners, _ = boot()
    bot = object.__new__(SchwabV2BotService)
    bot.strategy = strategy
    def fail(_):
        raise ValueError("cancel/fill/position source unreadable")
    bot._soft_rest_boot_proofs = fail
    asyncio.run(bot._recover_soft_rest_boot())
    assert strategy.watchlist_state("AIXI").flip_owner_phase == "resting"
    assert identities == owners == []


def test_flag_off_does_no_boot_sql_or_http():
    strategy, *_ = boot()
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_flip_enabled = False
    bot = object.__new__(SchwabV2BotService)
    bot.strategy = strategy
    asyncio.run(bot._recover_soft_rest_boot())
    assert strategy.watchlist_state("AIXI").flip_owner_phase == "resting"


def test_boot_proof_runs_off_loop_before_any_seed_or_emitter():
    import inspect
    source = inspect.getsource(SchwabV2BotService.run)
    assert source.index("await self._recover_soft_rest_boot()") < source.index("self.intent_emitter =")
    assert "asyncio.to_thread(self._soft_rest_boot_proofs" in inspect.getsource(SchwabV2BotService._recover_soft_rest_boot)


def position_reader(monkeypatch, *, schwab_body=None, webull_body=None, refresh=False):
    import sys
    import project_mai_tai.broker_adapters.schwab as sm
    import project_mai_tai.broker_adapters.webull as wm
    class Request:
        def set_account_id(self, value):
            self.account_id = value
        def set_page_size(self, value):
            self.page_size = value
        def set_last_instrument_id(self, value):
            self.cursor = value
    monkeypatch.setitem(sys.modules, "webull.trade.request.get_account_positions_request",
                        SimpleNamespace(AccountPositionsRequest=Request))
    strategy, *_ = boot()
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings = strategy, strategy.settings
    body = schwab_body if schwab_body is not None else {"securitiesAccount": {
        "accountNumber": "123", "currentBalances": {}, "positions": []}}
    sa = SimpleNamespace(_adapter_refresh_enabled=refresh,
        accounts_by_name={PRIMARY: SimpleNamespace(account_hash="mapped-hash")},
        _get_access_token=AsyncMock(return_value="READ-ONLY-FAKE-TOKEN"),
        _request_json=AsyncMock(side_effect=[(200, {}, [{"hashValue": "mapped-hash", "accountNumber": "123"}]), (200, {}, body)]))
    wb = webull_body if webull_body is not None else {"account_id": "456", "holdings": [], "has_next": False}
    response = SimpleNamespace(status_code=200, json=lambda: wb)
    wa = SimpleNamespace(accounts_by_name={WEBULL: SimpleNamespace(account_id="456")},
        _get_client=lambda: SimpleNamespace(get_response=lambda _: response))
    monkeypatch.setattr(sm, "SchwabBrokerAdapter", lambda _: sa)
    monkeypatch.setattr(wm, "WebullBrokerAdapter", lambda _: wa)
    return bot, sa


def test_fresh_complete_account_mapped_broker_empty_books_are_positive_flat(monkeypatch):
    bot, adapter = position_reader(monkeypatch)
    books = asyncio.run(bot._soft_rest_boot_positions())
    assert set(books) == {PRIMARY, WEBULL}
    assert all(not held for _, held in books.values())
    assert all(datetime.now(UTC).timestamp() * 1000 - stamp < 15000 for stamp, _ in books.values())
    assert all(call.args[0] == "GET" for call in adapter._request_json.call_args_list)


@pytest.mark.parametrize("body", [{}, {"securitiesAccount": {}},
    {"securitiesAccount": {"accountNumber": "foreign", "currentBalances": {}}},
    {"securitiesAccount": {"accountNumber": "123", "positions": []}},
    {"securitiesAccount": {"accountNumber": "123", "currentBalances": {}, "positions": None}},
    {"securitiesAccount": {"accountNumber": "123", "currentBalances": {}, "positions": [{"instrument": {"symbol": "AIXI"}, "longQuantity": "NaN"}]}},
])
def test_schwab_malformed_empty_wrong_account_or_nonfinite_is_unknown(monkeypatch, body):
    bot, _ = position_reader(monkeypatch, schwab_body=body)
    with pytest.raises((ValueError, TypeError, KeyError)):
        asyncio.run(bot._soft_rest_boot_positions())


@pytest.mark.parametrize("body", [{}, {"holdings": []},
    {"account_id": "foreign", "holdings": [], "has_next": False},
    {"holdings": [], "has_next": True}, {"holdings": [], "has_next": "false"},
    {"holdings": [], "has_next": False, "hasNext": True},
    {"holdings": [{"symbol": "AIXI"}], "has_next": False},
    {"holdings": [{"symbol": "AIXI", "quantity": "NaN"}], "has_next": False},
    {"holdings": [{"symbol": "AIXI", "quantity": False}], "has_next": False},
    {"holdings": [{"symbol": "AIXI", "quantity": 0, "account_id": "foreign"}], "has_next": False},
])
def test_webull_malformed_empty_wrong_account_pagination_or_quantity_unknown(monkeypatch, body):
    bot, _ = position_reader(monkeypatch, webull_body=body)
    with pytest.raises((ValueError, TypeError, KeyError)):
        asyncio.run(bot._soft_rest_boot_positions())


def test_schwab_boot_reader_never_refreshes_token(monkeypatch):
    bot, adapter = position_reader(monkeypatch, refresh=True)
    with pytest.raises(ValueError, match="must not refresh"):
        asyncio.run(bot._soft_rest_boot_positions())
    adapter._request_json.assert_not_called()


def test_partial_account_coverage_never_becomes_no_wire_proof():
    bot, *_ = sql_bot()
    bot._soft_rest_boot_positions.return_value.pop(WEBULL)
    with pytest.raises(ValueError, match="coverage incomplete"):
        bot._soft_rest_boot_proofs({"AIXI": OPP})


def test_retirement_write_failure_is_unknown_not_idle():
    strategy, _, _, _, proof = boot()
    def fail(*_):
        raise ValueError("durable retirement failed")
    strategy._fanout_identity_persist = fail
    strategy.recover_soft_rest_boot(strategy.soft_rest_boot_candidates(), proof)
    assert strategy.watchlist_state("AIXI").flip_owner_phase == "unknown"
    assert strategy._restored_fanout_segment_ids["AIXI"] == OPP


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("barrier", ["fill", "managed", "virtual"])
def test_both_account_fill_position_and_protection_evidence_blocks(account, barrier):
    bot, sessions, sid, aids, _ = sql_bot()
    with sessions() as session:
        if barrier == "fill":
            session.add(Fill(order_id=uuid4(), strategy_id=sid, broker_account_id=aids[account],
                symbol="AIXI", side="buy", quantity=1, price=2.03,
                filled_at=datetime.fromtimestamp(OPP / 1000, UTC)))
        elif barrier == "managed":
            session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account,
                symbol="AIXI", entry_price=2.03, original_quantity=1, current_quantity=1, status="open"))
        else:
            session.add(VirtualPosition(strategy_id=sid, broker_account_id=aids[account],
                symbol="AIXI", quantity=1))
        session.commit()
    proof = bot._soft_rest_boot_proofs({"AIXI": OPP})
    assert proof["AIXI"]["never_dispatched"] is False


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("strategy_code", "orb"),
    ("kind", "unknown_cancel"), ("attempt_token", "pending-wire"), ("account_name", PRIMARY)])
def test_dispatch_begin_must_be_exact_positive_protocol(field, value):
    bot, sessions, *_ = sql_bot()
    with sessions() as session:
        session.query(DashboardSnapshot).one().payload = {**BEGIN, field: value}
        session.commit()
    assert bot._soft_rest_boot_proofs({"AIXI": OPP})["AIXI"]["never_dispatched"] is False


@pytest.mark.parametrize("budget", ["closed", "unreadable"])
def test_boot_retirement_never_clears_a_closed_or_unknown_retry_budget(budget):
    strategy, _, _, _, proof = boot()
    if budget == "closed":
        strategy._restored_retry_one_budgets["AIXI"] = (SEGMENT, 1)
    else:
        strategy._retry_one_budget_restore_readable = False
    strategy.recover_soft_rest_boot(strategy.soft_rest_boot_candidates(), proof)
    assert strategy.watchlist_state("AIXI").flip_owner_phase == "resting"
