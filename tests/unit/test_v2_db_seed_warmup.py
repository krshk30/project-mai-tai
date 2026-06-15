"""Fix (b): cold-start DB-seed of the strategy bar buffer.

`SchwabV2BotService._seed_strategy_bars_from_db` hydrates `state.bars` from
`strategy_bar_history` so MACD/VWAP/ATR clear their ~135-bar warmup at once
instead of being blind for ~135 minutes after a restart. These pin: (1) warm
immediately, (2) the load-bearing pending-cross CLEAR (a native cross on the
last seed bar must NOT fire a phantom entry on the first live bar), (3) bounded
+ idempotent + graceful under the deque(maxlen=300).

The DB uses an in-memory SQLite with ONLY the StrategyBarHistory table (it uses
JSON, not JSONB, so it renders — unlike the market_trade_ticks tables).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import StrategyBarHistory
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.services.schwab_1m_v2_bot import (
    DB_SEED_BAR_LIMIT,
    INTERVAL_SECS,
    SchwabV2BotService,
)
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import STRATEGY_CODE

MIN_BARS = 135


def _factory_with_bars(rows):
    """In-memory SQLite with ONLY the StrategyBarHistory table, seeded with
    `rows` = list of (bar_time_dt, open, high, low, close, volume)."""
    engine = create_engine("sqlite://")
    StrategyBarHistory.__table__.create(engine)
    Session = sessionmaker(engine)
    with Session() as s:
        for (bt, o, h, low, c, v) in rows:
            s.add(StrategyBarHistory(
                strategy_code=STRATEGY_CODE, symbol="TEST",
                interval_secs=INTERVAL_SECS, bar_time=bt,
                open_price=Decimal(str(o)), high_price=Decimal(str(h)),
                low_price=Decimal(str(low)), close_price=Decimal(str(c)),
                volume=int(v),
            ))
        s.commit()
    return Session


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _bot(factory) -> SchwabV2BotService:
    return SchwabV2BotService(settings=Settings(), session_factory=factory)


# --------------------------------------------------------------------------- (1)

def test_db_seed_clears_min_bars_warmup() -> None:
    """200 persisted bars seeded → state.bars is warm (>= MIN_BARS), so a FRESH
    MACD cross emits IMMEDIATELY instead of waiting ~135 live bars."""
    now = int(datetime.now(UTC).timestamp() * 1000)
    # Recent contiguous flat bars (as persisted up to a restart), ending ~2 min
    # ago; then a FRESH green bar now. Mirrors the proven cross fixture in
    # test_schwab_1m_v2_atr_flip.test_atr_on_does_not_perturb_paths_1_2.
    rows = [
        (_dt(now - (201 - i) * 60_000), 10.0, 10.0, 10.0, 10.0, 1000)
        for i in range(200)
    ]
    bot = _bot(_factory_with_bars(rows))
    bot._seed_strategy_bars_from_db("TEST")

    st = bot.strategy.watchlist_state("TEST")
    assert len(st.bars) >= MIN_BARS                      # warm — blackout gone
    fresh = ChartBar("TEST", 10.0, 11.0, 10.0, 11.0, 100_000, now)
    draft = bot.strategy.on_bar("TEST", fresh)
    assert draft is not None and draft.metadata["path"] == "MACD Cross"


# --------------------------------------------------------------------------- (2)

def test_db_seed_clears_pending_cross_no_phantom_entry() -> None:
    """THE load-bearing safety: a native MACD cross on the LAST (stale) seed bar
    must NOT be consumed by the first live bar — the seed clears the pending-cross
    stash. Without the clear, the live bar (within the 180s gap) would fire a
    phantom 'MACD Cross' from replayed history."""
    now = int(datetime.now(UTC).timestamp() * 1000)
    rows = [
        # 199 flat bars, all older than the cross bar, ascending.
        (_dt(now - (200_000 + (199 - i) * 60_000)), 10.0, 10.0, 10.0, 10.0, 1000)
        for i in range(199)
    ]
    # Last seed bar: a green native cross, STALE (age 200s > 180s freshness).
    rows.append((_dt(now - 200_000), 10.0, 11.0, 10.0, 11.0, 100_000))

    bot = _bot(_factory_with_bars(rows))
    bot._seed_strategy_bars_from_db("TEST")

    st = bot.strategy.watchlist_state("TEST")
    # Direct: the pending-cross stash was cleared (memos kept).
    assert st.pending_path_macd is False
    assert st.pending_path_vwap is False
    assert st.pending_cross_bar_ts_ms == 0
    assert st.prev_macd is not None                      # memos warm (the point)

    # Behavioral: a FRESH bar 170s after the stale cross (within the 180s pending
    # gap) must NOT emit — pending was cleared and this bar is no new native cross.
    live = ChartBar("TEST", 11.0, 11.0, 11.0, 11.0, 100_000, now - 30_000)
    assert bot.strategy.on_bar("TEST", live) is None


# --------------------------------------------------------------------------- (3)

def test_db_seed_bounded_idempotent_and_deque_graceful() -> None:
    """Loads at most DB_SEED_BAR_LIMIT; re-seed is a no-op; seed + live bars sit
    gracefully under the deque(maxlen=300)."""
    now = int(datetime.now(UTC).timestamp() * 1000)
    # 300 recent contiguous bars, newest ~2 min ago. The seed loads the newest
    # DB_SEED_BAR_LIMIT; live bars then arrive newer still.
    rows = [
        (_dt(now - (302 - i) * 60_000), 10.0, 10.0, 10.0, 10.0, 1000)
        for i in range(300)                              # > DB_SEED_BAR_LIMIT (250)
    ]
    bot = _bot(_factory_with_bars(rows))
    bot._seed_strategy_bars_from_db("TEST")
    st = bot.strategy.watchlist_state("TEST")
    assert len(st.bars) == DB_SEED_BAR_LIMIT == 250      # bounded (newest 250 of 300)

    bot._seed_strategy_bars_from_db("TEST")              # idempotent
    assert len(st.bars) == 250

    # 60 newer live bars (now, now+1m, ...) → deque caps at 300, no thrash/error.
    for k in range(60):
        ts = now + k * 60_000
        bot.strategy.on_bar("TEST", ChartBar("TEST", 10.0, 10.0, 10.0, 10.0, 1000, ts))
    assert len(st.bars) == 300


# --------------------------------------------------------------------------- (4)

def test_db_seed_no_rows_is_safe() -> None:
    """A symbol with no persisted history seeds nothing and doesn't raise (it
    simply warms live-only, as before)."""
    bot = _bot(_factory_with_bars([]))
    bot._seed_strategy_bars_from_db("NOPE")
    assert len(bot.strategy.watchlist_state("NOPE").bars) == 0
