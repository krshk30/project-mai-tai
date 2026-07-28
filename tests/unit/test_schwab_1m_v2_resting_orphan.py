"""The resting-order ORPHAN — a live buy-stop the strategy stopped managing (live 2026-07-28).

⭐ WHAT HAPPENED. v2 placed a resting buy-stop-limit for EGG at 14:22 ET (stop=3.9327). Price then
fell to 3.55 and the ATR trail moved well below the order, but the strategy issued no reprice and no
cancel — for the rest of the session. The operator had to cancel it BY HAND, twice in one afternoon.
The intent tape shows it exactly: two `open buy 2` intents ending `cancelled` with no matching
`[V2-RESTING-CANCEL]` log line, because the strategy never asked for those cancels.

⭐ THE MECHANISM. `_fetch_open_positions` returns virtual_positions ∪ in-flight OPEN intents. A
resting order's intent stays `submitted` for its ENTIRE life — it only resolves when price triggers
it — so the union reported qty=2 for an order that had never filled. That tripped the first gate of
`_cw_v2_resting_track`:

    if state.position_qty != 0:
        state.resting_active = False      # clears the flag, does NOT cancel the broker order
        return

From that instant the strategy believed it had no resting order, so neither the 0.5% STABLE-REST
reprice nor the flip-no-fill cancel could ever fire. Live at the broker, invisible to its owner.

⭐ WHY IT LOOKED INTERMITTENT — it is a LATCH RACE, and the latch is permanent. On the same day and
the same code path INLF repriced 24 times with `pos_qty=0` throughout: its trail moved >=0.5% every
2-3 minutes, so it repriced before the position poll ever saw its own intent. EGG's trail sat still,
the poll won the race once, and the gate then blocked every FUTURE reprice too. Losing the race a
single time orphans the symbol for good.

THE FIX: this ownership gate reads fills-only (`position_qty_held`). Every OTHER gate — reactive
entry, cooldown, re-entry, fan-out — deliberately keeps the conservative union, because dropping
resting intents there would let a market buy fire while a stop-limit rests, i.e. a double position.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

_ET = ZoneInfo("America/New_York")
IN_WIN = int(datetime(2026, 7, 10, 11, 0, tzinfo=_ET).timestamp() * 1000)


def _strat(**overrides):
    kwargs = {
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
    }
    kwargs.update(overrides)
    return SchwabV2Strategy(Settings(**kwargs))


def _sig(trail):
    return {"touch": False, "touch_price": None, "flip": None, "flip_level": None,
            "trail": trail, "loss": 0.5, "state": "short", "state_age": 3}


def _tick(strat, state, *, trail):
    strat._resting_in_window = lambda now=None: True
    strat._now_ms = lambda: 1_000_000
    state.bars.append(OHLCVBar(timestamp_ms=IN_WIN, open=trail + 1, high=trail + 1.2,
                               low=trail - 0.2, close=trail + 0.9, volume=25_000))
    strat._cw_v2_resting_track(state, _sig(trail))
    return strat.drain_pending_intents()


def _rested(strat):
    """Place one resting order and return its live state, mid-flight (intent still `submitted`)."""
    st = strat.watchlist_state("EGG")
    _tick(strat, st, trail=3.9327)
    assert st.resting_active is True
    return st


# ---------------------------------------------------------------- the EGG repro
def test_own_inflight_intent_does_not_orphan_the_resting_order() -> None:
    """THE REGRESSION. The union says qty=2 purely because of the resting order's OWN submitted
    intent; nothing has filled. The strategy must keep managing the order it placed."""
    strat = _strat()
    st = _rested(strat)

    # The position poll observes the union (2, from our own in-flight intent) — but zero shares held.
    strat.update_position("EGG", 2, held_qty=0)
    assert st.position_qty == 2, "the conservative union must be preserved for the other gates"
    assert st.position_qty_held == 0

    # The trail falls far below the resting order, exactly as EGG's did.
    drafts = _tick(strat, st, trail=3.5500)

    # The orphan is SILENT: under the bug the gate returns having queued nothing at all, and the
    # order is abandoned live at the broker. Managing it means asking for something. (A reprice is
    # cancel-this-bar / place-next-bar per the NO-OVERLAP invariant, so the flag legitimately drops
    # here — the draft, not the flag, is what separates "repricing" from "disowned".)
    assert drafts, (
        "ORPHANED: the gate disowned an order that never filled and queued nothing, so no reprice "
        "or cancel can ever fire again"
    )
    assert [d for d in drafts if d.intent_type == "cancel"], (
        f"expected a reprice cancel for the stale level, got {[d.intent_type for d in drafts]}"
    )


def test_the_orphaned_order_actually_gets_repriced_down() -> None:
    """Not just 'still tracked' — the order must FOLLOW the trail down. EGG sat at 3.93 while the
    market fell to 3.55; that gap is the whole complaint."""
    strat = _strat()
    st = _rested(strat)
    strat.update_position("EGG", 2, held_qty=0)

    levels = []
    for trail in (3.8000, 3.7000, 3.6000, 3.5500):
        for d in _tick(strat, st, trail=trail):
            if d.intent_type == "open":
                levels.append(float(d.metadata["stop_price"]))

    assert levels, "no re-place at all — the order is stuck at its original level"
    assert levels[-1] < 3.9327, f"the order never followed the trail down: {levels}"


# ---------------------------------------------------------------- the other direction
def test_a_real_filled_position_still_takes_ownership() -> None:
    """The gate's real job must survive: once we actually HOLD shares, the OTOCO exit owns the
    symbol and the resting flag is dropped. Mutation-guard against 'fix' = 'delete the gate'."""
    strat = _strat()
    st = _rested(strat)

    strat.update_position("EGG", 2, held_qty=2)          # a genuine fill
    _tick(strat, st, trail=3.5500)

    assert st.resting_active is False
    assert st.resting_level == 0.0


def test_held_defaults_to_qty_so_existing_callers_are_unchanged() -> None:
    """`held_qty` is optional; omitting it must reproduce the old behaviour exactly."""
    strat = _strat()
    st = strat.watchlist_state("EGG")
    strat.update_position("EGG", 3)
    assert st.position_qty == 3
    assert st.position_qty_held == 3


def test_gate_reads_held_not_the_union() -> None:
    """PINS THE CHOICE. Reverting the gate to `position_qty` makes this fail: held=0 with a
    non-zero union is precisely the EGG shape, and it must NOT disown the order."""
    strat = _strat()
    st = _rested(strat)
    strat.update_position("EGG", 99, held_qty=0)
    assert _tick(strat, st, trail=3.5500), "read the union here and the order is silently disowned"


# ------------------------------------------------------------ NO COOLDOWN (removed 2026-07-28)
# ⭐ WHY IT WENT. The 5-bar cooldown was invented when reclaim was UNCAPPED and could chase the same
# trade repeatedly. The per-segment cap (`_cw_v2_max_entries_per_flip` = 2: one resting + one
# reclaim) replaced the need for it.
#
# It was also ALREADY INERT: every gate that read the counter sat on a path `_cw_v2_enabled`
# short-circuits (`on_quote` returns into `_cw_v2_quote`; `_cw_entry` returns None on its first
# line), and none of the three LIVE paths -- reactive, resting, fan-out -- ever consulted it.
#
# ⛔ And it CONTRADICTED the design: the reclaim gap is 1 bar, the cooldown was 5. Wiring the counter
# back up would block the exact second entry a segment is meant to allow (resting fills bar 1, spike
# on bar 4 -> reclaim). Removed rather than left dormant -- a switched-off safety gate invites a
# future "fix" that would silently break reclaim.

def test_the_cooldown_counter_is_gone_entirely() -> None:
    """PINS THE REMOVAL. A dormant counter is what invites someone to wire it back up."""
    strat = _strat()
    st = strat.watchlist_state("EGG")
    assert not hasattr(st, "cooldown_bars_remaining")
    assert not hasattr(strat.cfg, "cooldown_bars")


def test_a_close_still_releases_the_reclaim_claim() -> None:
    """⛔ THE LOAD-BEARING PART. These two lines lived in the same block as the cooldown; removing
    'the cooldown' without keeping them would silently stop every SECOND entry in a segment."""
    strat = _strat()
    st = strat.watchlist_state("EGG")
    st.cw_v2_emit_claimed = True
    st.cw_v2_bars_since_exit = 99

    strat.update_position("EGG", 2, held_qty=2)
    strat.update_position("EGG", 0, held_qty=0)

    assert st.cw_v2_emit_claimed is False, "reclaim can never fire again"
    assert st.cw_v2_bars_since_exit == 0, "the 1-bar reclaim gap never starts counting"


def test_the_close_is_logged_without_claiming_a_cooldown(caplog) -> None:
    strat = _strat()
    strat.update_position("EGG", 2, held_qty=2)
    with caplog.at_level("INFO"):
        strat.update_position("EGG", 0, held_qty=0)
    assert "position closed" in caplog.text
    assert "reclaim claim released" in caplog.text
    assert "cooldown armed" not in caplog.text
    assert "real-position-closed" in caplog.text


def test_a_spurious_close_is_still_labelled(caplog) -> None:
    """The union fires this transition when one of our OWN resting intents goes terminal. It no
    longer arms anything, but it DOES still release the reclaim claim, so it stays distinguishable."""
    strat = _strat()
    strat.update_position("EGG", 2, held_qty=0)      # in-flight resting intent only
    with caplog.at_level("INFO"):
        strat.update_position("EGG", 0, held_qty=0)
    assert "SPURIOUS-no-shares-ever-held" in caplog.text


def test_the_per_segment_cap_is_what_bounds_re_entry_now() -> None:
    """The cap is the replacement for the cooldown; pin BOTH values so the bound cannot vanish.

    The cap is reclaim-driven: 1 with reclaim off, 2 with it on. PRODUCTION runs reclaim ON
    (re-enabled 2026-07-27), so the live bound is 2 = one resting + one reclaim per ATR segment --
    exactly the shape the cooldown was removed in favour of.
    """
    assert _strat()._cw_v2_max_entries_per_flip == 1                      # reclaim off

    live = _strat(strategy_schwab_1m_v2_cw_v2_reclaim_enabled=True)       # production
    assert live._cw_v2_max_entries_per_flip == 2

    # ⛔ The gap is ENV-SET in production (`..._CW_V2_RECLAIM_GAP_BARS=1`); the code default is 0.
    # Pinning the default here documents the divergence instead of hiding it -- reading 0 from this
    # file and calling it the live value is the same trap the vol floor sprang (5000 vs a live
    # 10000). Whatever it is, it is FAR below the 5-bar cooldown that used to contradict it.
    assert _strat()._cw_v2_reclaim_gap_bars == 0, "code default"
    gapped = _strat(strategy_schwab_1m_v2_cw_v2_reclaim_gap_bars=1)       # what the box runs
    assert gapped._cw_v2_reclaim_gap_bars == 1
