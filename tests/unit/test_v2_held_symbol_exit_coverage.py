"""Held-symbol exit coverage — a position we HOLD keeps its market data after the
scanner drops the symbol, so its exits keep working.

THE HAZARD (design doc: docs/design/held-symbol-exit-coverage.md)
    `_sync_gateway_subscription` publishes `mode="replace"`, so a symbol absent from the
    published list is UNSUBSCRIBED. The OMS exit ladder is NOT watchlist-gated — `_watchlist`
    appears 0 times in oms/service.py — but it IS quote-driven: `_handle_quote_tick_event` is
    the only caller of `_evaluate_v2_managed_exit`. So dropping a held symbol does not merely
    stop watching it, it silently disarms CW_TARGET / CW_FLOOR / CW_HARD_STOP / CW_FLIP at once.

FIVE PROPERTIES, each with its OWN mutation (see each test's `MUTATION:` note). Fixing the one
we noticed and leaving the rest is how CW_FLIP sat broken.

⛔ FIXTURE MATCHES PRODUCTION: `oms_v2_exit_management_enabled` defaults False, and a fixture
that leaves it False passes by never running the code. Every harness here sets it True.
"""
from __future__ import annotations

import pytest

from project_mai_tai.events import MarketDataSubscriptionEvent
from project_mai_tai.exit_logic.cw_exit import cw_exit_decision
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings

HELD = "FRTT"          # held, then dropped by the scanner
STILL_WATCHED = "WXM"  # ordinary watchlist member, control


class _FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def xadd(self, stream, fields, **kw):
        self.calls.append((stream, fields, kw))
        return b"1-1"


class _Sink:
    """Stands in for the REST client / streamer — records the last desired set."""

    def __init__(self) -> None:
        self.desired: set[str] | None = None

    def set_desired_symbols(self, symbols) -> None:
        self.desired = set(symbols)


class _Harness:
    """Duck-typed stand-in; the methods under test use only these attributes."""

    # the real helper, so the union logic under test is the shipped one
    _subscription_symbols = SchwabV2BotService._subscription_symbols

    def __init__(self, *, watchlist: set[str], coverage: set[str]) -> None:
        self.settings = Settings(oms_v2_exit_management_enabled=True)
        self.redis = _FakeRedis()
        self._watchlist = set(watchlist)
        self._exit_coverage = set(coverage)
        self._last_gateway_symbols = None
        self.rest_client = _Sink()
        self.streamer = _Sink()


def _subscription(h: _Harness) -> set[str]:
    return SchwabV2BotService._subscription_symbols(h)


async def _sync(h: _Harness) -> None:
    await SchwabV2BotService._sync_gateway_subscription(h)


def _published(h: _Harness) -> list[str]:
    ev = MarketDataSubscriptionEvent.model_validate_json(h.redis.calls[-1][1]["data"])
    return list(ev.payload.symbols)


# --------------------------------------------------------------------- 1, 2, 3
# The quote feed is the shared input for CW_TARGET, CW_FLOOR and CW_HARD_STOP.
# MUTATION for all three: revert `_sync_gateway_subscription` to
#   desired = sorted(self._watchlist)
# -> HELD disappears from the published list -> quotes stop -> all three go dark.

@pytest.mark.asyncio
async def test_quote_feed_survives_delisting_for_a_held_symbol() -> None:
    h = _Harness(watchlist={STILL_WATCHED}, coverage={HELD})   # HELD already dropped
    await _sync(h)
    published = _published(h)
    assert HELD in published, (
        "a held symbol dropped from the watchlist lost its quote subscription — "
        "CW_TARGET, CW_FLOOR and CW_HARD_STOP all go dark together"
    )
    assert published == sorted({HELD, STILL_WATCHED})


@pytest.mark.parametrize(
    ("bid", "armed", "expected"),
    [
        (1.53, False, "arm"),     # +2% reached -> lock the floor and ride
        (1.62, True, "floor"),    # armed, bid falls back to the floor -> exit
        (1.40, False, "stop"),    # -5% -> hard stop
    ],
    ids=["target_arms_floor", "floor_exits", "hard_stop_exits"],
)
def test_each_exit_rule_is_reachable_for_a_delisted_held_symbol(bid, armed, expected) -> None:
    """The rules themselves, driven by the bid that only arrives because of test 1.

    Entry 1.50, target +2%, floor +8.5%, stop -5% — the FRTT 2026-08-11 shape.
    """
    action, _ = cw_exit_decision(
        1.50, bid, armed,
        target_pct=2.0, stop_pct=5.0, floor_pct=8.5,
        floor_enabled=True, flip_pending=False,
    )
    assert action == expected


