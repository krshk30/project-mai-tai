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
    ATR_REASON, ATR_SOURCE, BODY_REASON, completed_bar_evidence, confirmed_entry_time,
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


def _context(final=10, bars=None):
    bars = _bars() if bars is None else bars
    bars = [replace(bar, close=final) if bar.timestamp == OPEN else bar for bar in bars]
    return completed_bar_evidence("fill-1", FILL_AT, NOW, atr_bars=bars, atr_status="complete")


def test_body_under_45_matches_paper_formula_and_has_priority_over_atr():
    context = _context()
    assert forming_bar_body_pct(open_price=10, high=11, low=9, close=10.1) < 45
    assert compute_paper_atr_trail(_bars())[-1]["flip"] == "SELL"
    assert exit_signal(context, NOW) == (BODY_REASON, OPEN + timedelta(minutes=1))


@pytest.mark.parametrize("close,exits", [(108.98, True), (109, False), (109.1, False)])
def test_exact_45_body_boundary(close, exits):
    bar = OrbBar(timestamp=OPEN, open=100, high=120, low=100, close=close, volume=100)
    context = completed_bar_evidence("fill-1", FILL_AT, NOW, atr_bars=[bar], atr_status="unknown")
    assert (exit_signal(context, NOW)[0] == BODY_REASON) is exits


def test_body_uses_completed_candle_not_prefix_at_broker_fill():
    # At fill a prefix could have a tiny body; the completed Schwab bar is 50%.
    bar = OrbBar(timestamp=OPEN, open=10, high=11, low=9, close=11, volume=100)
    early = completed_bar_evidence("fill-1", FILL_AT, FILL_AT, atr_bars=[bar], atr_status="unknown")
    assert early["body"] is None and early["reason"] is None
    evidence = completed_bar_evidence("fill-1", FILL_AT, NOW, atr_bars=[bar], atr_status="unknown")
    assert evidence["body"]["close"] == 11 and evidence["reason"] is None


def test_restart_restores_persisted_body_instead_of_recreating_it_from_new_ticks():
    old = _context()
    new = completed_bar_evidence("fill-1", FILL_AT, NOW, atr_bars=[], atr_status="unknown", prior=old)
    assert new["body"] == old["body"] and new["reason"] == BODY_REASON


def test_unknown_body_does_not_become_a_passing_body_or_an_exit():
    evidence = completed_bar_evidence("fill-1", FILL_AT, NOW, atr_bars=[], atr_status="unknown")
    assert evidence["body"] is None and evidence["reason"] is None


def test_no_body_decision_before_close_even_if_context_contains_bar():
    assert exit_signal(_context(), FILL_AT) == (None, None)


def test_missing_break_bar_keeps_bracket_even_with_a_later_atr_flip():
    evidence = completed_bar_evidence("fill-1", FILL_AT, NOW,
                                     atr_bars=[bar for bar in _bars() if bar.timestamp != OPEN], atr_status="complete")
    assert evidence["body"] is None
    assert exit_signal(evidence, NOW) == (None, None)


@pytest.mark.parametrize("seconds,pending", [(3.5, True), (90, True), (91, False)])
def test_break_bar_waits_90_seconds_from_its_close(seconds, pending):
    now = OPEN + timedelta(minutes=1, seconds=seconds)
    evidence = completed_bar_evidence("fill-1", FILL_AT, now, atr_bars=[],
                                     atr_status="missing_last_closed_schwab_bar")
    assert evidence["body_status"] == ("pending_break_bar" if pending else "missing_completed_schwab_break_bar")
    assert exit_signal(evidence, now) == (None, None)


