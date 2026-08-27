"""Dual-broker FAN-OUT (docs/per-broker-eligibility-webull-fallback-design.md).

When the fan-out flag is ON the v2 bot emits a SECOND (Webull) MARKET-at-cross buy-open leg IN
PARALLEL with the Schwab leg at every up-cross, gated by per-broker eligibility. Flag-OFF is
byte-identical to the mirror-on-fill era (no second leg, nothing queued).

Coverage:
- strategy: the three cross moments queue a Webull leg (reactive MARKET / RTH-resting MARKET /
  EH-resting LIMIT); once-per-flip; flat + claim guards; claim resets on close/flip; flag-off inert.
- OMS: the not-tradable classifier vetoes 429/transient; _v2_accounts adds Webull under fan-out;
  the webull_ineligible store round-trips.
- bot: _emit_webull_fanout_legs routes to the Webull emitter, skips ineligible names, and drains
  even when the emitter is unset; eviction intersects the two ineligible sets under fan-out.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import Base, BrokerAccount
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

_ET = ZoneInfo("America/New_York")
RTH_MS = int(datetime(2026, 7, 10, 11, 0, tzinfo=_ET).timestamp() * 1000)   # 11:00 ET (RTH, non-ORB)
EH_MS = int(datetime(2026, 7, 10, 8, 0, tzinfo=_ET).timestamp() * 1000)     # 08:00 ET (pre-market)
SHARED_IDENTITY_KEYS = ("fanout_segment_id", "fanout_slot", "fanout_slot_id")


# ============================================================ strategy-side helpers
def _strat(*, fanout=True, **overrides) -> SchwabV2Strategy:
    kwargs = {
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_dual_broker_fanout_enabled": fanout,
    }
    kwargs.update(overrides)
    return SchwabV2Strategy(Settings(**kwargs))


def _bar(high: float, *, ts: int = 0) -> OHLCVBar:
    return OHLCVBar(timestamp_ms=ts, open=high - 0.1, high=high, low=high - 0.2,
                    close=high - 0.05, volume=25_000)


def _sig(flip=None, *, flip_level=None, trail=9.5, state="long", age=1) -> dict:
    return {"touch": False, "touch_price": None, "flip": flip, "flip_level": flip_level,
            "trail": trail, "loss": 0.5, "state": state, "state_age": age}


def _quote(px: float, *, ts: int = RTH_MS) -> Quote:
    return Quote("TEST", px - 0.01, px + 0.01, px, ts, 0)


def _arm_to_watch(strat, state):
    """BUY flip (high 12.0, flip_level 9.5) + 2 bars -> trigger=12.0, armed, bars_waited=2."""
    for h, t in ((12.0, 1), (10.0, 2), (11.0, 3)):
        state.bars.append(_bar(h, ts=t))
        strat._cw_v2_track(state, _sig(flip="BUY", flip_level=9.5) if t == 1 else _sig())
    strat._cw_v2_track(state, _sig())   # bar+3: watch phase


# ============================================================ REACTIVE cross
def test_reactive_cross_queues_webull_market_leg():
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _arm_to_watch(strat, st)
    primary = strat._cw_v2_quote(st, _quote(12.5))       # break 12.0, whole bar above 9.5
    assert primary is not None and primary.metadata["atr_variant"] == "CW-v2"
    legs = strat.drain_webull_fanout_intents()
    assert len(legs) == 1
    leg = legs[0]
    assert leg.side == "buy" and leg.intent_type == "open"
    assert leg.metadata["fanout_leg"] == "webull"
    assert leg.metadata["order_type"] == "market"        # RTH -> MARKET+OCO in the OMS
    assert leg.metadata["entry_price"] == "12.5000"      # OCO anchors off the cross px
    assert leg.metadata["fanout_source"] == "reactive"
    assert "ATR Flip" in leg.reason                       # keeps the ATR-only belt
    assert all(primary.metadata[key] == leg.metadata[key] for key in SHARED_IDENTITY_KEYS)


def test_reactive_cross_no_webull_leg_when_fanout_off():
    strat = _strat(fanout=False)
    st = strat.watchlist_state("TEST")
    _arm_to_watch(strat, st)
    primary = strat._cw_v2_quote(st, _quote(12.5))
    assert primary is not None                            # the Schwab leg is byte-identical
    assert all(key not in primary.metadata for key in SHARED_IDENTITY_KEYS)
    assert strat.drain_webull_fanout_intents() == []      # nothing queued -> no second leg


def test_buy_arm_mints_the_shared_segment_before_either_reactive_leg_emits():
    strat = _strat()
    st = strat.watchlist_state("TEST")
    st.bars.append(_bar(12.0, ts=RTH_MS))

    strat._cw_v2_track(st, _sig(flip="BUY", flip_level=9.5))

    assert st.cw_armed is True
    assert st.fanout_segment_id == RTH_MS
    assert strat.drain_webull_fanout_intents() == []


def test_buy_arm_preserves_an_identity_bound_by_the_earlier_resting_arm():
    strat = _strat(strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True)
    strat._resting_session_is_eh = lambda now=None: False
    strat._now_ms = lambda: RTH_MS - 60_000
    st = strat.watchlist_state("TEST")
    st.bars.append(_bar(11.0, ts=RTH_MS - 60_000))
    strat._queue_resting_place(st, 9.5, slot="first")
    resting_identity = st.fanout_segment_id

    st.bars.append(_bar(12.0, ts=RTH_MS))
    strat._cw_v2_track(st, _sig(flip="BUY", flip_level=9.5))

    assert resting_identity == RTH_MS - 60_000
    assert st.fanout_segment_id == resting_identity


def test_buy_arm_does_not_mint_fanout_identity_when_the_feature_is_off():
    strat = _strat(fanout=False)
    st = strat.watchlist_state("TEST")
    st.bars.append(_bar(12.0, ts=RTH_MS))

    strat._cw_v2_track(st, _sig(flip="BUY", flip_level=9.5))

    assert st.cw_armed is True
    assert st.fanout_segment_id == 0


def test_reactive_leg_fires_once_per_flip():
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _arm_to_watch(strat, st)
    assert strat._cw_v2_quote(st, _quote(12.5)) is not None
    assert len(strat.drain_webull_fanout_intents()) == 1
    # A second quote same flip: the reactive claim (cw_v2_emit_claimed) blocks a re-fire.
    assert strat._cw_v2_quote(st, _quote(12.6)) is None
    assert strat.drain_webull_fanout_intents() == []


# ============================================================ RTH RESTING cross (the new detector)
def _resting_state(strat, *, level=9.5, now_ms=RTH_MS):
    st = strat.watchlist_state("TEST")
    st.resting_active = True
    st.resting_level = level
    st.bars.append(_bar(level + 1, ts=now_ms))            # fresh live bar
    strat._resting_session_is_eh = lambda now=None: False  # RTH
    strat._now_ms = lambda: now_ms
    return st


def test_rth_resting_cross_queues_webull_market_leg():
    strat = _strat(strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True)
    st = _resting_state(strat, level=9.5)
    strat._fanout_rth_resting_cross(st, _quote(9.51))     # print reaches the resting level
    legs = strat.drain_webull_fanout_intents()
    assert len(legs) == 1 and legs[0].metadata["order_type"] == "market"
    assert legs[0].metadata["fanout_source"] == "rth_resting"
    assert legs[0].metadata["entry_price"] == "9.5100"
    assert st.fanout_webull_claimed is True
    # Second cross same flip -> claimed -> no re-queue.
    strat._fanout_rth_resting_cross(st, _quote(9.55))
    assert strat.drain_webull_fanout_intents() == []


def test_rth_resting_primary_and_cross_fired_webull_leg_share_identity():
    strat = _strat(strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True)
    strat._resting_session_is_eh = lambda now=None: False
    strat._now_ms = lambda: RTH_MS
    st = strat.watchlist_state("TEST")
    st.bars.append(_bar(10.0, ts=RTH_MS))

    strat._queue_resting_place(st, 9.5, slot="first")
    primary = strat.drain_pending_intents()[0]
    strat._fanout_rth_resting_cross(st, _quote(9.51))
    webull = strat.drain_webull_fanout_intents()[0]

    assert all(primary.metadata[key] == webull.metadata[key] for key in SHARED_IDENTITY_KEYS)


def test_rth_resting_no_leg_below_level_or_when_held():
    strat = _strat(strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True)
    st = _resting_state(strat, level=9.5)
    strat._fanout_rth_resting_cross(st, _quote(9.40))     # below the level
    assert strat.drain_webull_fanout_intents() == []
    assert st.fanout_webull_claimed is False
    st.position_qty = 10                                  # already holding
    strat._fanout_rth_resting_cross(st, _quote(9.60))
    assert strat.drain_webull_fanout_intents() == []


def test_rth_resting_off_when_fanout_off():
    strat = _strat(fanout=False, strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True)
    st = _resting_state(strat, level=9.5)
    strat._fanout_rth_resting_cross(st, _quote(9.60))
    assert strat.drain_webull_fanout_intents() == []


# ============================================================ EH RESTING cross
def test_eh_resting_cross_queues_webull_limit_leg():
    strat = _strat(
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=True,
    )
    st = strat.watchlist_state("TEST")
    st.resting_active = True
    st.resting_level = 9.5
    st.bars.append(_bar(9.5, ts=EH_MS))
    strat._resting_session_is_eh = lambda now=None: True   # EH
    strat._now_ms = lambda: EH_MS
    primary = strat._eh_resting_cross_check(st, _quote(9.51, ts=EH_MS))
    assert primary is not None and primary.metadata["eh_resting"] == "true"
    legs = strat.drain_webull_fanout_intents()
    assert len(legs) == 1
    assert legs[0].metadata["order_type"] == "limit"       # EH -> marketable EH-LIMIT (no OCO)
    assert legs[0].metadata["fanout_source"] == "eh_resting"
    assert all(primary.metadata[key] == legs[0].metadata[key] for key in SHARED_IDENTITY_KEYS)


# ============================================================ claim resets + qty
def test_fanout_claim_resets_on_position_close():
    strat = _strat(strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True)
    st = _resting_state(strat, level=9.5)
    strat._fanout_rth_resting_cross(st, _quote(9.51))
    assert st.fanout_webull_claimed is True
    strat.drain_webull_fanout_intents()
    # Position opens then the OMS closes it -> the reclaim path releases the claim.
    strat.update_position("TEST", 10)
    strat.update_position("TEST", 0)
    assert st.fanout_webull_claimed is False


def test_webull_fanout_qty_defaults_to_schwab_qty_and_is_overridable():
    default_strat = _strat()
    assert default_strat._webull_fanout_qty == int(default_strat._atr_qty)
    override = _strat(strategy_schwab_1m_v2_webull_fanout_quantity=3)
    assert override._webull_fanout_qty == 3


# ============================================================ OMS: not-tradable classifier
@pytest.mark.parametrize("reason", [
    "Webull order rejected: NO_SUCH_TICKER symbol not found (http 400)",
    "Webull order rejected: the instrument is not tradable (http 400)",
    "INVALID_SYMBOL",
])
def test_webull_ineligible_reason_matches_not_tradable(reason):
    assert OmsRiskService._is_webull_ineligible_reason(reason) is True


@pytest.mark.parametrize("reason", [
    "Webull order rejected: TOO_MANY_REQUESTS (http 429)",       # the 429 flood MUST NOT mark ineligible
    "Webull order rejected: rate_limit exceeded (http 429)",
    "Webull order rejected: missing Webull App Key/App Secret",  # config, not ineligibility
    "Webull order rejected: request timed out",                  # transient
    "",
    None,
])
def test_webull_ineligible_reason_vetoes_transient_and_config(reason):
    assert OmsRiskService._is_webull_ineligible_reason(reason) is False


def test_429_veto_wins_even_with_a_not_tradable_substring():
    # A reason that contains BOTH a not-tradable phrase AND a 429 marker must be vetoed (429 wins).
    reason = "Webull order rejected: not tradable — TOO_MANY_REQUESTS (http 429)"
    assert OmsRiskService._is_webull_ineligible_reason(reason) is False


# ============================================================ OMS: _v2_accounts under fan-out
def _svc_settings(**over):
    base = dict(
        strategy_schwab_1m_v2_account_name="paper:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    base.update(over)
    return Settings(**base)


def test_v2_accounts_single_when_both_flags_off():
    svc = OmsRiskService.__new__(OmsRiskService)          # only _v2_accounts is exercised (reads settings)
    svc.settings = _svc_settings()
    assert svc._v2_accounts() == ["paper:schwab_1m_v2"]


def test_v2_accounts_adds_webull_under_fanout():
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.settings = _svc_settings(strategy_schwab_1m_v2_dual_broker_fanout_enabled=True)
    assert svc._v2_accounts() == ["paper:schwab_1m_v2", "live:orb"]


def test_v2_accounts_adds_webull_under_mirror_too():
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.settings = _svc_settings(strategy_schwab_1m_v2_webull_mirror_enabled=True)
    assert svc._v2_accounts() == ["paper:schwab_1m_v2", "live:orb"]


# ============================================================ store: webull_ineligible round-trip
def _session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)                      # creates webull_ineligible_today from the model
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_webull_ineligible_store_round_trip():
    store = OmsStore()
    sf = _session_factory()
    with sf() as session:
        acct = BrokerAccount(name="live:orb", provider="webull", environment="live")
        session.add(acct)
        session.flush()
        aid = acct.id
        store.record_webull_ineligible_entry(
            session, broker_account_id=aid, symbol="foo", session_date="2026-07-25",
            reason_text="not tradable", first_seen_at=datetime(2026, 7, 25, 12, 0),
        )
        # upsert bumps hit_count, normalizes the symbol to upper.
        store.record_webull_ineligible_entry(
            session, broker_account_id=aid, symbol="FOO", session_date="2026-07-25",
            reason_text="not tradable", first_seen_at=datetime(2026, 7, 25, 12, 5),
        )
        entry = store.get_webull_ineligible_entry(
            session, broker_account_id=aid, symbol="FOO", session_date="2026-07-25")
        assert entry is not None and entry.hit_count == 2
        by_acct = store.list_webull_ineligible_symbols_by_account(
            session, broker_account_ids=[aid], session_date="2026-07-25")
        assert by_acct == {aid: {"FOO"}}
        # a different session_date does not match (daily auto-clear).
        assert store.get_webull_ineligible_entry(
            session, broker_account_id=aid, symbol="FOO", session_date="2026-07-26") is None


# ============================================================ bot: emit routing + drain + eviction
def _draft(symbol="TEST"):
    from project_mai_tai.strategy_core.schwab_1m_v2 import TradeIntentDraft
    return TradeIntentDraft(symbol=symbol, side="buy", intent_type="open",
                            quantity=Decimal("10"), reason="schwab_1m_v2 ATR Flip fan-out webull",
                            metadata={"order_type": "market", "entry_price": "1.0000",
                                      "fanout_leg": "webull"})


def _bot(**over) -> SchwabV2BotService:
    kwargs = dict(
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    kwargs.update(over)
    bot = SchwabV2BotService(Settings(**kwargs))
    bot._within_entry_window = lambda now=None: True       # hold the entry window open
    # Bypass EH re-pricing (real wall-clock dependent) — these tests assert emitter ROUTING, not the
    # EH-limit math (covered in test_v2_extended_hours_routing.py). Pass-through keeps the draft as-is.
    bot._apply_extended_hours_routing = lambda draft, now: True
    return bot


@pytest.mark.asyncio
async def test_emit_webull_fanout_legs_routes_to_webull_emitter():
    bot = _bot()
    bot.strategy._pending_webull_fanout_intents.append(_draft("TEST"))
    bot.webull_intent_emitter = AsyncMock()
    bot._webull_ineligible_symbols = lambda: set()
    await bot._emit_webull_fanout_legs()
    bot.webull_intent_emitter.emit.assert_awaited_once()
    assert bot.strategy._pending_webull_fanout_intents == []   # drained


@pytest.mark.asyncio
async def test_emit_webull_fanout_legs_skips_ineligible():
    bot = _bot()
    bot.strategy._pending_webull_fanout_intents.append(_draft("NOPE"))
    bot.webull_intent_emitter = AsyncMock()
    bot._webull_ineligible_symbols = lambda: {"NOPE"}
    await bot._emit_webull_fanout_legs()
    bot.webull_intent_emitter.emit.assert_not_awaited()        # skipped
    assert bot.strategy._pending_webull_fanout_intents == []   # still drained


@pytest.mark.asyncio
async def test_emit_webull_fanout_legs_drains_when_emitter_unset():
    bot = _bot(strategy_schwab_1m_v2_webull_account_name="")   # no webull account -> emitter stays None
    bot.strategy._pending_webull_fanout_intents.append(_draft("TEST"))
    assert bot.webull_intent_emitter is None
    await bot._emit_webull_fanout_legs()
    assert bot.strategy._pending_webull_fanout_intents == []   # drained (no unbounded growth)


@pytest.mark.asyncio
async def test_fanout_off_never_queues_or_emits():
    bot = _bot(strategy_schwab_1m_v2_dual_broker_fanout_enabled=False)
    assert bot.strategy._dual_broker_fanout_enabled is False
    bot.webull_intent_emitter = AsyncMock()                    # even if present, nothing is queued
    await bot._emit_webull_fanout_legs()
    bot.webull_intent_emitter.emit.assert_not_awaited()
