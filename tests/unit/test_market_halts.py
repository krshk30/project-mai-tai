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
