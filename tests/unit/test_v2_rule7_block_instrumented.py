"""Rule 7 must SAY when it declines an otherwise-qualifying entry.

⛔⭐⭐ THE MISSING NEGATIVE (the fourth found this week). Rule 7 — "the whole forming bar must have
stayed above the flip level" — was a bare `return None`. So "rule 7 never binds" and "rule 7 blocks
constantly" were the same observation from outside, and the frequency that decides a live design
question had to be RECONSTRUCTED from `[V2-CW-STATE-PROBE]` against the trade tape (65 of 5,129
rule-6 passes = 1.3%, an UPPER BOUND: the replay walks every print while v2 evaluates on 5-second
quote polls). One line makes it measured.

⭐ WHY IT MATTERS NOW: rule 7 is intrabar state a broker stop CANNOT carry, so it is the entire
behavioural difference for the resting reactive entry (docs/v2-reactive-resting-entry-design.md §2).

⛔ EDGE-TRIGGERED. This sits on the per-quote intrabar path — a level-triggered line would be the
trade-coach retry-storm shape (45% CPU while nominally disabled). One line per forming bar, and ONLY
when rule 6 already passed, so it counts *otherwise-qualifying* declines and nothing else.
"""
from __future__ import annotations

import logging


from project_mai_tai.strategy_core.schwab_1m_v2 import SymbolState

MARKER = "[V2-CW-RULE7-BLOCK]"


def _lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if MARKER in r.getMessage()]


def test_the_dedupe_field_exists_and_defaults_to_zero() -> None:
    """The dedupe key is per forming bar; a fresh state must be able to log its first block."""
    assert SymbolState(symbol="X").cw_rule7_logged_bar_ts == 0


def test_the_population_is_guaranteed_by_rule_6_not_by_a_local_gate() -> None:
    """⭐ WHY THERE IS NO `px > trig` CHECK AT THE LOG SITE.

    Rule 6 (`if trig <= 0.0 or px <= trig: return None`) has already returned for any quote that did
    not break the trigger, so reaching the rule-7 branch MEANS the entry was otherwise qualifying.
    An explicit gate here would be dead code — one was written, and the mutation that deleted it
    left every test GREEN, which is how it was found. **Deleting a condition must change a result,
    or it is not a condition.** The behavioural guarantee is pinned by
    `test_no_line_when_rule6_never_passed`; this test pins the REASON so nobody re-adds the gate."""
    import inspect

    from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy

    src = inspect.getsource(SchwabV2Strategy)
    before = src.split(MARKER, 1)[0]
    rule6 = "if trig <= 0.0 or px <= trig:"
    rule7 = "if fl <= 0.0 or px <= fl or state.cw_bar_low_so_far <= fl:"
    assert before.index(rule6) < before.index(rule7), (
        "rule 6 must still return before rule 7 is reached — that ordering IS the population guard"
    )
    guard = before.rsplit(rule7, 1)[1]
    assert "cw_rule7_logged_bar_ts" in guard, "must dedupe per forming bar, not fire per quote"


def test_the_line_carries_every_field_needed_to_audit_the_decision() -> None:
    import inspect

    from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy

    src = inspect.getsource(SchwabV2Strategy)
    line = src.split(MARKER, 1)[1][:400]
    for field in ("px=", "trig=", "low_sf=", "flip_level="):
        assert field in line, f"the block line must carry {field} so the decision is re-checkable"


def test_rule7_still_declines_the_entry() -> None:
    """⛔ MUTATION GUARD — this is a LOG-ONLY change. The line must not become a `pass`: rule 7
    still returns None and still blocks the entry."""
    import inspect

    from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy

    src = inspect.getsource(SchwabV2Strategy)
    after = src.split(MARKER, 1)[1]
    tail = after.split("\n")[:12]
    assert any("return None" in ln for ln in tail), (
        "rule 7 must still return None after logging — instrumenting a decline must never "
        "accidentally permit it"
    )


# ── BEHAVIOURAL: drive the real per-quote path and capture the line ──────────────────────────
from datetime import datetime                                              # noqa: E402
from types import SimpleNamespace                                          # noqa: E402
from zoneinfo import ZoneInfo                                              # noqa: E402

from project_mai_tai.settings import Settings                              # noqa: E402
from project_mai_tai.strategy_core.schwab_1m_v2 import (                   # noqa: E402
    OHLCVBar,
    SchwabV2Strategy,
)

_ET = ZoneInfo("America/New_York")
BAR_TS = int(datetime(2026, 8, 10, 11, 0, tzinfo=_ET).timestamp() * 1000)   # 11:00 ET, RTH


