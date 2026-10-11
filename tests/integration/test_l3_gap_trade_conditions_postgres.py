"""Real PostgreSQL gap SQL and WFF replay; private schemas and simulated venue only."""

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import (
    Base, BrokerOrder, MarketCaptureTrade, MarketTradeTick,
)
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.services import schwab_1m_v2_bot as service_module
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.session_line_restore import SessionCoverage, history_fingerprint
from tests.line_restore_acceptance_factory import make_line_restore_case
from tests.unit.test_oms_webull_mirror_deferred_resubmit import _FakeRedis


RAW = json.loads((Path(__file__).parents[1] / "fixtures/l3_wff_20261009.json").read_text())
FLAG = "strategy_schwab_1m_v2_gap_trade_conditions_filter_enabled"
PAIR = (1791572940000, 1791573240000)


class AcceptingVenue:
    """External venue boundary: accept a stop without inventing a historical fill."""

    def __init__(self):
        self.requests = []

    async def submit_order(self, request):
        self.requests.append(request)
        return [ExecutionReport(event_type="accepted", client_order_id=request.client_order_id,
            broker_order_id=f"replay-{uuid4().hex}", symbol=request.symbol, side=request.side,
            intent_type=request.intent_type, quantity=request.quantity,
            reason=request.reason, metadata=dict(request.metadata))]


