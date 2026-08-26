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

from datetime import UTC, datetime
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
    bot = SchwabV2BotService(settings=Settings(), session_factory=factory)
    # This file grades DB-seed warmup, not the independent 16:00 boundary. Pin the entry-window
    # precondition so the same fixture means the same thing before and after market close.
    bot.strategy._entry_window_closed_for_session = lambda now=None: False
    return bot


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


# --------------------------------------------------------------------------- (4)
# SEED-CAP AFTER WARMUP — the GMEX bot-wide freeze, 2026-07-27.
#
# The P1.3 cap marks a RECONSTRUCTED armed segment (arm_bar_ts < boot) as USED so a restart cannot
# re-issue the per-segment entry allowance. It ran ONLY at the end of the DB seed — but the REST
# warmup replays a fraction of a second later and RE-ARMS, and those arms were never capped:
#
#     10:33:07,137  [V2-CW-ARM] GMEX bar_ts=1780439040000   <- db-seed replay
#     10:33:07,187  db-seed: GMEX hydrated 250 bars         <- cap ran HERE
#     10:33:07,510  warmup feed for GMEX: 716 bars          <- REST warmup
#     10:33:07,531  [V2-CW-ARM] GMEX bar_ts=1784555700000   <- re-armed, NEVER capped
#
# An uncapped pre-boot segment reads as "dangerous", so the boot-hold suppressed CW-v2 entries
# BOT-WIDE for 54 minutes until a human restarted. One stale symbol froze every symbol.


def _safe_bot(factory) -> SchwabV2BotService:
    return SchwabV2BotService(
        settings=Settings(strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=True),
        session_factory=factory,
    )


def _reconstructed_arm(bot: SchwabV2BotService, symbol: str = "TEST"):
    """Put the symbol in the exact post-replay state: armed off a PRE-BOOT bar, uncapped."""
    st = bot.strategy.watchlist_state(symbol)
    st.cw_armed = True
    st.cw_arm_bar_ts = bot.strategy._boot_ms - 3_600_000  # 1h before boot
    st.cw_entries_this_flip = 0
    return st


def test_cap_helper_marks_reconstructed_segment_used() -> None:
    bot = _safe_bot(_factory_with_bars([]))
    st = _reconstructed_arm(bot)
    assert any(x["dangerous"] for x in bot.strategy.cw_armed_segments())
    bot._cap_reconstructed_segment("TEST", stage="unit")
    assert st.cw_entries_this_flip == bot.strategy._cw_v2_max_entries_per_flip
    assert not any(x["dangerous"] for x in bot.strategy.cw_armed_segments())


def test_warmup_rearm_after_db_seed_is_capped() -> None:
    """THE REGRESSION. Seed runs and caps; the warmup then RE-ARMS; the second cap must fire.

    Without the warmup-site cap the segment stays dangerous and the boot-hold freezes the bot.
    """
    bot = _safe_bot(_factory_with_bars([]))
    # 1. db-seed replay armed a reconstructed segment, and the seed cap ran
    _reconstructed_arm(bot)
    bot._cap_reconstructed_segment("TEST", stage="db-seed")
    assert not any(x["dangerous"] for x in bot.strategy.cw_armed_segments())
    # 2. the REST warmup replays and RE-ARMS off another pre-boot bar (entries reset to 0)
    _reconstructed_arm(bot)
    assert any(x["dangerous"] for x in bot.strategy.cw_armed_segments())  # the freeze state
    # 3. the warmup-site cap must clear it
    bot._cap_reconstructed_segment("TEST", stage="rest-warmup")
    assert not any(x["dangerous"] for x in bot.strategy.cw_armed_segments())


def test_cap_never_touches_a_live_post_boot_arm() -> None:
    """A LIVE flip (arm_bar_ts >= boot) must keep its full entry allowance — capping it would
    silently forfeit real entries, which is the opposite failure."""
    bot = _safe_bot(_factory_with_bars([]))
    st = bot.strategy.watchlist_state("TEST")
    st.cw_armed = True
    st.cw_arm_bar_ts = bot.strategy._boot_ms + 60_000  # live, post-boot
    st.cw_entries_this_flip = 0
    bot._cap_reconstructed_segment("TEST", stage="rest-warmup")
    assert st.cw_entries_this_flip == 0                       # untouched
    assert not any(x["dangerous"] for x in bot.strategy.cw_armed_segments())


def test_cap_is_inert_when_the_safety_flag_is_off() -> None:
    bot = SchwabV2BotService(settings=Settings(), session_factory=_factory_with_bars([]))
    st = _reconstructed_arm(bot)
    bot._cap_reconstructed_segment("TEST", stage="db-seed")
    assert st.cw_entries_this_flip == 0  # flag off -> no mutation
