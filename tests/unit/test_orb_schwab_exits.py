from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import Fill, StrategyBarHistory
from project_mai_tai.orb_paper_lifecycle import compute_paper_atr_trail, forming_bar_body_pct
from project_mai_tai.orb_schwab_exits import (
    ATR_REASON, ATR_SOURCE, BODY_REASON, OrbExitTape, confirmed_entry_time,
    exit_signal, schwab_completed_atr_bars,
)
from project_mai_tai.strategy_core.orb_intrabar import OrbBar

OPEN = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)
FILL_AT = OPEN + timedelta(seconds=10)
NOW = OPEN + timedelta(minutes=2, seconds=1)


def _bars():
    first = OPEN - timedelta(minutes=8)
    return [OrbBar(timestamp=first + timedelta(minutes=i), open=10, high=10.1,
                   low=9.9 if i < 9 else 8.8, close=10 if i < 9 else 8.9, volume=100)
            for i in range(10)]


def _tape(final=10.1):
    tape = OrbExitTape(OPEN - timedelta(minutes=5))
    for seconds, price in ((0, 10), (2, 11), (5, 9), (10, final)):
        tape.trade(OPEN + timedelta(seconds=seconds), price, 100)
    return tape


def _context(final=10.1, bars=None):
    return _tape(final).evidence("fill-1", FILL_AT, NOW,
                               atr_bars=_bars() if bars is None else bars, atr_status="complete")


def test_body_under_45_matches_paper_formula_and_has_priority_over_atr():
    context = _context()
    assert forming_bar_body_pct(open_price=10, high=11, low=9, close=10.1) < 45
    assert compute_paper_atr_trail(_bars())[-1]["flip"] == "SELL"
    assert exit_signal(context, NOW) == (BODY_REASON, FILL_AT)


@pytest.mark.parametrize("close,exits", [(108.9, True), (109, False), (109.1, False)])
def test_exact_45_body_boundary(close, exits):
    tape = OrbExitTape(OPEN - timedelta(minutes=5))
    for seconds, price in ((0, 100), (2, 120), (5, 100), (10, close)):
        tape.trade(OPEN + timedelta(seconds=seconds), price, 100)
    context = tape.evidence("fill-1", FILL_AT, NOW, atr_bars=[], atr_status="unknown")
    assert (exit_signal(context, NOW)[0] == BODY_REASON) is exits


def test_body_uses_prefix_at_broker_fill_not_later_completed_candle():
    tape = _tape()
    tape.trade(FILL_AT + timedelta(seconds=1), 20, 100)
    evidence = tape.evidence("fill-1", FILL_AT, NOW, atr_bars=[], atr_status="unknown")
    assert evidence["body"]["close"] == 10.1
    assert evidence["body"]["high"] == 11
    assert evidence["reason"] == BODY_REASON


def test_restart_restores_persisted_body_instead_of_recreating_it_from_new_ticks():
    old = _context()
    restarted = OrbExitTape(NOW)
    new = restarted.evidence("fill-1", FILL_AT, NOW, atr_bars=[], atr_status="unknown", prior=old)
    assert new["body"] == old["body"] and new["reason"] == BODY_REASON


def test_unknown_body_does_not_become_a_passing_body_or_an_exit():
    tape = OrbExitTape(FILL_AT + timedelta(seconds=1))
    evidence = tape.evidence("fill-1", FILL_AT, NOW, atr_bars=[], atr_status="unknown")
    assert evidence["body"] is None and evidence["reason"] is None


def test_truncated_fill_minute_fails_closed_instead_of_computing_a_partial_body():
    tape = OrbExitTape(OPEN - timedelta(minutes=5), max_ticks=2)
    for seconds in (0, 1, 2):
        tape.trade(OPEN + timedelta(seconds=seconds), 10, 100)
    assert tape.body_at(FILL_AT) is None


def test_unknown_body_does_not_suppress_a_proven_schwab_atr_exit():
    tape = OrbExitTape(FILL_AT + timedelta(seconds=1))
    evidence = tape.evidence("fill-1", FILL_AT, NOW, atr_bars=_bars(), atr_status="complete")
    assert evidence["body"] is None
    assert exit_signal(evidence, NOW) == (ATR_REASON, OPEN + timedelta(minutes=2))


