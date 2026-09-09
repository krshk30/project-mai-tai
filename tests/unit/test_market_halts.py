from datetime import UTC, datetime, timedelta

from project_mai_tai.market_halts import (
    HALT_MIN_PRINT_GAP,
    HALT_MIN_QUOTE_UPDATES,
    LiveHaltTracker,
    confirmed_halt_window,
    halt_is_confirmed,
)


def test_shared_halt_definition_requires_time_and_continuing_quotes() -> None:
    start = datetime(2026, 8, 24, 15, 21, 14, tzinfo=UTC)
    assert HALT_MIN_PRINT_GAP == timedelta(seconds=285)
    assert HALT_MIN_QUOTE_UPDATES == 2
    assert not halt_is_confirmed(
        last_print_at=start,
        through_at=start + timedelta(seconds=284),
        quote_updates=2,
    )
    assert not halt_is_confirmed(
        last_print_at=start,
        through_at=start + timedelta(seconds=285),
        quote_updates=1,
    )
    assert halt_is_confirmed(
        last_print_at=start,
        through_at=start + timedelta(seconds=285),
        quote_updates=2,
    )


def test_batch_and_live_paths_use_the_same_confirmation_function() -> None:
    start = datetime(2026, 8, 24, 15, 21, 14, tzinfo=UTC)
    reopen = start + timedelta(minutes=5)
    historical = confirmed_halt_window(
        last_print_at=start,
        reopen_print_at=reopen,
        quote_updates=2,
    )
    live = LiveHaltTracker()
    live.observe_print(start)
    live.observe_quote(start + timedelta(seconds=1))
    observation = live.observe_quote(start + timedelta(seconds=285))

    assert historical is not None
    assert observation.state == "CONFIRMED"
    assert observation.newly_confirmed is True
    assert live.observe_print(reopen) == historical


# ---------------------------------------------------------------------------
# ⛔⭐⭐ SESSION GUARD — a print gap that spans a MARKET CLOSURE is not a halt.
#
# Live v2 confirmed a halt on SUNE at 03:59 ET against a last print of 19:59:58 ET the previous
# evening: ~8 hours of closed market. Nothing was held and no decision was gated, but it paged the
# operator as the first real halt ever seen and spent a once-only alarm on an artefact.
# ---------------------------------------------------------------------------

from datetime import UTC as _UTC  # noqa: E402
from datetime import datetime as _dt  # noqa: E402
from datetime import date  # noqa: E402

from project_mai_tai.market_halts import (  # noqa: E402
    LiveHaltTracker as _Tracker,
)
from project_mai_tai.market_halts import (  # noqa: E402
    session_is_continuous,
)

# The real event, in UTC. 23:59:58Z = 19:59:58 ET; 07:59:01Z next day = 03:59:01 ET.
_SUNE_LAST_PRINT = _dt(2026, 9, 8, 23, 59, 58, tzinfo=_UTC)
_SUNE_CONFIRM_AT = _dt(2026, 9, 9, 7, 59, 1, tzinfo=_UTC)
# A genuine intraday pause, shaped like the real NUR LULD halt (10:51 -> 10:56 ET).
_NUR_LAST_PRINT = _dt(2026, 9, 8, 14, 51, 59, tzinfo=_UTC)
_NUR_CONFIRM_AT = _dt(2026, 9, 8, 14, 56, 59, tzinfo=_UTC)


def test_session_is_continuous_rejects_the_real_overnight_span():
    assert session_is_continuous(_SUNE_LAST_PRINT, _SUNE_CONFIRM_AT) is False


def test_session_is_continuous_accepts_a_real_intraday_span():
    assert session_is_continuous(_NUR_LAST_PRINT, _NUR_CONFIRM_AT) is True


def test_a_gap_confirming_just_after_the_0400_open_is_still_rejected():
    """⛔ WHY A TIME-OF-DAY TEST IS NOT ENOUGH: 08:01Z = 04:01 ET is inside session hours, but the
    gap still spans the closure."""
    assert session_is_continuous(_SUNE_LAST_PRINT, _dt(2026, 9, 9, 8, 1, tzinfo=_UTC)) is False


def test_a_weekend_span_is_rejected():
    friday_print = _dt(2026, 9, 4, 19, 0, tzinfo=_UTC)      # Fri 15:00 ET
    monday_quote = _dt(2026, 9, 7, 14, 0, tzinfo=_UTC)      # Mon 10:00 ET
    assert session_is_continuous(friday_print, monday_quote) is False


def _drive(tracker: _Tracker, last_print, quote_at):
    tracker.observe_print(last_print)
    return tracker.observe_quote(quote_at)


