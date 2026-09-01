from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.backtest.atr_bracket_study import (
    BracketPolicy,
    _consume_stale_arm,
    run_symbol_policy,
)
from project_mai_tai.backtest.data import Quote, SchwabBar
from project_mai_tai.backtest.replay import ReplayStrategy, build_replay_settings
from project_mai_tai.backtest.watch_start import WatchWindow

ET = ZoneInfo("America/New_York")
BASE = datetime(2026, 7, 23, 10, 0, tzinfo=ET)
SYMBOL = "TEST"

# Continuous bars that drive the live strategy through a short segment, a resting placement, and
# the BUY flip.  This is the same hand-verified shape used by the replay golden.
OHLC = [
    (100.0, 100.2, 99.8, 100.0),
    (100.0, 100.2, 99.8, 100.0),
    (100.0, 100.2, 99.8, 100.0),
    (100.0, 100.2, 99.8, 100.0),
    (100.0, 100.2, 99.8, 100.0),
    (100.0, 100.2, 99.8, 100.0),
    (100.0, 100.2, 99.8, 100.0),
    (100.0, 100.2, 99.8, 100.0),
    (100.0, 100.2, 99.8, 100.0),
    (99.8, 99.9, 97.9, 98.0),
    (97.8, 97.9, 97.5, 97.6),
    (97.4, 97.5, 97.1, 97.2),
    (97.1, 97.2, 96.8, 96.9),
    (96.9, 97.0, 96.6, 96.7),
    (96.8, 96.9, 96.5, 96.6),
    (96.7, 96.8, 96.4, 96.5),
    (96.7, 99.5, 96.6, 99.3),
]


def _bars() -> list[SchwabBar]:
    return [
        SchwabBar(
            ts=int((BASE + timedelta(minutes=index)).timestamp() * 1000),
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=50_000,
        )
        for index, (open_, high, low, close) in enumerate(OHLC)
    ]


def _entry_quotes() -> list[Quote]:
    quotes = []
    for index in range(9, 16):
        close = OHLC[index][3]
        quotes.append(
            Quote(
                ts=BASE + timedelta(minutes=index, seconds=30),
                bid=close - 0.15,
                ask=close + 0.05,
                last=close,
            )
        )
    quotes.append(
        Quote(
            ts=BASE + timedelta(minutes=16, seconds=30),
            bid=98.4,
            ask=98.5,
            last=98.5,
        )
    )
    return quotes


class Source:
    def __init__(
        self,
        quotes: list[Quote],
        *,
        bars: list[SchwabBar] | None = None,
        window_end: datetime | None = None,
    ):
        self.bars = bars or _bars()
        self.quotes = quotes
        start_ms = int(BASE.timestamp() * 1000)
        end_ms = int(window_end.timestamp() * 1000) if window_end else None
        self.windows = [WatchWindow(start_ms, end_ms)]

    def schwab_bars(self, symbol, start, end):
        lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        return [bar for bar in self.bars if lo <= bar.ts < hi]

    def schwab_quotes(self, symbol, start, end):
        return [quote for quote in self.quotes if start <= quote.ts < end]

    def watch_windows(self, symbol, trade_date, *, realtime_confirms_only=False):
        return self.windows


def _settings():
    return build_replay_settings(
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=False,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=False,
    )


def test_positive_floor_is_rejected() -> None:
    with pytest.raises(ValueError, match="zero or negative"):
        BracketPolicy(target_pct=1.0, floor_pct=1.0)


