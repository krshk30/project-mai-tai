"""Both legs should REST at their own broker and wait — not one rest while the other watches.

⛔⭐⭐ WHY. The Schwab stop-limit sits at the broker and fills itself. The Webull leg used a software
price-watcher that had to notice the same cross — and its own `position_qty != 0` gate then blocked
it, because Schwab had already filled. Measured 2026-08-13 (RTH): DFSC 0 of 3, INHD 0, OFAL 0.
XHG — which Schwab never traded — fired 7 of 7. The watcher worked only when it had no race to lose.

⛔ THE COST, and why the flag defaults OFF: the mirrored rest is BARE. Webull refuses a stop-limit
master carrying a bracket (Probe W shape B, 417) while accepting it alone (shape W7, 200). So the
Webull leg has no broker-side protection at the instant it fills.

⛔ AND THE HAZARD THIS FILE MOSTLY EXISTS FOR: an un-cancelled mirrored rest is a live order nobody
owns — FRTT 2026-08-11, 136 minutes — at the broker whose order book we cannot reliably read back.
"""
from __future__ import annotations

import inspect

from project_mai_tai.services import schwab_1m_v2_bot as bot
from project_mai_tai.strategy_core import schwab_1m_v2 as strat


def test_placing_the_schwab_rest_also_places_a_webull_rest() -> None:
    src = inspect.getsource(strat.SchwabV2Strategy._queue_resting_place)
    assert "[V2-WEBULL-RESTING-PLACE]" in src
    assert '"order_type": "STOP_LIMIT"' in src
    assert "_pending_webull_direct_intents.append" in src


def test_the_mirrored_rest_carries_NO_bracket() -> None:
    """⛔ Webull 417s a stop-limit master with legs attached. Adding bracket_* keys here would make
    every mirrored placement fail at the broker."""
    src = inspect.getsource(strat.SchwabV2Strategy._queue_resting_place)
    mirror = src.split("[V2-WEBULL-RESTING-PLACE]")[1]
    assert "bracket_target_price" not in mirror
    assert "bracket_stop_price" not in mirror
    assert "native_oco_bracket" not in mirror


def test_cancelling_the_schwab_rest_also_cancels_the_webull_one() -> None:
    """THE ORPHAN GUARD. Without this a mirrored rest stays live at Webull with nothing owning it.

    ⛔ THIS TEST WAS WEAK AND A MUTATION PROVED IT. It first asserted only that the strings existed
    in the source — so replacing `if was_webull_resting:` with `if False:` disabled the cancel
    entirely and the test still passed. Pin the CONDITION, not the presence of text.
    """
    src = inspect.getsource(strat.SchwabV2Strategy._queue_resting_cancel)
    assert "[V2-WEBULL-RESTING-CANCEL]" in src
    assert 'intent_type="cancel"' in src
    assert "if was_webull_resting:" in src, "the cancel must be reachable, not stubbed out"


def test_the_webull_cancel_is_ACTUALLY_QUEUED_when_a_mirror_was_live() -> None:
    """Behavioural, not textual: run the real method and look at the real queue.

    A source-scan cannot tell a live branch from a dead one — the mutation above proved that. This
    drives `_queue_resting_cancel` on a state with a live mirror and asserts a cancel draft comes
    out the other side.
    """
    s = object.__new__(strat.SchwabV2Strategy)
    s._pending_intents = []
    s._pending_webull_direct_intents = []
    s._atr_qty = 2
    s._webull_fanout_qty = 1

    st = strat.SymbolState(symbol="TEST")
    st.resting_active = True
    st.resting_level = 5.0
    st.resting_is_broker_order = True          # a REAL broker order was placed
    st.webull_resting_active = True            # ...and it was mirrored to Webull

    strat.SchwabV2Strategy._queue_resting_cancel(s, st, reason="reprice")

    webull = [d for d in s._pending_webull_direct_intents
              if getattr(d, "intent_type", "") == "cancel"]
    assert len(webull) == 1, "a live Webull mirror MUST produce exactly one cancel"
    assert webull[0].symbol == "TEST"
    assert st.webull_resting_active is False, "the flag must be cleared so it cannot double-cancel"


def _place_stub(mirror_on: bool) -> tuple[object, "strat.SymbolState"]:
    s = object.__new__(strat.SchwabV2Strategy)
    s._pending_intents = []
    s._pending_webull_direct_intents = []
    s._resting_entry_band_pct = 0.5
    s._eh_resting_enabled = False
    s._resting_session_is_eh = lambda *a, **k: False      # RTH
    s._atr_qty = 2
    s._webull_fanout_qty = 1
    s._webull_resting_mirror_enabled = mirror_on
    s._dual_broker_fanout_enabled = True
    return s, strat.SymbolState(symbol="TEST")


