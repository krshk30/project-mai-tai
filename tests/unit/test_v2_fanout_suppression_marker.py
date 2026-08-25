"""§266 — the marker #739 never shipped: a prevented duplicate must be COUNTED.

⛔⭐⭐ WHY THIS EXISTS. #739 added `and not state.fanout_webull_claimed` and **no `else`**, so a
suppression logged nothing at all. "The fix is preventing duplicates" and "the reactive path never
runs" were indistinguishable from outside — which is B28's own thesis (a feature that never
produced its success marker did not ship), violated two days after we built the tool for it.

⛔ THE COST WAS AN UNANSWERABLE GRADE, not a missing nicety. With no marker the only instrument for
#739 is signal 4 — a RATE over segments carrying `cw_arm_bar_ts`, running at a median 4 segments a
day against a 119-segment baseline, i.e. ~30 sessions to a verdict. A suppression is an EVENT and
is readable on the FIRST session the path runs.

⛔⭐ THESE ARE BEHAVIOURAL TESTS, NOT SOURCE-INSPECTION TESTS. #739's own suite asserts on the text
of the function (`assert "not state.fanout_webull_claimed" in gate`). That style cannot tell
whether the branch is REACHED, which is the entire question a success marker answers — a marker
that is present in the source and unreachable in practice passes every source assertion and still
prints zero for ever. So every test below drives `_cw_v2_quote` and reads the emitted log.

⭐ AND THE CONTROL RUNS IN BOTH DIRECTIONS. A suppression counter that has only ever printed zero
proves nothing, so `test_it_FIRES_on_reactive_after_resting` is the known-positive; the silent
cases are what stop it printing on everything.
"""
from __future__ import annotations

import logging

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy


SUPPRESSED = "[V2-FANOUT-REACTIVE-SUPPRESSED]"
LATCHED = "[V2-FANOUT-REACTIVE-LATCHED]"


class _Quote:
    """The reactive path reads `last_price` + `quote_time_ms` off the quote and nothing else."""

    def __init__(self, px: float, ms: int) -> None:
        self.last_price = px
        self.quote_time_ms = ms
        self.bid_price = 0.0
        self.ask_price = 0.0


# 2026-08-24 14:00:00 UTC = 10:00 ET — inside RTH on purpose. The EH branch requires a live bar
# and would return None before ever reaching the fan-out block, which would make every test below
# pass for the wrong reason.
RTH_MS = 1787580000000


def _strat(*, fanout: bool = True) -> SchwabV2Strategy:
    return SchwabV2Strategy(
        Settings(
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
            strategy_schwab_1m_v2_cw_v2_reactive_entry_enabled=True,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=fanout,
        )
    )


def _armed(strat: SchwabV2Strategy, symbol: str = "EGG"):
    """A symbol armed and eligible for the reactive intrabar break.

    Every field here is a guard in `_cw_v2_quote` upstream of the fan-out block. `state.bars` is
    left EMPTY deliberately: `_liquidity_floor_ok` returns True when there are no bars, so the
    floor cannot silently veto the entry and turn a real assertion into a vacuous one.
    """
    st = strat.watchlist_state(symbol)
    st.cw_armed = True
    st.cw_bars_waited = 2
    st.cw_segment_high = 5.00       # rule 6: px must break this
    st.cw_flip_level = 4.00         # rule 7: px and the forming-bar low must both clear this
    st.cw_bar_low_so_far = 0.0      # seeded from px on the first quote of the bar
    st.position_qty = 0
    st.cw_reclaim_taken = False
    st.cw_v2_emit_claimed = False
    st.cw_resting_taken = False
    st.resting_active = False
    return st


def _run(strat: SchwabV2Strategy, st, caplog, px: float = 5.50):
    with caplog.at_level(logging.INFO, logger="project_mai_tai.strategy_core.schwab_1m_v2"):
        draft = strat._cw_v2_quote(st, _Quote(px, RTH_MS))
    return draft, caplog.text


# ── the known-positive ───────────────────────────────────────────────────────────────────────
def test_it_FIRES_on_reactive_after_resting(caplog) -> None:
    """⭐ THE CONTROL. The §82 shape, reproduced: a resting leg has already claimed the latch for
    this segment and the reactive path then breaks the trigger. #739 suppresses the second leg —
    and as of §266 it SAYS SO."""
    strat = _strat()
    st = _armed(strat)
    st.fanout_webull_claimed = True                 # the resting leg got here first
    st.fanout_claim_ms = strat._now_ms() - 31_400   # STKH's live gap, 2026-08-14

    draft, text = _run(strat, st, caplog)

    assert SUPPRESSED in text, (
        "the prevented duplicate is STILL silent — #739 has no observable and signal 4 remains "
        "the only instrument"
    )
    assert LATCHED not in text, "a suppression must not also claim the latch"
    assert not strat._pending_webull_fanout_intents, (
        "⛔ BEHAVIOUR REGRESSION: the duplicate leg was actually queued. The marker is worthless "
        "if the thing it reports did not happen."
    )
    assert draft is not None, "the SCHWAB primary must still be emitted — only the Webull leg is suppressed"