@pytest.fixture
def factory():
    dsn = os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not dsn.startswith("postgresql"):
        pytest.fail("MAI_TAI_DATABASE_URL must name the required PostgreSQL CI service")
    schema = f"l3_gap_{uuid4().hex}"
    admin = create_engine(dsn)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(dsn, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(engine)
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def seed(factory, *, table="capture", provider="massive", conditions="32", stamp=None, symbol="WFF"):
    values = dict(provider=provider, symbol=symbol,
        event_ts=stamp or datetime.fromisoformat("2026-10-09T19:12:42.629+00:00"),
        price=Decimal("14.73"), size=140)
    row = (MarketCaptureTrade(**values, conditions=conditions) if table == "capture" else
           MarketTradeTick(**values, raw=conditions, service="test", raw_hash=uuid4().hex))
    with factory() as session:
        session.add(row)
        session.commit()


def read(factory, *, enabled=True, pairs=(PAIR,)):
    bot = object.__new__(SchwabV2BotService)
    bot.settings = Settings(**{FLAG: enabled})
    bot.session_factory = factory
    return bot._read_line_gap_trades("WFF", pairs)


@pytest.mark.parametrize("provider", ["massive", "polygon"])
@pytest.mark.parametrize("table,conditions", [
    ("capture", "32"), ("capture", "12,32"),
    ("ticks", {"conditions": [32]}), ("ticks", {"c": [12, 32]}),
])
def test_ineligible_print_excluded_only_on(factory, provider, table, conditions):
    seed(factory, provider=provider, table=table, conditions=conditions)
    assert read(factory) == ()
    assert read(factory, enabled=False) == (PAIR,)


@pytest.mark.parametrize("conditions", [
    None, "", "1", "12", "14,41", "999", "32,999", "32,garbage", "32,",
])
def test_regular_extended_hours_and_unknown_capture_still_block(factory, conditions):
    seed(factory, conditions=conditions)
    assert read(factory) == (PAIR,)


@pytest.mark.parametrize("provider", ["schwab", "other", "MASSIVE"])
def test_unknown_provider_labels_cannot_authorize_spanning(factory, provider):
    seed(factory, provider=provider)
    assert read(factory) == (PAIR,)


@pytest.mark.parametrize("raw", [
    {}, {"conditions": None}, {"conditions": "32"}, {"conditions": [32, 999]},
    {"conditions": [32, None]}, {"conditions": [1]}, {"conditions": [12]}, {"c": []},
])
def test_missing_malformed_unknown_and_eligible_raw_ticks_still_block(factory, raw):
    seed(factory, table="ticks", conditions=raw)
    assert read(factory) == (PAIR,)


def test_schwab_native_tape_still_blocks_cabo_type_traded_hole(factory):
    seed(factory, table="ticks", provider="schwab", conditions={"35": 1791573162629})
    seed(factory, conditions="32")
    assert read(factory) == (PAIR,)


@pytest.mark.parametrize("rule", [r for r in RAW["condition_rules"]
    if (r.get("update_rules") or {}).get("consolidated")], ids=lambda r: str(r["id"]))
def test_all_recorded_consolidated_condition_rules(factory, rule):
    seed(factory, conditions=str(rule["id"]))
    creates_bar = rule["id"] == 12 or rule["update_rules"]["consolidated"]["updates_open_close"]
    assert read(factory) == ((PAIR,) if creates_bar else ())


@pytest.mark.parametrize("offset,expected", [(59_999, ()), (60_000, (PAIR,)),
                                           (299_999, (PAIR,)), (300_000, ())])
def test_exact_missing_minute_boundaries(factory, offset, expected):
    seed(factory, conditions="1", stamp=datetime.fromtimestamp((PAIR[0] + offset) / 1000, UTC))
    assert read(factory) == expected


def test_foreign_symbol_and_thin_empty_holes_do_not_block(factory):
    seed(factory, conditions="1", symbol="RETO")
    assert read(factory) == ()


@pytest.mark.parametrize("mutation,conditions", [("bypass", "32"), ("unknown", "32,999"),
                                                ("regular", "1"), ("form_t", "12")])
def test_sql_mutations_are_killed_by_positive_and_negative_controls(factory, monkeypatch, mutation, conditions):
    seed(factory, conditions=conditions)
    real = service_module.text

    def mutated(statement):
        if "WITH gaps" in statement:
            if mutation == "bypass":
                statement = statement.replace("t.provider IN ('massive', 'polygon')", "FALSE")
            elif mutation == "unknown":
                statement = statement.replace("<@ CAST(:known_conditions AS text[])", "IS NOT NULL")
            else:
                statement = statement.replace("CAST(:non_bar_conditions AS text[])",
                    "ARRAY['1']::text[]" if mutation == "regular" else "ARRAY['12']::text[]")
        return real(statement)

    monkeypatch.setattr(service_module, "text", mutated)
    expected = () if mutation == "bypass" else (PAIR,)
    with pytest.raises(AssertionError):
        assert read(factory) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_original_wff_1553_buy_reaches_simulated_accepted_order(factory, monkeypatch, enabled):
    # Market bars and gap print are recorded. Complete provider coverage and a
    # flat account are controlled replay inputs, not historical venue evidence.
    seed(factory)
    rows = RAW["bars"]
    prefix, live_tail, buy_bar = rows[:-12], rows[-12:-2], rows[-2]
    now = int(datetime.fromisoformat(prefix[-1]["created_at"]).timestamp() * 1000)
    case = make_line_restore_case(symbol="WFF", now_ms=now, settings_overrides={
        **RAW["live_flags"], FLAG: enabled,
        "strategy_schwab_1m_v2_account_name": "live:schwab_1m_v2",
        "strategy_schwab_1m_v2_webull_account_name": "live:webull_orb",
        "strategy_schwab_1m_v2_atr_only_mode": True,
        "strategy_schwab_1m_v2_cw_v2_reactive_entry_enabled": True,
    })
    case.bot._persist_bar = lambda *args: None
    case.bot.session_factory = factory
    active = case.bot._configure_fanout_identity_store()
    case.bot._configure_flip_entry_ownership_store(active)
    case.bot._configure_removed_wait_store()
    case.schwab.broker_account_name = "live:schwab_1m_v2"
    case.webull.broker_account_name = case.settings.strategy_schwab_1m_v2_webull_account_name
    for row in prefix:
        await case.feed_bar(row)
    case.bot._rest_warmup_done.add("WFF")
    ledger = case.bot._line_sessions["WFF"]
    bars = [case.bar(row) for row in prefix]
    case.attest(SessionCoverage("schwab_rest_full_session", ledger.anchor_ms,
        bars[-1].timestamp_ms + 60_000, tuple(b.timestamp_ms for b in bars), True,
        history_fingerprint(bars), prefix_complete=True))
    assert await case.rebuild() is enabled
    if not enabled:
        assert ledger.incomplete_reason == "traded_gap_unrecovered"
    assert not case.snapshot()["entry_allowed"]  # No clean live closes yet.
    # Controlled completed startup; the real hold verifier must release it.
    case.bot._boot_state_restoration_complete = True
    case.bot._cw_boot_hold_check()
    assert not case.strategy._entries_held
    for index, row in enumerate(live_tail):
        received = int(datetime.fromisoformat(row["created_at"]).timestamp() * 1000)
        with case.clock(received):
            case.strategy.apply_flip_position_book(case.bot._fetch_flip_position_book())
        if index == len(live_tail) - 1:
            quote = RAW["quotes"][-1]
            stamp = int(datetime.fromisoformat(quote["event_ts"]).timestamp() * 1000)
            with case.clock(received):
                case.strategy.on_quote("WFF", Quote("WFF", float(quote["bid_price"] or 0),
                    float(quote["ask_price"]), float(quote["last_price"]), stamp))
        await case.feed_bar(row, now_ms=received, phase="live")
        await case.rebuild()
        assert case.snapshot()["entry_allowed"] is (enabled and index == 9)
        if index < 9:
            assert not case.schwab.intents  # Nine is still insufficient.
    with case.clock(int(datetime.fromisoformat(buy_bar["created_at"]).timestamp() * 1000)):
        await case.feed_bar(buy_bar, phase="live")
        await case.rebuild()
    drafts = [draft for draft in case.schwab.intents if draft.intent_type == "open"]
    assert bool(drafts) is enabled, case.snapshot()
    if not enabled:
        return
    assert case.snapshot()["state"] == "long"
    state = case.strategy.watchlist_state("WFF")
    assert state.resting_active and state.resting_buy_frozen
    assert state.flip_owner_phase == "awaiting_fill"
    assert len(drafts) == 1
    assert drafts[0].metadata["order_type"] == "STOP_LIMIT"
    assert drafts[0].metadata["stop_price"] == "13.2800"
    adapter = AcceptingVenue()
    service = OmsRiskService(settings=case.settings.model_copy(update={
        "oms_adapter": "simulated", "strategy_schwab_1m_v2_entry_notional_usd": 0,
        "strategy_schwab_1m_v2_webull_entry_notional_usd": 0,
    }), redis_client=_FakeRedis(), session_factory=factory, broker_adapter=adapter)
    from project_mai_tai.oms import service as oms_module
    monkeypatch.setattr(oms_module, "utcnow", lambda: datetime.fromtimestamp(case.now_ms / 1000, UTC))
    for draft in drafts:
        event = TradeIntentEvent(source_service="schwab-1m-v2",
            produced_at=datetime.fromtimestamp(case.now_ms / 1000, UTC),
            payload=TradeIntentPayload(strategy_code="schwab_1m_v2",
                broker_account_name="live:schwab_1m_v2", symbol=draft.symbol, side=draft.side,
                intent_type=draft.intent_type, quantity=draft.quantity, reason=draft.reason,
                metadata=draft.metadata))
        await service._handle_stream_message({"data": event.model_dump_json()})
    with factory() as session:
        orders = session.scalars(select(BrokerOrder).where(BrokerOrder.symbol == "WFF")).all()
        assert len(orders) == 1 and orders[0].status == "accepted"
    assert adapter.requests
    await case.feed_bar(rows[-1], phase="live",
        now_ms=int(datetime.fromisoformat(rows[-1]["created_at"]).timestamp() * 1000))
    await case.rebuild()
    assert len([d for d in case.schwab.intents if d.intent_type == "open"]) == 1