def test_schwab_atr_reuses_exact_paper_5_35_wilders_math_on_completed_input():
    evidence = _context(final=11)
    assert compute_paper_atr_trail(_bars())[-1]["flip"] == "SELL"
    assert evidence["atr_source"] == ATR_SOURCE
    assert exit_signal(evidence, NOW) == (ATR_REASON, OPEN + timedelta(minutes=2))


def test_atr_flip_before_the_fill_never_closes_a_later_entry():
    filled = OPEN + timedelta(minutes=2, seconds=1)
    tape = OrbExitTape(OPEN)
    tape.trade(filled.replace(second=0), 10, 100)
    tape.trade(filled, 11, 100)
    evidence = tape.evidence("fill-2", filled, NOW, atr_bars=_bars(), atr_status="complete")
    assert evidence["reason"] is None


def test_intrabar_purple_is_not_an_atr_exit():
    evidence = _context(final=11)
    evidence["atr_bars"][-1]["at"] = NOW.replace(second=0).isoformat()
    with pytest.raises(ValueError, match="completed"):
        exit_signal(evidence, NOW)


def test_gateway_bars_cannot_impersonate_schwab_atr():
    evidence = _context(final=11)
    evidence["atr_source"] = "ORB_GATEWAY_TRADE_TICKS"
    with pytest.raises(ValueError, match="wrong_atr_source"):
        exit_signal(evidence, NOW)


def test_a_post_fill_body_timestamp_is_rejected():
    evidence = _context()
    evidence["body"]["last_trade_at"] = (FILL_AT + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="post_fill"):
        exit_signal(evidence, NOW)


def _factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


def _write(factory, bars, code="schwab_1m_v2", source="live"):
    with factory.begin() as session:
        for bar in bars:
            session.add(StrategyBarHistory(
                strategy_code=code, symbol="CLRO", interval_secs=60, bar_time=bar.timestamp,
                open_price=bar.open, high_price=bar.high, low_price=bar.low,
                close_price=bar.close, volume=int(bar.volume), source=source,
            ))


def test_database_read_uses_schwab_ohlc_not_synthetic_close_only_or_gateway():
    factory = _factory()
    _write(factory, _bars())
    _write(factory, _bars(), code="orb")
    future = replace(_bars()[-1], timestamp=NOW.replace(second=0))
    _write(factory, [future])
    bars, status = schwab_completed_atr_bars(factory, "CLRO", NOW)
    assert status == "complete" and len(bars) == 10
    assert bars[-1].high == 10.1 and bars[-1].low == 8.8 and bars[-1].close == 8.9
    assert compute_paper_atr_trail(bars)[-1]["flip"] == "SELL"


def test_missing_schwab_minute_does_not_fall_back_to_gateway_or_forming_bar():
    factory = _factory()
    _write(factory, _bars()[:-1])
    _write(factory, _bars(), code="orb")
    bars, status = schwab_completed_atr_bars(factory, "CLRO", NOW)
    assert bars == [] and status == "missing_last_closed_schwab_bar"


def test_schwab_database_unreadable_stays_unknown():
    def bad_factory():
        raise RuntimeError("not readable")
    assert schwab_completed_atr_bars(bad_factory, "CLRO", NOW) == ([], "schwab_bar_read_unavailable")


def test_atr_history_is_session_scoped():
    factory = _factory()
    yesterday = [replace(bar, timestamp=bar.timestamp - timedelta(days=1)) for bar in _bars()]
    _write(factory, yesterday)
    assert schwab_completed_atr_bars(factory, "CLRO", NOW)[1] == "missing_last_closed_schwab_bar"


def test_fill_time_requires_broker_execution_evidence_not_generic_report_time():
    fill = Fill(broker_fill_id="broker-1", filled_at=FILL_AT, quantity=Decimal("2"), payload={})
    assert confirmed_entry_time(fill) is None
    fill.payload = {"metadata": {"orb_entry_fill_time_source": "execution_leg",
                                 "orb_entry_first_fill_at": FILL_AT.isoformat()}}
    assert confirmed_entry_time(fill) == FILL_AT