@pytest.mark.asyncio
async def test_bar_feed_survives_delisting_so_the_flip_exit_can_arm() -> None:
    """CW_FLIP is armed off a BAR close, so the streamer/REST feed must survive too.

    MUTATION: revert `_push_desired_symbols` to push `self._watchlist` -> HELD is unsubscribed
    from CHART_EQUITY and no bar can ever arm its flip exit.
    """
    h = _Harness(watchlist={STILL_WATCHED}, coverage={HELD})
    SchwabV2BotService._push_desired_symbols(h)
    assert h.streamer.desired == {HELD, STILL_WATCHED}, "held symbol lost its BAR feed"
    assert h.rest_client.desired == {HELD, STILL_WATCHED}

    # and the flip rule itself fires once a bar-close flip is pending
    action, _ = cw_exit_decision(
        1.50, 1.55, False,
        target_pct=99.0, stop_pct=99.0, floor_pct=8.5,
        floor_enabled=True, flip_pending=True,
    )
    assert action == "flip"


# --------------------------------------------------------------------------- 4

def test_resting_cancel_reaches_a_delisted_symbol() -> None:
    """The 16:00 resting-cancel must reach a working order on a symbol the scanner dropped.

    BEHAVIOURAL: the strategy holds a resting order for HELD, the entry window closes, and the
    cancel must be queued. The strategy has no watchlist at all — it drives off
    `self._symbol_states` — so a de-listed symbol stays reachable by construction. This test
    pins that property so it cannot regress into watchlist-gating.

    MUTATION: gate `_cw_v2_resting_track`'s window-closed branch on watchlist membership
    (or make the enclosing loop iterate a watchlist) -> the cancel is never queued -> RED.
    """
    from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

    strat = SchwabV2Strategy(
        Settings(
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
            strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        )
    )
    st = strat.watchlist_state(HELD)

    # 1) arm a resting order INSIDE the window
    strat._resting_in_window = lambda now=None: True
    strat._now_ms = lambda: 1_000_000
    st.bars.append(
        OHLCVBar(timestamp_ms=1_000_000, open=10.5, high=10.7, low=9.3, close=10.4, volume=25_000)
    )
    strat._cw_v2_resting_track(
        st,
        {"touch": False, "touch_price": None, "flip": None, "flip_level": None,
         "trail": 9.5, "loss": 0.5, "state": "short", "state_age": 3},
    )
    strat.drain_pending_intents()
    assert st.resting_active is True, "precondition: a resting order is working"

    # 2) the window closes while the symbol is HELD and long since de-listed
    strat._resting_in_window = lambda now=None: False
    strat._cw_v2_resting_track(
        st,
        {"touch": False, "touch_price": None, "flip": None, "flip_level": None,
         "trail": 9.5, "loss": 0.5, "state": "short", "state_age": 3},
    )
    out = strat.drain_pending_intents()

    assert st.resting_active is False
    assert [d for d in out if d.intent_type == "cancel"], (
        "the 16:00 resting-cancel did not reach a de-listed symbol's working order — "
        "it would be left live at the broker overnight"
    )


# --------------------------------------------------------------------------- 5

@pytest.mark.asyncio
async def test_held_delisted_symbol_can_never_enter() -> None:
    """⛔ EXIT-ONLY. Coverage closes positions; it must never open one.

    A held, de-listed symbol now receives bars and quotes, so the strategy WILL evaluate it.
    The chokepoint must drop its "open" intent.

    MUTATION: remove the `[V2-ENTRY-OFF-WATCHLIST-BLOCK]` guard in `_maybe_emit` -> RED.
    """

    class _Draft:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol
            self.intent_type = "open"
            self.reason = "schwab_1m_v2 ATR Flip CW-v2-resting"
            self.metadata: dict[str, str] = {}

    class _Emitter:
        def __init__(self) -> None:
            self.emitted: list[str] = []

        async def emit(self, draft):  # pragma: no cover - must never run for HELD
            self.emitted.append(draft.symbol)

    class _EmitHarness:
        def __init__(self) -> None:
            self.settings = Settings(oms_v2_exit_management_enabled=True)
            self._watchlist = {STILL_WATCHED}        # HELD deliberately absent
            self._exit_coverage = {HELD}
            self.intent_emitter = _Emitter()
            self.webull_intent_emitter = None

    h = _EmitHarness()
    await SchwabV2BotService._maybe_emit(h, _Draft(HELD))
    assert h.intent_emitter.emitted == [], (
        "held-symbol coverage opened a position on a symbol the scanner had dropped — "
        "coverage is EXIT-ONLY (design §2)"
    )