def test_atr_pending_deadline_survives_minute_roll_and_context_reload():
    import json

    # Break body is known and >=45%; only the later ATR minute is missing.
    bars = [replace(bar, close=10.1) if bar.timestamp == OPEN else bar for bar in _bars()[:-1]]
    close = OPEN + timedelta(minutes=2)
    prior = None
    for seconds in (3.5, 61, 90, 91):
        now = close + timedelta(seconds=seconds)
        evidence = completed_bar_evidence("fill-1", FILL_AT, now, atr_bars=bars,
                                         atr_status="missing_last_closed_schwab_bar", prior=prior)
        assert evidence["atr_status"] == ("pending_last_closed_schwab_bar" if seconds <= 90
                                           else "missing_completed_schwab_atr_bar")
        assert (OPEN + timedelta(minutes=1)).isoformat() in evidence["atr_missing_minutes"]
        assert evidence["reason"] is None
        prior = json.loads(json.dumps(evidence))
    # A newer bar arriving does not prove the earlier missing minute arrived.
    newer = replace(bars[-1], timestamp=OPEN + timedelta(minutes=2))
    evidence = completed_bar_evidence("fill-1", FILL_AT, now, atr_bars=[*bars, newer],
                                     atr_status="complete", prior=prior)
    assert evidence["atr_status"] == "missing_completed_schwab_atr_bar"
    # Only the actual missing minute clears it; a new trip does not inherit it.
    restored = [*bars, replace(bars[-1], timestamp=OPEN + timedelta(minutes=1)), newer]
    recovered = completed_bar_evidence("fill-1", FILL_AT, now, atr_bars=restored,
                                     atr_status="complete", prior=evidence)
    assert recovered["atr_status"] == "complete" and recovered["atr_missing_minutes"] == []
    fresh = completed_bar_evidence("fill-2", FILL_AT, now, atr_bars=[*bars, newer],
                                 atr_status="complete", prior=evidence)
    assert fresh["atr_status"] == "complete"


def test_pending_body_cannot_sell_even_if_an_inconsistent_context_contains_ohlc():
    evidence = _context()
    evidence["body_status"] = "pending_break_bar"
    assert exit_signal(evidence, NOW) == (None, None)


def test_schwab_atr_reuses_exact_paper_5_35_wilders_math_on_completed_input():
    evidence = _context(final=10.1)
    assert compute_paper_atr_trail(_bars())[-1]["flip"] == "SELL"
    assert evidence["atr_source"] == ATR_SOURCE
    assert exit_signal(evidence, NOW) == (ATR_REASON, OPEN + timedelta(minutes=2))


def test_atr_flip_before_the_fill_never_closes_a_later_entry():
    filled = OPEN + timedelta(minutes=2, seconds=1)
    evidence = completed_bar_evidence("fill-2", filled, NOW, atr_bars=_bars(), atr_status="complete")
    assert evidence["reason"] is None


def test_atr_sell_on_break_bar_itself_is_not_a_later_bar_exit():
    bars = _bars()
    filled = bars[-1].timestamp + timedelta(seconds=10)
    evidence = completed_bar_evidence("fill-2", filled, NOW, atr_bars=bars, atr_status="complete")
    assert compute_paper_atr_trail(bars)[-1]["flip"] == "SELL"
    assert exit_signal(evidence, NOW) == (None, None)  # break body >45%, hold


def test_intrabar_purple_is_not_an_atr_exit():
    evidence = _context(final=10.1)
    evidence["atr_bars"][-1]["at"] = NOW.replace(second=0).isoformat()
    with pytest.raises(ValueError, match="completed"):
        exit_signal(evidence, NOW)


def test_gateway_bars_cannot_impersonate_schwab_atr():
    evidence = _context(final=10.1)
    evidence["atr_source"] = "ORB_GATEWAY_TRADE_TICKS"
    with pytest.raises(ValueError, match="wrong_atr_source"):
        exit_signal(evidence, NOW)


def test_a_different_body_minute_is_rejected():
    evidence = _context()
    evidence["body"]["at"] = (OPEN - timedelta(minutes=1)).isoformat()
    with pytest.raises(ValueError, match="fill_minute"):
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
    assert len(bars) == 9 and status == "missing_last_closed_schwab_bar"


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