def test_the_LIVE_tracker_refuses_to_confirm_the_real_SUNE_artefact():
    """⛔ THE CONTROL. This exact sequence produced [V2-HALT-CONFIRMED] in production."""
    tracker = _Tracker(require_continuous_session=True)
    tracker.observe_print(_SUNE_LAST_PRINT)
    first = tracker.observe_quote(_SUNE_CONFIRM_AT)
    second = tracker.observe_quote(_SUNE_CONFIRM_AT + timedelta(seconds=30))
    third = tracker.observe_quote(_SUNE_CONFIRM_AT + timedelta(seconds=60))
    for obs in (first, second, third):
        assert obs.newly_confirmed is False
        assert obs.state == "UNKNOWN"
    assert tracker.confirmed is False
    # ⛔ And no evidence accumulated overnight, so nothing confirms the moment the session opens.
    assert tracker.quote_updates == 0


def test_the_LIVE_tracker_STILL_confirms_a_genuine_intraday_halt():
    """PINS THE OTHER DIRECTION. The guard must not turn the detector off."""
    tracker = _Tracker(require_continuous_session=True)
    tracker.observe_print(_NUR_LAST_PRINT)
    tracker.observe_quote(_NUR_CONFIRM_AT - timedelta(seconds=10))
    final = tracker.observe_quote(_NUR_CONFIRM_AT)
    assert final.newly_confirmed is True
    assert final.state == "CONFIRMED"


def test_the_DEFAULT_tracker_is_byte_identical_to_the_old_behaviour():
    """⛔⭐⭐ THE BLAST-RADIUS CONTROL. paper_exit.py and three research scripts share this module.
    Default-off must still confirm the overnight span exactly as it did before, or this fix has
    silently moved historical halt windows and every result derived from them."""
    tracker = _Tracker()
    assert tracker.require_continuous_session is False
    tracker.observe_print(_SUNE_LAST_PRINT)
    tracker.observe_quote(_SUNE_CONFIRM_AT)
    final = tracker.observe_quote(_SUNE_CONFIRM_AT + timedelta(seconds=30))
    assert final.newly_confirmed is True, "the historical definition must be unchanged"


