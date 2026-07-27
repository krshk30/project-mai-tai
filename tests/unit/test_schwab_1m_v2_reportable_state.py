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
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import (
    Base,
    BrokerAccount,
    OmsManagedPosition,
    Strategy,
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
        "positions": [], "pending_open": [], "pending_close": []
    }


def test_a_db_failure_still_lets_the_snapshot_publish() -> None:
    """The SAME payload carries data_health and cw_armed_segments, which the health crons page
    on. A reporting read must never take the whole heartbeat down with it."""
    def boom():
        raise RuntimeError("db down")

    bot = _bot(boom)
    assert bot._fetch_reportable_state() == {
        "positions": [], "pending_open": [], "pending_close": []
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
        session.commit()

    bot = _bot(sf)
    # the Webull-only position is INVISIBLE to the trading read ...
    assert bot._fetch_open_positions() == {}
    # ... while the reporting read is the one that spans accounts
    with sf() as session:
        _managed(session, "QBTX", FANOUT, 1, "8.80")
        session.commit()
    assert [p["leg"] for p in bot._fetch_reportable_state()["positions"]] == ["fanout"]