def test_target_uses_executable_ask_entry_and_stated_limit_exit() -> None:
    quotes = _entry_quotes()
    quotes.append(
        Quote(
            ts=BASE + timedelta(minutes=16, seconds=31),
            bid=99.7,
            ask=99.8,
            last=99.75,
        )
    )
    result = run_symbol_policy(
        Source(quotes), SYMBOL, "2026-07-23", _settings(), BracketPolicy(1.0, -2.0)
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_slot == "first"
    assert trade.entry_mode == "resting"
    assert trade.entry_px == pytest.approx(98.5)
    assert trade.exit_reason == "target"
    assert trade.exit_px == pytest.approx(98.5 * 1.01)
    assert trade.ret_pct == pytest.approx(1.0)


def test_zero_floor_triggers_on_first_later_bid_and_fills_on_next_bid() -> None:
    quotes = _entry_quotes()
    trigger_ts = BASE + timedelta(minutes=16, seconds=31)
    fill_ts = BASE + timedelta(minutes=16, seconds=32)
    quotes.extend(
        [
            Quote(ts=trigger_ts, bid=98.4, ask=98.6, last=98.5),
            Quote(ts=fill_ts, bid=98.3, ask=98.5, last=98.4),
        ]
    )
    result = run_symbol_policy(
        Source(quotes), SYMBOL, "2026-07-23", _settings(), BracketPolicy(2.0, 0.0)
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "floor"
    assert trade.exit_trigger_ts == trigger_ts
    assert trade.exit_ts == fill_ts
    assert trade.exit_px == pytest.approx(98.3)
    assert trade.ret_pct < 0


def test_scanner_removal_cancels_entry_eligibility() -> None:
    quotes = _entry_quotes()
    source = Source(quotes, window_end=BASE + timedelta(minutes=16))
    result = run_symbol_policy(
        source, SYMBOL, "2026-07-23", _settings(), BracketPolicy(1.0, -2.0)
    )

    assert result.trades == []


def test_modelled_close_runs_live_transition_and_allows_reclaim_round_trip() -> None:
    bars = _bars()
    for index, (open_, high, low, close) in enumerate(
        [
            (99.3, 99.6, 99.1, 99.5),
            (99.5, 99.7, 99.4, 99.6),
            (99.6, 99.8, 99.5, 99.7),
        ],
        start=17,
    ):
        bars.append(
            SchwabBar(
                ts=int((BASE + timedelta(minutes=index)).timestamp() * 1000),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=50_000,
            )
        )
    quotes = _entry_quotes()
    quotes.extend(
        [
            # First bracket closes before the BUY-flip bar closes; the bar-close then confirms
            # the segment and the live one-bar reclaim gap starts from the modelled close.
            Quote(
                ts=BASE + timedelta(minutes=16, seconds=31),
                bid=99.7,
                ask=99.8,
                last=99.75,
            ),
            Quote(
                ts=BASE + timedelta(minutes=17, seconds=30),
                bid=99.45,
                ask=99.55,
                last=99.5,
            ),
            Quote(
                ts=BASE + timedelta(minutes=18, seconds=30),
                bid=99.55,
                ask=99.65,
                last=99.6,
            ),
            Quote(
                ts=BASE + timedelta(minutes=19, seconds=30),
                bid=99.65,
                ask=99.75,
                last=99.7,
            ),
            Quote(
                ts=BASE + timedelta(minutes=19, seconds=31),
                bid=100.8,
                ask=100.9,
                last=100.85,
            ),
        ]
    )
    result = run_symbol_policy(
        Source(quotes, bars=bars),
        SYMBOL,
        "2026-07-23",
        _settings(),
        BracketPolicy(1.0, -2.0),
    )

    assert [trade.entry_slot for trade in result.trades] == ["first", "reclaim"]
    assert [trade.exit_reason for trade in result.trades] == ["target", "target"]


def test_watch_start_cap_consumes_the_live_composition_flags() -> None:
    strategy = ReplayStrategy(_settings())
    state = strategy.watchlist_state(SYMBOL)
    state.cw_armed = True
    state.cw_arm_bar_ts = 1_000
    state.cw_entries_this_flip = 0
    state.cw_resting_taken = False
    state.cw_reclaim_taken = False

    assert _consume_stale_arm(
        strategy,
        SYMBOL,
        watch_start_ms=2_000,
        session_anchor_ms=500,
    )
    assert state.cw_entries_this_flip == strategy._cw_v2_max_entries_per_flip
    assert state.cw_resting_taken is True
    assert state.cw_reclaim_taken is True