def test_the_suppression_line_carries_a_USABLE_claim_age(caplog) -> None:
    """`claim_age_ms` is the §82 signature — reactive following rth_resting seconds-to-minutes
    later. A line that only says 'suppressed' cannot separate that from a same-instant double
    evaluation."""
    strat = _strat()
    st = _armed(strat)
    st.fanout_webull_claimed = True
    st.fanout_claim_ms = strat._now_ms() - 31_400
    _, text = _run(strat, st, caplog)

    line = next(ln for ln in text.splitlines() if SUPPRESSED in ln)
    age = int(line.split("claim_age_ms=")[1].split()[0])
    assert 31_000 <= age <= 40_000, f"claim age is not the real elapsed time: {age}"


def test_an_UNSTAMPED_claim_reports_minus_one_not_a_fake_age(caplog) -> None:
    """⛔ A claim with no timestamp must NOT render as `claim_age_ms=<now>`, which would read as a
    ~57-year-old claim and look like data corruption rather than a missing stamp. Name the absence."""
    strat = _strat()
    st = _armed(strat)
    st.fanout_webull_claimed = True
    st.fanout_claim_ms = 0
    _, text = _run(strat, st, caplog)

    line = next(ln for ln in text.splitlines() if SUPPRESSED in ln)
    assert "claim_age_ms=-1" in line, "an unstamped claim must be declared, never given a fake age"


# ── the silent cases: what stops it printing on everything ───────────────────────────────────
def test_it_is_SILENT_on_a_first_reactive_entry(caplog) -> None:
    """The ordinary case. Nothing has claimed the latch, so the leg is queued and this is a
    LATCHED, never a SUPPRESSED."""
    strat = _strat()
    st = _armed(strat)
    st.fanout_webull_claimed = False

    draft, text = _run(strat, st, caplog)

    assert SUPPRESSED not in text, "a first entry must never be counted as a prevented duplicate"
    assert LATCHED in text, "the denominator is missing — SUPPRESSED=0 would be unreadable"
    assert len(strat._pending_webull_fanout_intents) == 1, "the fan-out leg must still be queued"
    assert st.fanout_webull_claimed is True and st.fanout_claim_ms > 0, "the claim must be stamped"
    assert draft is not None


def test_it_is_SILENT_when_the_FANOUT_IS_OFF(caplog) -> None:
    """⛔⭐⭐ THE LOAD-BEARING NEGATIVE, and the reason the `else` is NESTED.

    `fanout_enabled == False` is not a prevented duplicate — it is the fan-out being off. A flat
    `else` on the combined condition would emit SUPPRESSED for every qualifying quote of every
    symbol on a fan-out-off deployment: a confident wrong number, strictly worse than the missing
    one it replaces. This test is what kills that mutant.
    """
    strat = _strat(fanout=False)
    st = _armed(strat)
    st.fanout_webull_claimed = False

    draft, text = _run(strat, st, caplog)

    assert SUPPRESSED not in text, (
        "⛔ fan-out being OFF is being counted as a prevented duplicate — a different population "
        "wearing §82's number"
    )
    assert LATCHED not in text
    assert not strat._pending_webull_fanout_intents
    assert draft is not None, "the Schwab primary is unaffected by the fan-out flag"


def test_it_is_SILENT_when_the_entry_never_qualifies(caplog) -> None:
    """No break, no entry, no marker. Guards upstream of the fan-out block must keep both lines off
    the tape entirely — otherwise the denominator inflates with quotes that never entered."""
    strat = _strat()
    st = _armed(strat)
    st.fanout_webull_claimed = True

    draft, text = _run(strat, st, caplog, px=4.50)   # below cw_segment_high (5.00): rule 6 declines

    assert draft is None
    assert SUPPRESSED not in text and LATCHED not in text


def test_the_two_MARKERS_ARE_NOT_SUBSTRINGS_OF_EACH_OTHER(caplog) -> None:
    """⛔⭐⭐ THE NEAR-MISS THIS PINS. The first draft of the LATCHED line ended
    `DENOMINATOR for [V2-FANOUT-REACTIVE-SUPPRESSED]` — so a production
    `grep -c "[V2-FANOUT-REACTIVE-SUPPRESSED]"` matched every LATCHED line too, and the suppression
    count came back inflated by EXACTLY its own denominator. Two metrics that must differ, reading
    the same number, for a reason invisible in either line.

    ⭐ THE POINT IS THE GREP, NOT THE PREFIX. These lines are counted from the tape by string
    match, so a marker is only distinct if the SIBLING'S WHOLE LINE does not contain it.
    """
    strat = _strat()
    st = _armed(strat)
    st.fanout_webull_claimed = False
    _, text = _run(strat, st, caplog)

    latched_line = next(ln for ln in text.splitlines() if LATCHED in ln)
    assert SUPPRESSED not in latched_line, (
        "the LATCHED line contains the SUPPRESSED marker — every `grep -c` of the suppression "
        "count will also count its own denominator"
    )

    strat2 = _strat()
    st2 = _armed(strat2)
    st2.fanout_webull_claimed = True
    st2.fanout_claim_ms = strat2._now_ms() - 1000
    _, text2 = _run(strat2, st2, caplog)

    suppressed_line = next(ln for ln in text2.splitlines() if SUPPRESSED in ln)
    assert LATCHED not in suppressed_line, "the SUPPRESSED line contains the LATCHED marker"
