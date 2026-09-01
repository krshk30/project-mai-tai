"""Replay of NCRA 2026-08-31 against the merged G01 fix (#859, cc58b74).

The DAY, fully derived (all evidence DB- or log-pinned, times UTC):
Three boots re-armed the SAME reconstructed segment (arm_bar_ts=1788181200000, trig frozen at
2.3800) and capped it — 13:47:00 / 14:01:12 / 15:12:33, watch_starts below. Within seconds to
13 minutes of EACH cap, the reactive path emitted a real Schwab BUY intent straight through the
"capped" segment (13:47:05 n=3 px=2.5350 · 14:01:14 n=3 px=2.6006 · 15:25:11 n=3 px=3.3600 —
n=3 = the cap's counter 2, +1 at emit), and the resting path placed reclaim rests through it
too (15:06:39, 15:12:48). All were Schwab-rejected. The counter is a FOSSIL: the live gate
reads position / cw_reclaim_taken / cw_v2_emit_claimed, never the counter — which is exactly
what G01 fixes by consuming the flags the gate actually reads.

The controlled pair varies ONE thing: the two flag writes G01 added to the cap. With them, all
three replayed emissions refuse; with them undone (the pre-G01 cap wrote only the fossil
counter), all three emit.

⚠ LIMIT, stated: this proves the fixed cap would have refused the known NCRA emissions; it does
not prove live-timing behaviour across real restarts. That grade is the next restart's tape.
"""

from __future__ import annotations

import pytest

from tests.unit.test_schwab_1m_v2_armed_segment_safety import (
    NON_ORB_MS,
    _arm,
    _bare_bot,
    _quote,
    _safe,
)

ARM_BAR_TS = 1788181200000
# (boot's watch_start_ms, the reactive emission's real px)
BOOTS = [
    (1788184020180, 2.5350),   # 13:47:00Z cap -> 13:47:05Z emit
    (1788184872517, 2.6006),   # 14:01:12Z cap -> 14:01:14Z emit
    (1788189153449, 3.3600),   # 15:12:33Z cap -> 15:25:11Z emit
]
TRIG = 2.3800
FLIP_LEVEL = 2.3659


def _booted(watch_start: int):
    """One 08-31 boot: fresh state, reconstructed arm, cap run — as the day did it."""
    s = _safe()
    st = s.watchlist_state("TEST")
    _arm(s, st, base_ts=1)
    st.cw_arm_bar_ts = ARM_BAR_TS
    st.cw_trigger = TRIG
    st.cw_segment_high = TRIG
    st.cw_flip_level = FLIP_LEVEL
    st.cw_entries_this_flip = 0                 # fresh boot: per-process state starts clean
    _bare_bot(s, {"TEST": watch_start})._cap_reconstructed_segment("TEST", stage="db-seed")
    s._entries_held = False                     # boot hold released, as on the day
    return s, st


@pytest.mark.parametrize("watch_start,px", BOOTS, ids=[f"boot@{w}" for w, _ in BOOTS])
def test_ncra_0831_replayed_emission_refused_by_the_fixed_cap(watch_start, px) -> None:
    s, st = _booted(watch_start)
    assert st.cw_resting_taken is True and st.cw_reclaim_taken is True, "cap must consume slots"
    assert s._cw_v2_quote(st, _quote(px, ts=NON_ORB_MS)) is None, (
        f"the replayed NCRA emission (px={px}) went through the capped segment again"
    )


@pytest.mark.parametrize("watch_start,px", BOOTS, ids=[f"boot@{w}" for w, _ in BOOTS])
def test_ncra_0831_emission_reproduces_when_the_g01_writes_are_undone(watch_start, px) -> None:
    """THE KNOWN-BAD ARM (one variable: the two flags G01 writes). The pre-G01 cap wrote only
    the fossil counter; undoing the flag writes reproduces the day's emission on every boot —
    which is what makes the refusal above a measurement rather than a hope."""
    s, st = _booted(watch_start)
    st.cw_resting_taken = False                 # undo exactly what G01 added
    st.cw_reclaim_taken = False
    assert st.cw_entries_this_flip >= 1, "the fossil counter is written either way"
    assert s._cw_v2_quote(st, _quote(px, ts=NON_ORB_MS)) is not None, (
        f"fixture cannot reproduce the day's emission (px={px}) — the refusal test is vacuous"
    )
