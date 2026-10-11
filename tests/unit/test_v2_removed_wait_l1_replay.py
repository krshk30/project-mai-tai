"""Recorded DKI/JZ inputs through v2 and unchanged OMS, controlled transport/books."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import json
from math import ceil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, Fill, StrategyBarHistory, TradeIntent
from project_mai_tai.events import StrategyStateSnapshotEvent, StrategyStateSnapshotPayload, TradeIntentEvent
from project_mai_tai.falseflip1_runtime import FalseFlipStore
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.market_data.massive_atr_seed import MassiveAtrSeedClient
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, SchwabV2RestClient
from project_mai_tai.market_data.schwab_v2_streamer import SchwabTick
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2IntentEmitter
from project_mai_tai.v2_flip_entry_ownership import FlipPositionBook
from tests.unit.test_v2_removed_wait_clear_rules import PRIMARY, WEBULL, db as l1_database  # noqa: F401

db = l1_database

RECORDED = json.loads((Path(__file__).parents[1] / "fixtures/v2_removed_wait_l1_recorded.json").read_text())
LIVE = json.loads((Path(__file__).parents[1] / "fixtures/v2_removed_wait_l1_live_flags.json").read_text())
LINE = json.loads((Path(__file__).parents[1] / "fixtures/v2_removed_wait_l1_line_sources.json").read_text())
REVIEW = json.loads((Path(__file__).parents[1] / "fixtures/v2_removed_wait_l1_reviewer_evidence.json").read_text())


class DatabaseOnlyLineSource:
    """Recorded DB bars, with UNKNOWN provider coverage; never accesses a provider/token."""
    def __init__(self, factory):
        self.factory = factory

    def set_desired_symbols(self, symbols):
        self.desired_symbols = set(symbols)

    def fetch_session_history(self, symbol, anchor, current):
        with self.factory() as session:
            rows = session.scalars(select(StrategyBarHistory).where(
                StrategyBarHistory.symbol == symbol,
                StrategyBarHistory.bar_time >= datetime.fromtimestamp(anchor / 1000, UTC),
                StrategyBarHistory.bar_time <= datetime.fromtimestamp(current / 1000, UTC),
            ).order_by(StrategyBarHistory.bar_time)).all()
            return [ChartBar(symbol, *(float(getattr(r, k + "_price")) for k in
                ("open", "high", "low", "close")), r.volume,
                int(r.bar_time.replace(tzinfo=UTC).timestamp() * 1000)) for r in rows], None

    def _set_line_source_state(self, *_):
        pass


def ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def bar(row):
    return OHLCVBar(ms(row["bar_time"]), *(float(row[k]) for k in
        ("open_price", "high_price", "low_price", "close_price")), int(row["volume"]))


class ControlledTransport:
    """Answers belong to this replay, not to historical or live broker inventory."""
    def __init__(self):
        self.events = []
        self.requests = []

    async def xadd(self, _stream, fields, **_kwargs):
        self.events.append(json.loads(fields["data"]))
        return "1-0"

    async def get(self, _key):
        return None

    async def set(self, *_args, **_kwargs):
        return True

    async def submit_order(self, request):
        self.requests.append(request)
        return [ExecutionReport(event_type="accepted", client_order_id=request.client_order_id,
            broker_order_id="controlled-" + request.client_order_id, symbol=request.symbol, side=request.side,
            intent_type=request.intent_type, quantity=request.quantity, reason=request.reason,
            metadata=request.metadata, origin="broker")]

    async def list_account_positions(self, _account):
        return []

    async def fetch_order_update(self, _request):
        return None


async def replay_case(db, monkeypatch, name, enabled, *, native_line=False):
    store, factory, accounts, strategies = db
    case = RECORDED[name]
    symbol = name.upper()
    start = "2026-10-08T11:28:42Z" if name == "dki" else "2026-10-09T18:19:51.846Z"
    clock = [int(case["request"]["payload"]["requested_at_ms"]) if native_line and name == "jz"
             else ms(case["bars"][0]["created_at"]) if native_line else ms(start)]
    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class Clock(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0] / 1000, tz or UTC)

    import project_mai_tai.strategy_core.schwab_1m_v2 as core
    import project_mai_tai.services.schwab_1m_v2_bot as bot_module
    import project_mai_tai.oms.service as oms_module
    import project_mai_tai.v2_removed_wait as removed_module
    monkeypatch.setattr(core, "datetime", Clock)
    monkeypatch.setattr(bot_module, "datetime", Clock)
    monkeypatch.setattr(removed_module, "datetime", Clock)
    monkeypatch.setattr(oms_module, "utcnow", lambda: Clock.now(UTC))
    settings = Settings(_env_file=None,
        strategy_schwab_1m_v2_removed_wait_clear_rules_enabled=enabled,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
        strategy_schwab_1m_v2_go_live_enabled=True,
        strategy_schwab_1m_v2_atr_flip_enabled=True,
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct=.5,
        strategy_schwab_1m_v2_cw_v2_resting_entry_band_pct=.5,
        strategy_schwab_1m_v2_pm_print_ask_confirm_enabled=True,
        strategy_schwab_1m_v2_pm_flip_wait_enabled=True,
        strategy_schwab_1m_v2_pm_rest_reprice_enabled=True,
        strategy_schwab_1m_v2_resting_buy_round_up_enabled=True,
        strategy_schwab_1m_v2_keep_rest_after_buy_enabled=True,
        strategy_schwab_1m_v2_gap_hold_enabled=True,
        strategy_schwab_1m_v2_retry_one_enabled=True,
        strategy_schwab_1m_v2_retry_one_max_retries=0,
        strategy_schwab_1m_v2_account_name=PRIMARY,
        strategy_schwab_1m_v2_webull_account_name=WEBULL,
        oms_adapter="simulated")
    if native_line:
        options = {key.removeprefix("MAI_TAI_").lower():
                   (value == "true" if value in {"true", "false"} else int(value))
                   for key, value in LIVE["flags"].items()}
        assert set(options) <= set(Settings.model_fields)
        settings = Settings(_env_file=None, **{**settings.model_dump(), **options, **LIVE["accounts"]})
        assert len(options) == 40 and settings.strategy_schwab_1m_v2_atr_only_mode
        for key, value in options.items():
            assert getattr(settings, key) == value
    bot = SchwabV2BotService(settings, session_factory=factory)
    strat = bot.strategy
    strat._now_ms = lambda: clock[0]
    bot._configure_flip_entry_ownership_store(bot._configure_fanout_identity_store())
    if native_line:
        assert isinstance(bot._falseflip_store, FalseFlipStore)
        assert strat._falseflip_readable is True
        print(f"native {symbol} FalseFlipStore_restore=True readable={strat._falseflip_readable} "
              f"restored_budgets={len(strat._falseflip_budgets)} disposable_database=True")
    # Replay the historical request under its original type, explicitly counterfactual
    # local restoration. The actual box currently has zero active requests.
    with factory() as session:
        session.add(DashboardSnapshot(snapshot_type="v2_removed_wait", created_at=Clock.now(UTC),
                                       payload=case["request"]["payload"]))
        for row in case.get("receipts", case.get("intents", [])):
            payload = row.get("payload", {"metadata": row.get("metadata", {}),
                "refusal_origin": row.get("refusal_origin"), "refusal_code": row.get("refusal_code")})
            account = row.get("account") or next(n for ident, n in RECORDED["jz"].get("accounts", {}).items()
                                                 if ident == row["broker_account_id"])
            session.add(TradeIntent(id=UUID(row["id"]), strategy_id=strategies["schwab_1m_v2"],
                broker_account_id=accounts[account], symbol=symbol, side="buy", intent_type="cancel",
                quantity=row["quantity"], status=row["status"], reason="recorded cancel receipt",
                payload=payload, created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"])))
        if name == "jz" and native_line:
            evidence = REVIEW["jz"]
            assert evidence["oct9_broker_orders"] == 0
            assert evidence["account_positions"] == evidence["virtual_positions"] == evidence["managed_open_positions"] == 0
            for row in evidence["historical_orders"]:
                session.add(BrokerOrder(id=UUID(row["id"]), strategy_id=strategies["schwab_1m_v2"],
                    broker_account_id=accounts[row["account"]], symbol=symbol, side=row["side"],
                    order_type=row["order_type"], time_in_force=row["time_in_force"], quantity=row["quantity"],
                    status=row["status"], client_order_id=row["client_order_id"], payload=row["payload"],
                    submitted_at=datetime.fromisoformat(row["submitted_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"])))
            for row in evidence["historical_fills"]:
                session.add(Fill(id=UUID(row["id"]), order_id=UUID(row["order_id"]),
                    strategy_id=strategies["schwab_1m_v2"], broker_account_id=accounts[row["account"]],
                    symbol=symbol, side=row["side"], quantity=row["quantity"], price=row["price"],
                    filled_at=datetime.fromisoformat(row["filled_at"])))
            for row in case["bars"]:
                if ms(row["created_at"]) > clock[0]:
                    continue
                session.add(StrategyBarHistory(strategy_code="schwab_1m_v2", symbol=symbol,
                    interval_secs=60, bar_time=datetime.fromisoformat(row["bar_time"]),
                    **{k: row[k] for k in ("open_price", "high_price", "low_price", "close_price", "volume")},
                    created_at=datetime.fromisoformat(row["created_at"]), source="live"))
        session.commit()
    transport = ControlledTransport()
    bot.redis = transport
    bot.intent_emitter = SchwabV2IntentEmitter(settings, transport, PRIMARY)
    bot.webull_intent_emitter = SchwabV2IntentEmitter(settings, transport, WEBULL)
    await bot._configure_removed_wait_store()
    cleared_at = None
    if native_line and name == "jz" and enabled:
        req = strat._removed_wait_requests[symbol]
        assert req.opportunity_id == 0
        clock[0] = req.requested_at_ms + 14_999
        await bot._removed_wait_poll()
        assert symbol in strat._removed_wait_requests
        # The recorded cancel rows settle slightly later than the request itself.
        first_valid = max(req.requested_at_ms,
                          *(ceil(datetime.fromisoformat(r["updated_at"]).timestamp() * 1000)
                            for r in case["receipts"])) + 15_000
        clock[0] = first_valid - 1
        await bot._removed_wait_poll()
        assert symbol in strat._removed_wait_requests
        clock[0] = first_valid
        await bot._removed_wait_poll()
        assert not strat._removed_wait_requests and store.restore() == {}
        cleared_at = first_valid
        print(f"JZ opportunity_id=0 first_valid_poll_CLEAR={first_valid} "
              f"requested_at_ms={req.requested_at_ms} settle_ms={first_valid - req.requested_at_ms}")
    if native_line and name == "jz":
        clock[0] = ms(start)
    strat.scanner_readded(symbol)
    if native_line:
        source = LINE[name]
        bot.tick_writer = None  # Offline replay does not run the capture writer.
        if name == "dki":
            bot.rest_client = SchwabV2RestClient(settings, on_chart_bar=None, on_quote=None)
            bot.rest_client._authorized_get = lambda _url: source["schwab"]
            provider = Mock(get_aggs=Mock(return_value=SimpleNamespace(data=json.dumps(source["preopen"]).encode())))
            bot._atr_massive_seed_client = MassiveAtrSeedClient("recorded-not-a-live-key", rest_client=provider)
        else:
            bot.rest_client = DatabaseOnlyLineSource(factory)
        event = StrategyStateSnapshotEvent(source_service="recorded-scanner", produced_at=Clock.now(UTC),
            payload=StrategyStateSnapshotPayload(watchlist=[symbol],
                top_confirmed=[{"symbol": symbol, **({"confirmed_at": source["added_at"]}
                                                    if "added_at" in source else {})}]))
        bot._apply_strategy_state_event({"data": event.model_dump_json()}, max_watchlist=1)
        assert bot._watchlist == {symbol} and bot._boot_exclusion_sources_readable
    state = strat.watchlist_state(symbol)
    admitted = []
    actual_admission = strat._strict_first_rest_admitted
    def observe_admission(state, *, slot):
        allowed = actual_admission(state, slot=slot)
        admitted.append((clock[0], state.symbol, allowed, strat._removed_wait_gate_closed(state.symbol)))
        return allowed
    monkeypatch.setattr(strat, "_strict_first_rest_admitted", observe_admission)
    for row in case["bars"]:
        current = bar(row)
        if name == "dki" and current.timestamp_ms > ms("2026-10-08T11:44Z"):
            break
        if name == "jz" and current.timestamp_ms > case["sell"]["bar_time_ms"] + 3 * 60_000:
            break
        clock[0] = max(clock[0], ms(row["created_at"]))
        if native_line and name == "jz":
            with factory() as session:
                existing = session.scalar(select(StrategyBarHistory.id).where(
                    StrategyBarHistory.symbol == symbol,
                    StrategyBarHistory.bar_time == datetime.fromisoformat(row["bar_time"])))
                if existing is None:
                    session.add(StrategyBarHistory(strategy_code="schwab_1m_v2", symbol=symbol,
                        interval_secs=60, bar_time=datetime.fromisoformat(row["bar_time"]),
                        **{k: row[k] for k in ("open_price", "high_price", "low_price", "close_price", "volume")},
                        created_at=datetime.fromisoformat(row["created_at"]), source="live"))
                    session.commit()
        strat.apply_flip_position_book(FlipPositionBook(clock[0], True, {}))  # controlled empty book
        await bot._removed_wait_poll()
        phase = "replay" if current.timestamp_ms < ms(start) - 60_000 else "live"
        if name == "jz":
            phase = "replay" if current.timestamp_ms < case["sell"]["bar_time_ms"] else "live"
        if native_line:
            native = ChartBar(symbol, current.open, current.high, current.low, current.close,
                              current.volume, current.timestamp_ms)
            await bot._handle_bar_from_rest(symbol, native)
            bot._try_complete_boot_state_restoration({symbol}, new_symbols=set())
            bot._cw_boot_hold_check()
            await bot._line_restoration_pass()
            if symbol in bot._line_source_pending:
                await bot._line_source_events_pass()
                await bot._line_restoration_pass()
        else:
            bot._strategy_on_bar(symbol, current, observation_phase=phase)
    primary = []
    assert bool(strat._removed_wait_requests) is (not enabled)
    if name == "dki" and native_line:
        callbacks = [(ms(row["received_at"]), "trade", row) for row in source["stream"]["trades"]]
        callbacks += [(ms(row["received_at"]), "quote", row) for row in source["stream"]["quotes"]]
        callbacks += [(ms(row["created_at"]), "bar", row) for row in case["bars"]
                      if ms(row["bar_time"]) >= ms("2026-10-08T11:45Z")]
        for received, kind, row in sorted(callbacks, key=lambda item: item[0]):
            if received < clock[0]:
                continue
            clock[0] = received
            strat.apply_flip_position_book(FlipPositionBook(clock[0], True, {}))
            await bot._removed_wait_poll()
            if kind == "bar":
                current = bar(row)
                await bot._handle_bar_from_rest(symbol, ChartBar(symbol, current.open, current.high,
                    current.low, current.close, current.volume, current.timestamp_ms))
                await bot._line_restoration_pass()
            else:
                if kind == "trade" and ms(row["event_ts"]) == ms(case["cross"]["schwab_trade"]["event_ts"]):
                    print(f"native DKI original_cross received_ms={clock[0]} "
                          f"last_closed_bar_ms={state.bars[-1].timestamp_ms} "
                          f"required_closed_bar_ms={clock[0] // 60_000 * 60_000 - 60_000} "
                          f"line_buy_ready={strat.line_buy_ready(symbol)}")
                await bot._handle_stream_tick(SchwabTick(kind=kind, service=row["service"],
                    symbol=symbol, event_ts_ms=ms(row["event_ts"]), raw=row["raw"], raw_hash=row["raw_hash"],
                    price=float(row["price"]) if kind == "trade" else None,
                    size=row.get("size")))
                if bot._eh_stream_emit_tasks:
                    await asyncio.gather(*tuple(bot._eh_stream_emit_tasks))
        ledger = bot._line_sessions[symbol]
        print(f"native DKI provider_complete={getattr(ledger._coverage, 'complete', None)} "
              f"LINE_published={symbol in bot._line_published} reason={ledger.incomplete_reason} "
              f"fallback={state.line_live_fallback} events={len(transport.events)} "
              f"falseflip_readable={strat._falseflip_readable} "
              f"falseflip_entry_blocked={strat._falseflip_entry_blocked(state)}")
        print("native DKI events=" + json.dumps([{
            "event_type": e.get("event_type"),
            "intent_type": (e.get("payload") or {}).get("intent_type"),
            "account": (e.get("payload") or {}).get("broker_account_name")
        } for e in transport.events], default=str))
        if enabled:
            assert not strat._removed_wait_gate_closed(symbol)
            assert state.cw_arm_bar_ts == ms("2026-10-08T11:45Z")
            assert strat._flip_owner_evidence_fresh(state) and not strat._removed_wait_has_owner(state)
            assert not strat._falseflip_entry_blocked(state)
            assert any(allowed and not blocked for _, _, allowed, blocked in admitted)
            assert state.line_live_fallback and not state.gap_hold_active
            assert not state.resting_flip_ms and not transport.requests
            return {"admitted": admitted, "state": state, "events": transport.events}
    elif name == "dki":
        tick = case["cross"]["schwab_trade"]
        clock[0] = ms(tick["received_at"])
        strat.apply_flip_position_book(FlipPositionBook(clock[0], True, {}))
        draft = strat.on_stream_trade(symbol, float(tick["price"]), ms(tick["event_ts"]),
            ask_price=float(tick["raw"]["2"]), bid_price=float(tick["raw"]["1"]),
            print_size=tick["size"], ask_age_ms=clock[0] - ms(tick["event_ts"]))
        if draft is not None:
            primary.append(draft)
    primary.extend(strat.drain_pending_intents())
    mirror = strat.drain_webull_direct_intents() + strat.drain_webull_fanout_intents()
    opens = [(bot.intent_emitter, d) for d in primary if d.intent_type == "open"]
    opens += [(bot.webull_intent_emitter, d) for d in mirror if d.intent_type == "open"]
    if not enabled:
        assert not opens and strat._removed_wait_gate_closed(symbol)
        assert not any(e.get("payload", {}).get("intent_type") == "open" for e in transport.events)
        return
    if native_line and name == "jz":
        assert not strat._removed_wait_gate_closed(symbol)
        assert any(allowed and not blocked for _, _, allowed, blocked in admitted)
        assert state.line_live_fallback and not strat._falseflip_entry_blocked(state)
        assert any(e.get("payload", {}).get("intent_type") == "open" for e in transport.events), (
            state.resting_active, state.resting_level, state.flip_owner_phase)
        return {"admitted": admitted, "state": state, "events": transport.events, "cleared_at": cleared_at}
    assert opens or transport.events, (state.atr_state, state.atr_trail, state.resting_active, state.resting_level,
                   state.flip_owner_phase, state.gap_hold_active)
    # JZ's 15:04 SELL must create a normal resting BUY. DKI's 07:45 candle
    # remains software-rested until the recorded 07:46:00.728 Schwab callback.
    for emitter, draft in opens:
        assert draft.quantity > 0 and draft.side == "buy"
        assert await bot._emit_removal_tracked(emitter, draft)
    events = [TradeIntentEvent.model_validate(e) for e in transport.events if e.get("event_type") == "trade_intent"]
    oms = OmsRiskService(settings, transport, session_factory=factory, broker_adapter=transport)
    if name == "dki":
        if native_line:
            quote = max((q for q in source["stream"]["quotes"] if ms(q["received_at"]) <= clock[0]),
                        key=lambda q: ms(q["received_at"]))
            oms._latest_quotes_by_symbol[symbol] = {"ask": float(quote["ask_price"]),
                "bid": float(quote["bid_price"]), "received_at": datetime.fromisoformat(quote["received_at"])}
        else:
            tick = case["cross"]["schwab_trade"]
            oms._latest_quotes_by_symbol[symbol] = {"ask": float(tick["raw"]["2"]),
                "bid": float(tick["raw"]["1"]), "received_at": datetime.fromisoformat(tick["received_at"])}
    for event in events:
        await oms.process_trade_intent(event)
    with factory() as session:
        accepted = session.scalars(select(BrokerOrder).where(BrokerOrder.symbol == symbol,
                                                            BrokerOrder.status == "accepted")).all()
        assert accepted, [(r.status, r.payload) for r in session.scalars(select(BrokerOrder))]
        assert all(r.side == "buy" and r.quantity > Decimal(0) for r in accepted)
        print(f"L1 recorded {symbol} rules_ON accepted_BUY={len(accepted)} "
              f"quantities={[str(r.quantity) for r in accepted]} controlled_broker_answers=True")
    assert transport.requests


@pytest.mark.parametrize("name", ["dki", "jz"])
@pytest.mark.parametrize("enabled", [True, False], ids=["rules_ON", "rollback_OFF"])
@pytest.mark.asyncio
async def test_original_recorded_case_ends_accepted_buy(db, monkeypatch, name, enabled):
    await replay_case(db, monkeypatch, name, enabled)


@pytest.mark.parametrize("name", ["dki", "jz"])
@pytest.mark.parametrize("enabled", [True, False], ids=["rules_ON", "rollback_OFF"])
@pytest.mark.asyncio
async def test_reviewer_done_checks_actual_live_flags_native_path(db, monkeypatch, caplog, name, enabled):
    result = await replay_case(db, monkeypatch, name, enabled, native_line=True)
    if not enabled:
        return
    assert "verdict=CLEAR reason=" in caplog.text
    assert "reason=scanner_removed_waiting_buy" not in caplog.text
    if name == "dki":
        assert "reason=ask_past_band ask=4.8300 cap=4.8100" in caplog.text
        assert "reason=ask_past_band ask=4.8500 cap=4.8100" in caplog.text
        witness = REVIEW["dki"]["raw_log_witness"]
        assert REVIEW["dki"]["db_created_at_is_replay_proxy"] is True
        assert any("11:46:02,861" in line and "flip=BUY" in line for line in witness)
        assert any("11:46:02,862" in line and "[V2-CW-ARM]" in line for line in witness)
        print("DKI done: stale request CLEAR; normal admission allowed; asks 4.83/4.85 > cap 4.81; "
              "DB created_at is arrival proxy, separate UTC PROBE/ARM witness 11:46:02.861/.862")
    else:
        assert result["cleared_at"] is not None
        opens = [e["payload"] for e in result["events"] if e.get("payload", {}).get("intent_type") == "open"]
        assert {e["broker_account_name"] for e in opens} == {PRIMARY, WEBULL}
        assert all(e["side"] == "buy" and e["metadata"].get("resting_entry") == "true"
                   and e["metadata"]["order_type"] == "STOP_LIMIT"
                   and Decimal(e["metadata"]["stop_price"]) == Decimal("1.41")
                   and Decimal(e["metadata"]["limit_price"]) == Decimal("1.42") for e in opens), json.dumps(opens)
        assert "bar_time_ms=1791572640000" in caplog.text
        assert "[V2-FLIP-OWNER-ADMISSION] JZ evaluated=1 refused_unknown=0 allowed=1" in caplog.text
        print("JZ done: rule A opportunity 0 CLEAR, original 15:04 SELL reaches normal waiting BUY; "
              "DB-only replay, provider coverage UNKNOWN, no provider/credential read")
