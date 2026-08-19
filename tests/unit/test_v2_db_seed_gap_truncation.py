"""The db-seed must be bounded by TIME as well as by ROW COUNT.

⛔⭐⭐ PROVEN LIVE 2026-08-18, not hypothetical. `DB_SEED_BAR_LIMIT` takes 250 ROWS, so on a
thinly-traded name it reaches back as far as the rows do:

    CAST 08-18:  38 bars today | 61-DAY HOLE | 06-18 (2572), 06-15 (2675), 06-12 (1411)
    the 250th-newest CAST bar is dated 2026-06-18 18:49
    -> [V2-CW-ARM] CAST armed bar_ts=2026-06-18 16:14 ET  flip_level=7.9889
    -> CAST actually traded 1.04-1.28 that day.                        ** 6.7x OFF **

⛔ EVERY DOWNSTREAM GUARD IS EXEMPT, MIS-ORDERED, OR ABSENT — which is why the fix is at the input:
  * `min_bars` (~135) explicitly EXEMPTS ATR-Flip, and ATR-Flip is the path that armed CAST;
  * `[V2-CW-SEED-CAP]` is post-hoc and has failed TWICE — by 50ms ordering on the REST-warmup path
    (#619) and by never running at all for CAST (08-18);
  * the pending-cross stash only stops a MACD/VWAP cross, never an ARM.

⛔⭐ AND THE REJECTS WERE A PROTECTIVE ACCIDENT, NOT A GUARD. 33 of 454 v2 entries carried an arm
bar from a prior session; 32 were refused by Schwab because the level was absurd, and ONE (BQ,
08-12, arm bar 06-11) FILLED — for +1.75%. A stale level that happens to land near the live price
fills exactly like a good one, and afterwards the books cannot tell them apart.
⇒ Severity is about the MECHANISM. Realised damage is one lucky winner.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from project_mai_tai.services.schwab_1m_v2_bot import (
    DB_SEED_MAX_MISSED_SESSIONS,
    SchwabV2BotService,
)


class _Row:
    def __init__(self, bar_time: datetime, close: float = 1.0) -> None:
        self.bar_time = bar_time
        self.open_price = self.high_price = self.low_price = self.close_price = close
        self.volume = 100


class _Session:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *_a, **_k):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self._rows))


class _Strategy:
    """Records exactly which bars reached the strategy."""

    def __init__(self):
        self.seen: list = []
        self._st = SimpleNamespace(
            pending_path_macd=False,
            pending_path_vwap=False,
            pending_cross_bar_ts_ms=0,
            bars=self.seen,
        )

    def on_bar(self, _symbol, bar):
        self.seen.append(bar)

    def watchlist_state(self, _symbol):
        return self._st


def _seed(rows: list[_Row], sessions: set | None = None, today: date | None = None):
    """⛔⭐ Drives the SHIPPED `_seed_strategy_bars_from_db`, never a copy of its logic.
    A test that re-implements the walk stays green when production changes — the
    fixture-differs-from-production trap. Only the DB and the strategy are stubbed."""
    bot = object.__new__(SchwabV2BotService)
    bot.session_factory = lambda: _Session(rows)
    bot._db_seeded = set()
    bot._db_seed_gap_truncations = 0
    bot.strategy = _Strategy()
    # ⛔⭐ The cap is stubbed to a RECORDER, not silenced. It runs AFTER the replay loop, so every
    # arm inside `on_bar` has already fired and stamped — the 50ms race of #619, still present on
    # this path. The fix deliberately does NOT rely on it; this records that it stays late.
    bot._cap_stage = None
    bot._cap_reconstructed_segment = lambda _sym, stage=None: setattr(bot, "_cap_stage", stage)
    # The session calendar is stubbed to a SET OF ET DATES on which the market traded. The real
    # helper derives the same thing from the DB; only the source is stubbed, not the arithmetic.
    cal = sessions if sessions is not None else set()

    def _missed(_session, older, newer):
        older_d = older.astimezone(UTC).date()
        newer_d = newer.astimezone(UTC).date()
        return max(0, len({d for d in cal if older_d < d < newer_d}))

    bot._missed_sessions_between = _missed
    # ⛔⭐⭐ THE BOUNDARY CALENDAR (P10). Stubbed EXPLICITLY, from the same date set, because the
    # real helper reads the wall clock — and if it is left unstubbed the fixture's fake session
    # object makes it raise, which its own except-branch turns into 0. The tests would then pass
    # for the wrong reason: "no boundary gap" rather than "the boundary gap was handled".
    _today = today if today is not None else date(2026, 8, 18)

    def _missed_before_today(_session, newest_bar):
        newest_d = newest_bar.astimezone(UTC).date()
        return max(0, len({d for d in cal if newest_d < d < _today}))

    bot._missed_sessions_before_today = _missed_before_today
    bot._seed_strategy_bars_from_db("TEST")
    return bot.strategy, bot


def _series(newest: datetime, n: int, step_min: int = 1, close: float = 1.0) -> list[_Row]:
    return [_Row(newest - timedelta(minutes=step_min * i), close) for i in range(n)]


# ------------------------------------------------------------------ the live incident
def test_CAST_shape_the_june_bars_never_reach_the_strategy() -> None:
    """⛔⭐⭐ THE 08-18 INCIDENT. 38 bars today, a 61-day hole, then June."""
    today = datetime(2026, 8, 18, 11, 0, tzinfo=UTC)
    june = datetime(2026, 6, 18, 18, 49, tzinfo=UTC)
    strat, bot = _seed(
        _series(today, 38) + _series(june, 212),
        sessions={date(2026, 7, 1), date(2026, 7, 15), date(2026, 8, 3)},
    )
    assert len(strat.seen) == 38, "only today's contiguous run may reach the strategy"
    assert bot._db_seed_gap_truncations == 1, "the refusal must be COUNTED, not silent"


def test_the_dropped_side_is_what_produced_the_6_7x_error() -> None:
    """Pins WHY: the far side is a different regime, not merely older."""
    today = datetime(2026, 8, 18, 11, 0, tzinfo=UTC)
    june = datetime(2026, 6, 18, 18, 49, tzinfo=UTC)
    strat, _ = _seed(
        _series(today, 38, close=1.21) + _series(june, 212, close=7.99), sessions={date(2026, 7, 1)}
    )
    assert {b.close for b in strat.seen} == {1.21}, "a 7.99-priced June bar reached the strategy"


def test_the_seeded_bars_arrive_OLDEST_FIRST() -> None:
    """Truncation must not disturb the replay order the indicators depend on."""
    today = datetime(2026, 8, 18, 11, 0, tzinfo=UTC)
    strat, _ = _seed(_series(today, 20))
    assert strat.seen == sorted(strat.seen, key=lambda b: b.timestamp_ms)


# ------------------------------------------------------------------ the controls that MUST pass
def test_SHORT_history_is_short_NOT_holed() -> None:
    """⛔⭐ THE REGRESSION DIRECTION. AIXC (74 bars) and NTWOW (160) had history starting TODAY.
    Truncation must be a no-op — treating short as holed would blind every fresh symbol."""
    strat, bot = _seed(_series(datetime(2026, 8, 18, 11, 0, tzinfo=UTC), 74))
    assert len(strat.seen) == 74
    assert bot._db_seed_gap_truncations == 0, "a short series must not be counted as a truncation"


def test_a_normal_OVERNIGHT_gap_still_seeds_in_full() -> None:
    """Consecutive sessions: no session falls BETWEEN them, so nothing is missed."""
    today = datetime(2026, 8, 18, 11, 0, tzinfo=UTC)
    strat, _ = _seed(_series(today, 60) + _series(today - timedelta(hours=13), 190), sessions=set())
    assert len(strat.seen) == 250


def test_ONE_MISSED_SESSION_TRUNCATES_even_though_it_is_only_a_day() -> None:
    """⛔⭐ THE CASE THE 4-DAY THRESHOLD MISSED ENTIRELY. A single skipped trading day carries a
    26.2% median discontinuity — as dangerous as a 60-day hole, and it used to pass."""
    wed = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)
    mon = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)
    strat, bot = _seed(_series(wed, 40) + _series(mon, 210), sessions={date(2026, 8, 18)})
    assert len(strat.seen) == 40, "a skipped session must truncate regardless of wall-clock"
    assert bot._db_seed_gap_truncations == 1


def test_the_threshold_is_ZERO_MISSED_SESSIONS_and_that_is_where_the_void_is() -> None:
    """⛔⭐⭐ THE MEASURED VOID (256k gaps in strategy_bar_history, 2026-08-18):
        same session          255,243 gaps   median move  0.7%
        0 missed (a CLOSURE)      345 gaps   median move 10.2%  <- legitimate, must be seeded
        1 missed                   75 gaps   median move 26.2%  <- 2.6x jump
        2..10 missed             ~190 gaps   median move 16-32% <- FLAT, no further structure
    ⛔ Wall-clock was the WRONG variable: the earlier 4-DAY threshold let 110 gaps through whose
    median discontinuity was 18%, and a PRICE cut would truncate every Monday because penny-stock
    weekend gaps genuinely run 10-18%. Only 'missed sessions' separates a CLOSURE from an ABSENCE."""
    assert DB_SEED_MAX_MISSED_SESSIONS == 0


def test_a_WEEKEND_is_a_CLOSURE_and_must_still_seed_in_full() -> None:
    """⛔⭐ THE CONTROL THE DAY-BASED DESIGN COULD NOT EXPRESS. Fri -> Mon is ~65h of wall clock but
    ZERO missed sessions. A duration cut or a price cut truncates every Monday; a session cut must
    not — and MACD continuity across a closure is deliberate (see the seed docstring)."""
    mon = datetime(2026, 8, 17, 11, 0, tzinfo=UTC)
    fri = datetime(2026, 8, 14, 15, 0, tzinfo=UTC)
    strat, bot = _seed(_series(mon, 60) + _series(fri, 190), sessions=set())
    assert len(strat.seen) == 250, "a weekend closure must not truncate"
    assert bot._db_seed_gap_truncations == 0


def test_truncation_stops_at_the_FIRST_gap_not_the_widest() -> None:
    """Everything beyond the first wide jump is a different regime, regardless of later spacing."""
    now = datetime(2026, 8, 18, 11, 0, tzinfo=UTC)
    rows = (
        _series(now, 10)
        + _series(now - timedelta(days=30), 10)
        + _series(now - timedelta(days=31), 10)
    )
    strat, _ = _seed(rows, sessions={date(2026, 8, 5), date(2026, 7, 25)})
    assert len(strat.seen) == 10


def test_an_empty_series_is_not_an_error() -> None:
    strat, bot = _seed([])
    assert strat.seen == [] and bot._db_seed_gap_truncations == 0


# ------------------------------------------------------- the REAL helper, not a reimplementation
# ⛔⭐⭐ The tests above stub `_missed_sessions_between` so they can drive the truncation walk.
# That stub is a REIMPLEMENTATION, and it let two mutants live: an off-by-one that counts the
# endpoints (a WEEKEND would truncate) and a fail-CLOSED calendar error. These exercise the SHIPPED
# helper with only the DB stubbed, which is what actually pins the arithmetic.
class _CalSession:
    """Returns the raw DISTINCT-date count the real SQL would return."""

    def __init__(self, distinct_dates: int | None, raises: bool = False):
        self._n, self._raises = distinct_dates, raises

    def execute(self, *_a, **_k):
        if self._raises:
            raise RuntimeError("calendar read failed")
        return SimpleNamespace(scalar=lambda: self._n)


def _real_missed(distinct_dates: int | None, raises: bool = False) -> int:
    bot = object.__new__(SchwabV2BotService)
    return bot._missed_sessions_between(
        _CalSession(distinct_dates, raises),
        datetime(2026, 8, 14, 15, 0, tzinfo=UTC),
        datetime(2026, 8, 17, 11, 0, tzinfo=UTC),
    )


def test_REAL_HELPER_a_weekend_counts_ZERO_missed_sessions() -> None:
    """⛔⭐⭐ THE OFF-BY-ONE THAT WOULD TRUNCATE EVERY MONDAY.

    The SQL counts DISTINCT session dates in the OPEN interval (bar_time > lo AND < hi). Fri->Mon
    contains no trading date at all, so the raw count is 0 and the answer must be 0 — not -1, and
    certainly not 1. A mutant that forgets `- 1` reports 1 here and truncates every weekend.
    """
    assert _real_missed(0) == 0


def test_REAL_HELPER_consecutive_sessions_count_ZERO() -> None:
    """Mon->Tue: nothing falls strictly between, so nothing is missed."""
    assert _real_missed(0) == 0


def test_REAL_HELPER_one_skipped_session_counts_ONE() -> None:
    """⛔ The endpoints' own sessions must not inflate the count. Two dates present in the interval
    means ONE session was genuinely skipped once the boundary is discounted."""
    assert _real_missed(2) == 1


def test_REAL_HELPER_a_long_absence_counts_many() -> None:
    assert _real_missed(44) == 43


def test_REAL_HELPER_a_failed_calendar_read_FAILS_OPEN() -> None:
    """⛔⭐⭐ DIRECTION OF BIAS. A DB error must return 0 — 'no sessions missed' — so the seed behaves
    exactly as it did before the fix. Failing CLOSED would let one transient DB blip silently
    truncate real history on every symbol, which is a worse defect than the one being fixed.
    Same shape as 'no quote => no opinion'."""
    assert _real_missed(None, raises=True) == 0


def test_REAL_HELPER_a_null_count_is_treated_as_zero() -> None:
    assert _real_missed(None) == 0


# ==================================================================================================
# P10 — THE BOUNDARY GAP (2026-08-19). #721 only compared ADJACENT LOADED BARS, so a wholly-stale
# but internally-contiguous history seeded IN FULL, with no truncation and no log line.
# Measured that day: 178 symbols in exactly that state, 600-780 bars each, 35-62 days stale.
# ==================================================================================================


def test_VRAX_shape_a_wholly_stale_contiguous_window_seeds_NOTHING() -> None:
    """⛔⭐⭐ THE DEFECT. VRAX: 241 contiguous bars from 07-09 and nothing since.

    There is no INTERNAL gap to find, so the pre-P10 walk kept all 241 — a series where VRAX traded
    5.92-12.85, replayed on a day it traded 3.22-4.07.
    """
    july = datetime(2026, 7, 9, 15, 47, tzinfo=UTC)
    strat, bot = _seed(
        _series(july, 241),
        sessions={date(2026, 7, 20), date(2026, 8, 3), date(2026, 8, 17)},
        today=date(2026, 8, 19),
    )
    assert strat.seen == [], "a wholly-stale window has no contiguous tail to keep"
    assert bot._db_seed_gap_truncations == 1, "the refusal must be COUNTED, not silent"


def test_a_normal_symbol_with_bars_today_is_UNTOUCHED() -> None:
    """⛔ The other direction, and the one that must not regress: a current series seeds in full."""
    now = datetime(2026, 8, 19, 13, 0, tzinfo=UTC)
    strat, bot = _seed(_series(now, 120), sessions=set(), today=date(2026, 8, 19))
    assert len(strat.seen) == 120
    assert bot._db_seed_gap_truncations == 0


def test_newest_bar_YESTERDAY_is_a_CLOSURE_and_seeds_across() -> None:
    """⛔⭐⭐ THE REGRESSION THE NAIVE IMPLEMENTATION WOULD CAUSE.

    Reusing `_missed_sessions_between(newest, now)` would score 1 here — its -1 assumes BOTH
    endpoints are bars — and wipe the history of every symbol pre-open. An overnight is a CLOSURE.
    """
    yesterday = datetime(2026, 8, 18, 19, 59, tzinfo=UTC)
    strat, bot = _seed(_series(yesterday, 200), sessions=set(), today=date(2026, 8, 19))
    assert len(strat.seen) == 200, "an overnight gap must be seeded ACROSS, never truncated"
    assert bot._db_seed_gap_truncations == 0


def test_a_weekend_is_a_CLOSURE_not_an_ABSENCE() -> None:
    """Friday close -> Monday: no trading session falls inside, so nothing is dropped."""
    friday = datetime(2026, 8, 14, 19, 59, tzinfo=UTC)
    strat, bot = _seed(_series(friday, 150), sessions=set(), today=date(2026, 8, 17))
    assert len(strat.seen) == 150
    assert bot._db_seed_gap_truncations == 0


def test_one_missed_session_before_today_truncates_everything() -> None:
    """The threshold is the SAME constant as the internal check."""
    older = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    strat, bot = _seed(_series(older, 100), sessions={date(2026, 8, 17)}, today=date(2026, 8, 19))
    assert DB_SEED_MAX_MISSED_SESSIONS == 0
    assert strat.seen == []
    assert bot._db_seed_gap_truncations == 1


def test_the_boundary_refusal_does_not_double_count_the_census() -> None:
    """One truncation event, one increment — the census denominator must stay readable."""
    july = datetime(2026, 7, 9, tzinfo=UTC)
    _, bot = _seed(_series(july, 250), sessions={date(2026, 8, 3)}, today=date(2026, 8, 19))
    assert bot._db_seed_gap_truncations == 1


def test_an_empty_series_is_not_a_boundary_truncation() -> None:
    """No rows means nothing to seed and nothing to refuse — it must not inflate the census."""
    _, bot = _seed([], sessions={date(2026, 8, 3)}, today=date(2026, 8, 19))
    assert bot._db_seed_gap_truncations == 0
