"""v2 bot-state snapshot: the `positions` / `pending_*` fields the operator actually reads.

Before this, `_publish_bot_state` hardcoded `positions=[]`, `pending_open_symbols=[]`,
`pending_close_symbols=[]` and `daily_pnl=0.0` as LITERALS -- v2 had never reported a position in
its life. Live 2026-07-27 that turned into a real incident: four Webull fan-out legs filled and
closed at the broker while the snapshot insisted `positions: []` and `daily_pnl 0.0`, so the
operator saw trades the dashboard denied existed and closed one by hand.

The fix reads `oms_managed_positions` (the OMS is its sole writer, and it is the same table the
exit ladder runs off) across BOTH broker accounts, so a dual-broker fan-out shows as the two legs
it really is.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import (
    Base,
    BrokerAccount,
    OmsManagedPosition,
    Strategy,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings

PRIMARY = "live:schwab_1m_v2"
FANOUT = "live:orb"


def _session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _bot(session_factory):
    return SchwabV2BotService(
        settings=Settings(strategy_schwab_1m_v2_account_name=PRIMARY),
        session_factory=session_factory,
    )


def _managed(session, symbol, account, qty, entry, *, profit=0.0):
    from datetime import UTC, datetime
    session.add(OmsManagedPosition(
        strategy_code="schwab_1m_v2",
        broker_account_name=account,
        symbol=symbol,
        entry_price=Decimal(str(entry)),
        original_quantity=qty,
        current_quantity=qty,
        entry_time=datetime.now(UTC),
        current_profit_pct=Decimal(str(profit)),
        peak_profit_pct=Decimal(str(profit)),
    ))


def test_a_fanout_trade_reports_BOTH_legs_labelled_by_broker() -> None:
    """THE REGRESSION: one QBTX trade, two brokers -> two legs, each identifiable."""
    sf = _session_factory()
    with sf() as session:
        _managed(session, "QBTX", PRIMARY, 2, "8.8078", profit=1.5)
        _managed(session, "QBTX", FANOUT, 1, "8.80")
        session.commit()

    state = _bot(sf)._fetch_reportable_state()
    legs = state["positions"]
    assert len(legs) == 2, legs
    by_account = {p["broker_account_name"]: p for p in legs}
    assert by_account[PRIMARY]["leg"] == "primary"
    assert by_account[PRIMARY]["quantity"] == 2
    assert by_account[PRIMARY]["entry_price"] == pytest.approx(8.8078)
    assert by_account[PRIMARY]["current_profit_pct"] == pytest.approx(1.5)
    assert by_account[FANOUT]["leg"] == "fanout"        # the Webull side, previously invisible
    assert by_account[FANOUT]["quantity"] == 1


def test_closed_positions_are_not_reported() -> None:
    """current_quantity == 0 is a closed row; it must not linger in the snapshot."""
    sf = _session_factory()
    with sf() as session:
        _managed(session, "LGHL", FANOUT, 0, "1.20")
        session.commit()
    assert _bot(sf)._fetch_reportable_state()["positions"] == []


def test_no_positions_reports_empty_not_error() -> None:
    assert _bot(_session_factory())._fetch_reportable_state() == {
        "positions": [], "pending_open": [], "pending_close": [],
        "closed_today": [], "daily_pnl": 0.0,
    }


def test_a_db_failure_still_lets_the_snapshot_publish() -> None:
    """The SAME payload carries data_health and cw_armed_segments, which the health crons page
    on. A reporting read must never take the whole heartbeat down with it."""
    def boom():
        raise RuntimeError("db down")

    bot = _bot(boom)
    assert bot._fetch_reportable_state() == {
        "positions": [], "pending_open": [], "pending_close": [],
        "closed_today": [], "daily_pnl": 0.0,
    }


def test_trading_logic_read_is_untouched_by_the_reporting_read() -> None:
    """GUARD: `_fetch_open_positions` drives cooldown/re-entry and is scoped to the PRIMARY
    account only. If it ever started seeing the fan-out account, v2 would believe it is
    "in position" on Schwab when only the Webull leg is open -- silently changing ENTRY
    behaviour. The reporting read spans both accounts; this one must not.
    """
    sf = _session_factory()
    with sf() as session:
        primary = BrokerAccount(name=PRIMARY, provider="schwab", environment="live")
        fanout = BrokerAccount(name=FANOUT, provider="webull", environment="live")
        strategy = Strategy(code="schwab_1m_v2", name="V2", execution_mode="live")
        session.add_all([primary, fanout, strategy])
        session.flush()
        session.add(VirtualPosition(
            strategy_id=strategy.id, broker_account_id=fanout.id, symbol="QBTX",
            quantity=1, average_price=Decimal("8.80"),
        ))
        session.add(TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=fanout.id,
            symbol="ORBONLY",
            side="buy",
            intent_type="open",
            quantity=Decimal("1"),
            reason="Webull fan-out leg",
            status="submitted",
            payload={},
        ))
        session.commit()

    bot = _bot(sf)
    # Both Webull-only inputs are INVISIBLE to the trading read: a filled position and the
    # working fan-out intent. The latter is the D3 regression -- it used to enter the union because
    # the intent half filtered only by strategy, even though broker_account_id is already indexed.
    assert bot._fetch_open_positions() == {}
    # ... while the same symbol held in the PRIMARY account is visible.  Both polarities matter:
    # a read that returned empty for every account would also satisfy the assertion above.
    with sf() as session:
        primary = session.scalar(select(BrokerAccount).where(BrokerAccount.name == PRIMARY))
        strategy = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        session.add(VirtualPosition(
            strategy_id=strategy.id, broker_account_id=primary.id, symbol="QBTX",
            quantity=2, average_price=Decimal("8.81"),
        ))
        session.add(TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=primary.id,
            symbol="SCHWABINTENT",
            side="buy",
            intent_type="open",
            quantity=Decimal("3"),
            reason="Schwab primary leg",
            status="submitted",
            payload={},
        ))
        session.commit()
    assert bot._fetch_open_positions() == {"QBTX": 2, "SCHWABINTENT": 3}
    # ... while the reporting read is the one that spans accounts
    with sf() as session:
        _managed(session, "QBTX", FANOUT, 1, "8.80")
        session.commit()
    assert [p["leg"] for p in bot._fetch_reportable_state()["positions"]] == ["fanout"]


# ------------------------------------------- closed round trips + daily_pnl (2026-07-28)
# `daily_pnl` and `closed_today` were the last two hardcoded literals in the snapshot. They are
# now paired FIFO from `fills`. ⭐ PERCENT is the primary figure per the standing output rule --
# one $25 name outweighs sixteen $1-7 names and flips conclusions.

def _fill(session, *, acct, symbol, side, qty, price, minute):
    from datetime import UTC, datetime, timedelta
    from project_mai_tai.db.models import BrokerOrder, Fill, TradeIntent
    strategy = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
    account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == acct))
    intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id, symbol=symbol,
                         side=side, intent_type="open", quantity=Decimal(str(qty)),
                         reason="t", status="filled", payload={})
    session.add(intent)
    session.flush()
    order = BrokerOrder(intent_id=intent.id, strategy_id=strategy.id,
                        broker_account_id=account.id,
                        client_order_id=f"{symbol}-{side}-{minute}-{acct}", symbol=symbol,
                        side=side, order_type="market", time_in_force="day",
                        quantity=Decimal(str(qty)), status="filled", payload={})
    session.add(order)
    session.flush()
    session.add(Fill(order_id=order.id, strategy_id=strategy.id,
                     broker_account_id=account.id, broker_fill_id=f"{symbol}{side}{minute}{acct}",
                     symbol=symbol, side=side, quantity=Decimal(str(qty)),
                     price=Decimal(str(price)),
                     filled_at=datetime.now(UTC) + timedelta(minutes=minute)))


def _seed_accounts(session):
    session.add_all([
        Strategy(code="schwab_1m_v2", name="V2", execution_mode="live"),
        BrokerAccount(name=PRIMARY, provider="schwab", environment="live"),
        BrokerAccount(name=FANOUT, provider="webull", environment="live"),
    ])
    session.commit()


def test_a_completed_round_trip_reports_percent_and_pnl() -> None:
    sf = _session_factory()
    with sf() as session:
        _seed_accounts(session)
        _fill(session, acct=FANOUT, symbol="BIYA", side="buy", qty=1, price="3.859", minute=-30)
        _fill(session, acct=FANOUT, symbol="BIYA", side="sell", qty=1, price="3.930", minute=-27)
        session.commit()
    state = _bot(sf)._fetch_reportable_state()
    assert len(state["closed_today"]) == 1
    t = state["closed_today"][0]
    assert t["profit_pct"] == pytest.approx(1.8399, abs=0.001)   # the real BIYA fan-out trade
    assert t["leg"] == "fanout"
    assert state["daily_pnl"] == pytest.approx(0.071, abs=0.0005)


def test_an_open_position_is_not_counted_as_closed() -> None:
    """An entry with no exit is still HELD -- counting it would invent a realised loss."""
    sf = _session_factory()
    with sf() as session:
        _seed_accounts(session)
        _fill(session, acct=FANOUT, symbol="BIYA", side="buy", qty=1, price="3.859", minute=-5)
        session.commit()
    state = _bot(sf)._fetch_reportable_state()
    assert state["closed_today"] == []
    assert state["daily_pnl"] == 0.0


def test_two_entries_one_segment_pair_FIFO_not_averaged() -> None:
    """THE RECLAIM SHAPE, live from 07-27 onward: a symbol entered twice must pair its exits with
    its entries IN ORDER. Averaging would report one blended trade and hide that the reclaim was
    the loser -- which is the entire question reclaim was re-enabled to answer."""
    sf = _session_factory()
    with sf() as session:
        _seed_accounts(session)
        _fill(session, acct=PRIMARY, symbol="FIEE", side="buy", qty=2, price="5.00", minute=-40)
        _fill(session, acct=PRIMARY, symbol="FIEE", side="buy", qty=2, price="6.00", minute=-30)
        _fill(session, acct=PRIMARY, symbol="FIEE", side="sell", qty=2, price="5.10", minute=-20)
        _fill(session, acct=PRIMARY, symbol="FIEE", side="sell", qty=2, price="5.40", minute=-10)
        session.commit()
    trades = _bot(sf)._fetch_reportable_state()["closed_today"]
    assert len(trades) == 2
    assert trades[0]["entry_price"] == 5.00 and trades[0]["profit_pct"] == pytest.approx(2.0)
    assert trades[1]["entry_price"] == 6.00 and trades[1]["profit_pct"] == pytest.approx(-10.0)


def test_a_zero_priced_fill_never_prices_a_trade() -> None:
    """⛔ the $0 cancelled-OCO-leg artefact: booking it would report -100%."""
    sf = _session_factory()
    with sf() as session:
        _seed_accounts(session)
        _fill(session, acct=FANOUT, symbol="BIYA", side="buy", qty=1, price="3.859", minute=-30)
        _fill(session, acct=FANOUT, symbol="BIYA", side="sell", qty=1, price="0", minute=-27)
        session.commit()
    state = _bot(sf)._fetch_reportable_state()
    assert state["closed_today"] == []
    assert state["daily_pnl"] == 0.0
