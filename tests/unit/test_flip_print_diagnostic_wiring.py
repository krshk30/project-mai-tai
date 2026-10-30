"""Requires Main to compose the four reviewed external shared-file proposals.

Controlled real-method seams only; no subscriptions, brokers, DB, or replay.
"""
import asyncio
import json
import logging
from types import MethodType, SimpleNamespace

import pytest

from project_mai_tai.settings import Settings
from project_mai_tai.services import schwab_1m_v2_bot as service
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy
from project_mai_tai.strategy_core.flip_print_diagnostic import FlipPrintDiagnostic

BAR = 1793290800000


def strategy(enabled):
    result = SchwabV2Strategy(Settings(
        _env_file=None,
        strategy_schwab_1m_v2_flip_print_diagnostic_enabled=enabled,
        strategy_schwab_1m_v2_keepwatch_sized_trade_gap_enabled=True,
        strategy_schwab_1m_v2_gap_trade_conditions_filter_enabled=True))
    result._atr_period = 1
    result._atr_factor = 1.0
    return result


def bot(st):
    calls = []
    def eligible(**kwargs):
        calls.append(("eligibility", kwargs))
        return service.SchwabV2BotService._line_print_eligibility(**kwargs)
    instance = SimpleNamespace(strategy=st, settings=st.settings, _keepwatch_enabled=True,
        _line_print_eligibility=eligible,
        _observe_line_trade=lambda *args: calls.append(("line", args)),
        _eh_stream_cross_enabled=False, _gap_hold_enabled=False, tick_writer=None)
    instance._line_sized_trade_policy_enabled = MethodType(
        service.SchwabV2BotService._line_sized_trade_policy_enabled, instance)
    return instance, calls


def tick():
    return SimpleNamespace(symbol="NCPL", kind="trade", service="TIMESALE_EQUITY",
        event_ts_ms=BAR + 1000, price=1.7, size=100,
        raw={"1": BAR + 1000, "conditions": [0]}, raw_hash="a" * 40)


def test_off_does_not_read_receipt_clock_or_observe(monkeypatch):
    st = strategy(False)
    b, calls = bot(st)
    clock_reads = []
    def forbidden():
        clock_reads.append(1)
        raise AssertionError("OFF read the diagnostic clock")
    monkeypatch.setattr(service.time, "time_ns", forbidden)
    asyncio.run(service.SchwabV2BotService._handle_stream_tick(b, tick()))
    assert calls == [("eligibility", {"size": 100, "conditions": [0]}),
                     ("line", ("NCPL", BAR + 1000))]
    assert clock_reads == []
    assert not hasattr(st, "_flip_print_observer")


def test_on_samples_clock_once_and_reuses_single_existing_eligibility_result(monkeypatch):
    st = strategy(True)
    b, calls = bot(st)
    clock = []
    monkeypatch.setattr(service.time, "time_ns", lambda: clock.append(1) or (BAR + 2000) * 1000000)
    asyncio.run(service.SchwabV2BotService._handle_stream_tick(b, tick()))
    assert clock == [1]
    assert len(calls) == 2 and calls[0][0] == "eligibility" and calls[1][0] == "line"
    assert len(st._flip_print_observer._trades["NCPL"]) == 1
    refused, refused_calls = bot(st)
    negative = tick()
    negative.raw["conditions"] = [37]
    asyncio.run(service.SchwabV2BotService._handle_stream_tick(refused, negative))
    assert len(refused_calls) == 1
    assert len(st._flip_print_observer._trades["NCPL"]) == 1


def test_observer_fault_does_not_change_existing_tick_work(monkeypatch):
    st = strategy(True)
    b, calls = bot(st)
    def broken(*args):
        raise RuntimeError("controlled diagnostic fault")
    monkeypatch.setattr(st, "_observe_flip_print_trade", broken)
    asyncio.run(service.SchwabV2BotService._handle_stream_tick(b, tick()))
    assert [name for name, _ in calls] == ["eligibility", "line"]


def test_every_live_buy_and_sell_logged_with_probe_off_and_same_trading_signals(caplog):
    caplog.set_level(logging.INFO)
    on, off = strategy(True), strategy(False)
    on_state, off_state = on.watchlist_state("NCPL"), off.watchlist_state("NCPL")
    # First bar has no prior bar; the second seeds Wilder(1) and initializes
    # long. Only subsequent crossings can produce actual SELL/BUY transitions.
    bars = [OHLCVBar(BAR, 2.0, 2.1, 1.9, 2.0, 100),
            OHLCVBar(BAR + 60000, 2.0, 2.1, 1.9, 2.0, 100),
            OHLCVBar(BAR + 120000, 1.7, 1.8, 1.4, 1.5, 200),
            OHLCVBar(BAR + 180000, 2.4, 2.6, 2.3, 2.5, 300)]
    signals = []
    for bar in bars:
        lhs = on._update_atr_state(on_state, bar, observation_phase="live")
        rhs = off._update_atr_state(off_state, bar, observation_phase="live")
        assert lhs == rhs
        signals.append(lhs["flip"] if lhs is not None else None)
    assert signals == [None, None, "SELL", "BUY"]
    messages = [r.getMessage() for r in caplog.records if "[V2-FLIP-PRINT-DIAGNOSTIC]" in r.getMessage()]
    assert len(messages) == 2
    assert [json.loads(m.split(" ", 1)[1])["flip"] for m in messages] == ["SELL", "BUY"]
    assert all(json.loads(m.split(" ", 1)[1])["observed_last_trade"] == "UNKNOWN" for m in messages)


def test_logger_failure_cannot_change_literal_sell_transition(monkeypatch):
    st = strategy(True)
    state = st.watchlist_state("NCPL")
    st._update_atr_state(state, OHLCVBar(BAR, 2.0, 2.1, 1.9, 2.0, 100), observation_phase="live")
    st._update_atr_state(state, OHLCVBar(BAR + 60000, 2.0, 2.1, 1.9, 2.0, 100), observation_phase="live")
    def broken(*args, **kwargs):
        raise RuntimeError("controlled observer formatter failure")
    monkeypatch.setattr(FlipPrintDiagnostic, "line", broken)
    signal = st._update_atr_state(state, OHLCVBar(BAR + 120000, 1.7, 1.8, 1.4, 1.5, 200), observation_phase="live")
    assert signal["flip"] == "SELL"
    assert state.atr_state == "short"


def test_existing_callback_selection_off_and_on_identical_no_new_subscription():
    # Only existing capture/gap/EH guards select a callback. Diagnostic ON alone
    # must not add a subscription or route capture-only ticks through strategy.
    callback = object()
    for enabled in (False, True):
        b = SimpleNamespace(strategy=strategy(enabled), _gap_hold_enabled=False,
            _eh_stream_cross_enabled=False, settings=SimpleNamespace(
                strategy_schwab_1m_v2_tick_capture_enabled=False),
            tick_writer=SimpleNamespace(on_tick=callback))
        assert service.SchwabV2BotService._stream_tick_callback(b) is callback
        b.tick_writer = None
        assert service.SchwabV2BotService._stream_tick_callback(b) is None