def test_placing_ACTUALLY_QUEUES_a_webull_rest_and_marks_it_live() -> None:
    """Behavioural. ⛔ A source-scan missed this too: stubbing `webull_resting_active = True` to
    False left every textual test green, while the cancel could then never fire — a silent orphan.
    """
    s, st = _place_stub(mirror_on=True)
    strat.SchwabV2Strategy._queue_resting_place(s, st, 5.0, slot="first")

    assert len(s._pending_webull_direct_intents) == 1, "the mirror must actually be queued"
    d = s._pending_webull_direct_intents[0]
    assert d.metadata["order_type"] == "STOP_LIMIT"
    assert d.metadata["stop_price"] == "5.0000"
    assert d.metadata["limit_price"] == "5.0250"          # 5.0 * 1.005, the same band as Schwab
    assert st.webull_resting_active is True, (
        "the flag MUST be set, or the cancel can never fire and the order is orphaned"
    )


def test_mirror_off_places_nothing_at_webull() -> None:
    s, st = _place_stub(mirror_on=False)
    strat.SchwabV2Strategy._queue_resting_place(s, st, 5.0, slot="first")
    assert s._pending_webull_direct_intents == []
    assert st.webull_resting_active is False
    assert len(s._pending_intents) == 1, "the Schwab rest is unaffected by the flag"


def test_the_two_legs_rest_at_the_SAME_price() -> None:
    """The whole point: both wait at their own broker for the same level."""
    s, st = _place_stub(mirror_on=True)
    strat.SchwabV2Strategy._queue_resting_place(s, st, 5.0, slot="first")
    schwab = s._pending_intents[0].metadata
    webull = s._pending_webull_direct_intents[0].metadata
    assert schwab["stop_price"] == webull["stop_price"]
    assert schwab["limit_price"] == webull["limit_price"]


def test_no_webull_cancel_when_no_mirror_was_live() -> None:
    """The mirror off (or never placed) must not emit a spurious cancel for a non-existent order."""
    s = object.__new__(strat.SchwabV2Strategy)
    s._pending_intents = []
    s._pending_webull_direct_intents = []
    s._atr_qty = 2
    s._webull_fanout_qty = 1

    st = strat.SymbolState(symbol="TEST")
    st.resting_active = True
    st.resting_level = 5.0
    st.resting_is_broker_order = True
    st.webull_resting_active = False           # no mirror

    strat.SchwabV2Strategy._queue_resting_cancel(s, st, reason="reprice")
    assert s._pending_webull_direct_intents == []


def test_the_cancel_state_is_captured_BEFORE_it_is_cleared() -> None:
    """Reading the flag after clearing it would cancel nothing, silently."""
    src = inspect.getsource(strat.SchwabV2Strategy._queue_resting_cancel)
    read = src.index("was_webull_resting = state.webull_resting_active")
    clear = src.index("state.webull_resting_active = False")
    assert read < clear


def test_both_mirror_drafts_use_the_CANCEL_SAFE_queue() -> None:
    """⛔ The fan-out queue is drained through `_maybe_emit` (entry-window gate, ATR-only belt,
    exit-only chokepoint). A cancel routed there can be dropped silently."""
    for fn in (strat.SchwabV2Strategy._queue_resting_place,
               strat.SchwabV2Strategy._queue_resting_cancel):
        src = inspect.getsource(fn)
        if "webull" in src.lower():
            assert "_pending_webull_direct_intents" in src
            assert "_pending_webull_fanout_intents" not in src


def test_the_bot_drains_the_direct_queue_WITHOUT_maybe_emit() -> None:
    whole = inspect.getsource(bot)
    assert "drain_webull_direct_intents" in whole
    seg = whole.split("drain_webull_direct_intents")[1].split("_emit_webull_fanout_legs")[0]
    assert "webull_intent_emitter.emit(d)" in seg, "must emit directly"
    assert "_maybe_emit" not in seg, "must NOT route through the gated path"


def test_a_missing_webull_emitter_is_LOUD_not_silent() -> None:
    """Dropping a cancel because the emitter is unset must never be quiet."""
    whole = inspect.getsource(bot)
    seg = whole.split("drain_webull_direct_intents")[1].split("_emit_webull_fanout_legs")[0]
    assert "DROPPED" in seg and "warning" in seg


def test_on_fill_does_not_double_up_with_a_live_mirror() -> None:
    """Two Webull orders behind one signal is the failure this guard prevents."""
    src = inspect.getsource(strat.SchwabV2Strategy.update_position)
    assert "not state.webull_resting_active" in src


def test_the_mirror_defaults_OFF() -> None:
    """⛔ It trades away the attached bracket. That must be chosen, not inherited."""
    from project_mai_tai.settings import Settings

    assert Settings().strategy_schwab_1m_v2_webull_resting_mirror_enabled is False