def _armed_strategy():
    """PRODUCTION flags — cw_v2 + reactive on. ⛔ A bare Settings() leaves cw_v2 off and every
    assertion below would pass by never reaching rule 7."""
    s = SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
    ))
    s._reactive_entry_enabled = True
    s._entries_held = False
    return s


def _armed_state(strat, *, trig=10.0, flip=9.0):
    from project_mai_tai.strategy_core.schwab_1m_v2 import SymbolState as _S
    st = _S(symbol="TEST")
    st.cw_armed = True
    st.cw_bars_waited = 2
    st.cw_segment_high = trig
    st.cw_trigger = trig
    st.cw_flip_level = flip
    st.position_qty = 0
    st.bars.append(OHLCVBar(timestamp_ms=BAR_TS, open=9.5, high=trig, low=8.9,
                            close=9.6, volume=500_000))
    return st


def _q(px, ts=BAR_TS):
    return SimpleNamespace(last_price=px, bid_price=px - 0.01, ask_price=px + 0.01,
                           quote_time_ms=ts)


def test_rule7_block_is_logged_when_it_declines_a_qualifying_entry(caplog) -> None:
    """BEHAVIOURAL. Walk the forming bar DOWN through the flip level first, then UP through the
    trigger: rule 6 passes, rule 7 declines, and the decline must now be on the tape."""
    strat = _armed_strategy()
    st = _armed_state(strat, trig=10.0, flip=9.0)

    with caplog.at_level(logging.INFO):
        assert strat._cw_v2_quote(st, _q(8.90)) is None      # dips below flip -> low_sf = 8.90
        draft = strat._cw_v2_quote(st, _q(10.50))            # breaks the trigger

    assert draft is None, "rule 7 must still decline the entry"
    hits = _lines(caplog)
    assert len(hits) == 1, f"expected exactly one {MARKER}; got {caplog.text!r}"
    assert "low_sf=8.9000" in hits[0] and "flip_level=9.0000" in hits[0]


def test_no_line_when_rule6_never_passed(caplog) -> None:
    """⛔ THE DENOMINATOR GUARD. A quote below the trigger was declined by rule 6, not rule 7.
    Logging it would inflate the very frequency this line exists to measure."""
    strat = _armed_strategy()
    st = _armed_state(strat, trig=10.0, flip=9.0)

    with caplog.at_level(logging.INFO):
        strat._cw_v2_quote(st, _q(8.90))
        strat._cw_v2_quote(st, _q(9.50))                     # above flip, BELOW trigger

    assert _lines(caplog) == [], "rule 6 declined this quote — it is not a rule-7 block"


def test_one_line_per_forming_bar_not_per_quote(caplog) -> None:
    """⛔ EDGE-TRIGGERED. This sits on the per-quote path; a line per quote is the trade-coach
    retry-storm shape (45% CPU while nominally disabled)."""
    strat = _armed_strategy()
    st = _armed_state(strat, trig=10.0, flip=9.0)

    with caplog.at_level(logging.INFO):
        strat._cw_v2_quote(st, _q(8.90))
        for _ in range(25):
            strat._cw_v2_quote(st, _q(10.50))

    assert len(_lines(caplog)) == 1, "25 qualifying quotes in one bar must produce ONE line"


def test_a_new_forming_bar_logs_again(caplog) -> None:
    """The dedupe is per BAR, not per segment — a fresh bar is a fresh decline and must be counted,
    or the frequency under-reports."""
    strat = _armed_strategy()
    st = _armed_state(strat, trig=10.0, flip=9.0)

    with caplog.at_level(logging.INFO):
        strat._cw_v2_quote(st, _q(8.90))
        strat._cw_v2_quote(st, _q(10.50))
        st.bars.append(OHLCVBar(timestamp_ms=BAR_TS + 60_000, open=9.5, high=10.0,
                                low=8.9, close=9.6, volume=500_000))
        st.cw_bar_low_so_far = 0.0                            # new bar seeds the running low
        strat._cw_v2_quote(st, _q(8.95))
        strat._cw_v2_quote(st, _q(10.60))

    assert len(_lines(caplog)) == 2, "each forming bar's decline must be counted once"


def test_a_passing_entry_emits_no_block_line(caplog) -> None:
    """⛔ THE FALSE-POSITIVE DIRECTION. When rule 7 PASSES, there is no decline — a line here would
    over-report and an inflated count is as wrong as a missing one."""
    strat = _armed_strategy()
    st = _armed_state(strat, trig=10.0, flip=9.0)

    with caplog.at_level(logging.INFO):
        strat._cw_v2_quote(st, _q(9.60))                      # stays ABOVE the flip level
        draft = strat._cw_v2_quote(st, _q(10.50))

    assert _lines(caplog) == [], "rule 7 passed — nothing was declined"
    assert draft is not None, "the entry should have fired"