def test_the_live_v2_bot_actually_opts_in():
    """⛔ A flag nothing sets is a dead guard. Pin the live call site, not just the capability."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src/project_mai_tai/services/schwab_1m_v2_bot.py"
    assert "LiveHaltTracker(require_continuous_session=True)" in src.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ⛔⭐⭐ THE EPISODE IS BOUNDED BY ITS SESSION (codex-2, #927).
#
# The first guard lived only in observe_quote. Two halves of one root escaped it:
#   (a) the PRINT that closes the span still ran through unguarded confirmed_halt_window, so
#       quotes at 19:59 ET + the first print at 04:01 ET next day returned an overnight HaltWindow;
#   (b) `confirmed` / `quote_updates` SURVIVED the boundary, so after a genuine intraday
#       confirmation the symbol kept reporting as halted into the next session.
# Confirming and closing are two ends of one episode. A boundary that refuses one must refuse
# the other, and must end the episode rather than merely decline to judge it.
# ---------------------------------------------------------------------------

_PRE_CLOSE_QUOTE = _dt(2026, 9, 8, 23, 59, 0, tzinfo=_UTC)   # 19:59 ET
_NEXT_SESSION_PRINT = _dt(2026, 9, 9, 8, 1, 0, tzinfo=_UTC)  # 04:01 ET next day


def test_pre_close_quotes_then_first_next_session_print_yields_NO_halt_window():
    """⛔ CONTROL 1, codex-2's exact repro: seed two quote updates at 19:59 ET, then send the
    first print at 04:01 ET the next day. observe_print must NOT return a HaltWindow."""
    tracker = _Tracker(require_continuous_session=True)
    tracker.observe_print(_dt(2026, 9, 8, 23, 55, tzinfo=_UTC))     # 19:55 ET, last real print
    tracker.observe_quote(_PRE_CLOSE_QUOTE)
    tracker.observe_quote(_PRE_CLOSE_QUOTE + timedelta(seconds=30))

    window = tracker.observe_print(_NEXT_SESSION_PRINT)
    assert window is None, "a print that closes an overnight gap is not a halt reopen"
    # ...and the episode is over, not carried forward.
    assert tracker.confirmed is False
    assert tracker.quote_updates == 0


def test_a_confirmed_halt_does_not_survive_into_the_next_session():
    """⛔ CONTROL 2, codex-2's exact repro: a GENUINE intraday confirmation, then a next-session
    quote. The observation must be UNKNOWN and the retained episode state must be CLEARED, because
    `_sync_halt_data_health` reads `confirmed` and would otherwise report the symbol as currently
    halted across a new session."""
    tracker = _Tracker(require_continuous_session=True)
    tracker.observe_print(_NUR_LAST_PRINT)
    tracker.observe_quote(_NUR_CONFIRM_AT - timedelta(seconds=10))
    confirmed = tracker.observe_quote(_NUR_CONFIRM_AT)
    assert confirmed.newly_confirmed is True and tracker.confirmed is True   # a REAL halt first

    obs = tracker.observe_quote(_dt(2026, 9, 9, 14, 0, tzinfo=_UTC))         # 10:00 ET next day
    assert obs.state == "UNKNOWN"
    assert obs.newly_confirmed is False
    assert tracker.confirmed is False, "a halt episode must not outlive its session"
    assert tracker.quote_updates == 0, "evidence from the old session must not persist"


def test_the_default_tracker_still_carries_episode_state_across_the_boundary():
    """⛔ BLAST-RADIUS CONTROL, unchanged in spirit from the default-off pin: research consumers
    (paper_exit + 3 scripts) must be byte-identical. With the guard off, the overnight print still
    produces its window exactly as before."""
    tracker = _Tracker()
    tracker.observe_print(_dt(2026, 9, 8, 23, 55, tzinfo=_UTC))
    tracker.observe_quote(_PRE_CLOSE_QUOTE)
    tracker.observe_quote(_PRE_CLOSE_QUOTE + timedelta(seconds=30))
    assert tracker.observe_print(_NEXT_SESSION_PRINT) is not None, "default-off must be unchanged"


# ---------------------------------------------------------------------------
# ⛔⭐⭐ FULL-CLOSURE HOLIDAYS ARE NOT SESSIONS (codex-2, #927).
#
# The first guard excluded weekends and nothing else. A gap lying ENTIRELY WITHIN a holiday —
# both ends on the same weekday date, both inside 04:00-20:00 — passed as one continuous session.
# A quiet Thanksgiving would confirm as a halt on exactly the arithmetic that produced the
# overnight SUNE artefact. 2026-11-26 (Thanksgiving) and 2026-09-07 (Labor Day) are weekdays.
# ---------------------------------------------------------------------------

from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS  # noqa: E402
import project_mai_tai.market_halts as _mh  # noqa: E402


def test_a_gap_entirely_inside_a_holiday_is_not_a_continuous_session():
    """⛔ THE CONTROL. Thanksgiving 2026-11-26 is a Thursday; 10:00 -> 15:00 ET is inside the
    04:00-20:00 window on ONE date. Weekday and same-date checks both pass; the market is shut."""
    a = _dt(2026, 11, 26, 15, 0, tzinfo=_UTC)   # 10:00 ET
    b = _dt(2026, 11, 26, 20, 0, tzinfo=_UTC)   # 15:00 ET
    assert session_is_continuous(a, b) is False


def test_the_live_tracker_refuses_to_confirm_a_holiday_gap():
    """Same case driven through the tracker: a five-hour holiday gap must not confirm."""
    tracker = _Tracker(require_continuous_session=True)
    tracker.observe_print(_dt(2026, 11, 26, 15, 0, tzinfo=_UTC))
    tracker.observe_quote(_dt(2026, 11, 26, 19, 30, tzinfo=_UTC))
    obs = tracker.observe_quote(_dt(2026, 11, 26, 20, 0, tzinfo=_UTC))
    assert obs.newly_confirmed is False
    assert obs.state == "UNKNOWN"
    assert tracker.confirmed is False


def test_a_span_across_a_holiday_closure_is_refused_on_the_print_path_too():
    """The print that closes a span over a holiday must not produce a HaltWindow either —
    the same both-ends discipline as the overnight case."""
    tracker = _Tracker(require_continuous_session=True)
    tracker.observe_print(_dt(2026, 11, 25, 20, 0, tzinfo=_UTC))   # Wed 15:00 ET, real session
    tracker.observe_quote(_dt(2026, 11, 25, 20, 30, tzinfo=_UTC))
    tracker.observe_quote(_dt(2026, 11, 25, 21, 0, tzinfo=_UTC))
    assert tracker.observe_print(_dt(2026, 11, 27, 15, 0, tzinfo=_UTC)) is None  # Fri, post-holiday


def test_an_ordinary_trading_weekday_is_still_a_session():
    """⛔ PINS THE OTHER DIRECTION. A holiday term that swallowed normal days would silently
    disable the detector — the failure this guard exists to prevent, inverted."""
    a = _dt(2026, 9, 8, 14, 51, 59, tzinfo=_UTC)   # Tue 10:51 ET, a real trading day
    b = _dt(2026, 9, 8, 14, 56, 59, tzinfo=_UTC)
    assert session_is_continuous(a, b) is True


def test_the_holiday_list_is_the_SHARED_one_and_not_a_private_copy():
    """⛔⭐⭐ A SECOND COPY WOULD ROT INDEPENDENTLY. time_utils' own comment says the list must be
    rolled forward yearly or 'window checks silently treat an un-listed holiday as a normal trading
    day' — which is precisely this defect. Pin the identity so a future edit cannot fork it."""
    assert _mh.US_MARKET_HOLIDAYS is US_MARKET_HOLIDAYS
    assert date(2026, 11, 26) in _mh.US_MARKET_HOLIDAYS   # Thanksgiving
    assert date(2026, 9, 7) in _mh.US_MARKET_HOLIDAYS     # Labor Day
