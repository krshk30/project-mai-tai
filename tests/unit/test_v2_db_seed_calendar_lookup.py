"""The seed-gap calendar lookup must be CHEAP, must not CASCADE, and must be COUNTABLE.

⛔⭐⭐ ALL THREE FAILED LIVE ON 2026-08-20, and every one of them failed SILENTLY.

`#734` shipped the boundary check on 08-19 and the 04:00 roll the next morning was its first real
exercise. It worked — `SGLY dropped ALL 86 seed bars` is the new class catching a real name. But
the same tape carried three defects that no test could have caught, because none existed:

  1. **The lookup TIMED OUT.** `_missed_sessions_before_today` filtered on
     `(bar_time AT TIME ZONE 'America/New_York')::date`, which wraps the indexed column in an
     expression. Postgres cannot use an expression as an index CONDITION, so it degraded to a
     post-index FILTER over every row the strategy owns — `Rows Removed by Filter: 257621`,
     `Heap Fetches: 112449`, **1603 ms warm** against the **5 s** `statement_timeout` the "fast"
     session profile sets. Cold, it cleared the timeout and the fix went INERT, fail-open, for
     every symbol it touched.
     ⛔ Fail-open is the correct BIAS and the wrong OUTCOME to leave unmeasured: the log said
     "seeding unchanged", which is precisely the pre-#734 behaviour that seeded weeks-stale bars.

  2. **One timeout became four failures.** A cancelled statement leaves the transaction ABORTED;
     every later statement on it raises `InFailedSqlTransaction`. The seed walk re-uses the same
     session per gap, so the boundary timeout at 21:57:04.372 turned the three gap lookups at
     .391/.394/.397 into "failures" too. Read as four slow queries, it is one slow query and three
     refusals — and those two diagnoses point at different code.

  3. **The census denominator was the WRONG SET.** It printed `len(self._db_seeded)`, a DEDUP set
     pruned to the live watchlist every selection pass (`self._db_seeded &= selected`). The
     numerator is monotonic since boot; the denominator shrinks. At the 04:00 roll it reached
     EMPTY and the line printed **`truncations=7 of 0 symbols seeded since boot`** — a numerator
     with no denominator, on the one line whose stated purpose is to supply the denominator.

⛔⭐ The tell in all three was the same: **a number that did not reconcile.** 7 of 0.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from project_mai_tai.services.schwab_1m_v2_bot import (
    EASTERN_TZ,
    INTERVAL_SECS,
    STRATEGY_CODE,
    SchwabV2BotService,
)


class _CapturingSession:
    """Records the SQL and params of every execute; optionally fails the first N calls."""

    def __init__(self, result=0, fail_first: int = 0, exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.rollbacks = 0
        self.closed = False
        self._result = result
        self._fail_first = fail_first
        self._exc = exc or RuntimeError("canceling statement due to statement timeout")

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), dict(params or {})))
        if self._fail_first > 0:
            self._fail_first -= 1
            raise self._exc
        return SimpleNamespace(scalar=lambda: self._result)

    def rollback(self) -> None:
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.closed = True
        return False


def _bot() -> SchwabV2BotService:
    bot = object.__new__(SchwabV2BotService)
    bot._db_seed_gap_truncations = 0
    bot._db_seed_evaluations = 0
    return bot


# ------------------------------------------------------------------ 1. the timeout / sargability
def test_the_boundary_bounds_are_TIMESTAMPS_not_ET_DATES() -> None:
    """⛔⭐⭐ THE FIX, PINNED AT ITS CAUSE. Date-expression bounds are unindexable.

    This asserts the PARAMETER TYPES, not the plan: the moment `lo`/`hi` go back to `date`
    objects, the WHERE clause has to wrap `bar_time` in `AT TIME ZONE ...::date` to compare
    against them, and the index condition is lost again. Types are the thing a revert changes.
    """
    bot = _bot()
    sess = _CapturingSession(result=0)
    newest = datetime(2026, 8, 18, 15, 32, tzinfo=UTC)
    bot._missed_sessions_before_today(sess, newest)

    assert sess.calls, "the lookup must actually query"
    sql, params = sess.calls[-1]
    assert isinstance(params["lo"], datetime), "a date bound cannot use the bar_time index"
    assert isinstance(params["hi"], datetime), "a date bound cannot use the bar_time index"
    assert params["sc"] == STRATEGY_CODE and params["iv"] == INTERVAL_SECS


def test_the_WHERE_clause_compares_bar_time_RAW() -> None:
    """⛔⭐ The sargability invariant itself. 1603 ms -> 32.7 ms rested on exactly this.

    Measured on the box 2026-08-20: with the expression predicate the planner reported
    `Rows Removed by Filter: 257621`; with the raw comparison the same two dates became an
    Index Cond and the query returned the SAME answer for gaps of 0, 1, 2, 3 and 35 sessions.
    """
    bot = _bot()
    sess = _CapturingSession(result=0)
    bot._missed_sessions_before_today(sess, datetime(2026, 8, 18, 15, 32, tzinfo=UTC))
    sql = sess.calls[-1][0]
    where = sql.split("WHERE", 1)[1]
    assert "bar_time >= :lo" in where and "bar_time < :hi" in where
    assert "::date > :lo" not in where and "::date < :hi" not in where, (
        "an expression predicate on bar_time is a FILTER, never an index CONDITION"
    )


def test_the_bounds_are_ET_MIDNIGHTS_spanning_the_dates_strictly_between() -> None:
    """The rewrite must be EXACTLY equivalent, not merely faster.

    ET dates strictly between `newest` and today  <=>  instants in
    [ET-midnight(newest_date + 1), ET-midnight(today)).
    """
    bot = _bot()
    sess = _CapturingSession(result=0)
    newest = datetime(2026, 8, 18, 15, 32, tzinfo=UTC)  # 11:32 ET on 08-18
    bot._missed_sessions_before_today(sess, newest)
    _, params = sess.calls[-1]

    assert params["lo"] == datetime(2026, 8, 19, 0, 0, tzinfo=EASTERN_TZ)
    today_et = datetime.now(UTC).astimezone(EASTERN_TZ).date()
    assert params["hi"] == datetime(today_et.year, today_et.month, today_et.day, tzinfo=EASTERN_TZ)


def test_the_ET_midnight_bound_follows_DST_it_is_not_a_fixed_offset() -> None:
    """⛔ A fixed -4h/-5h would silently shift the boundary by an hour half the year.

    Built through EASTERN_TZ, a January midnight is -05:00 and an August midnight is -04:00.
    An hour's drift moves whole bars across the boundary at exactly 00:00 ET.
    """
    bot = _bot()
    winter, summer = _CapturingSession(result=0), _CapturingSession(result=0)
    bot._missed_sessions_before_today(winter, datetime(2026, 1, 14, 15, 0, tzinfo=UTC))
    bot._missed_sessions_before_today(summer, datetime(2026, 8, 14, 15, 0, tzinfo=UTC))

    assert winter.calls[-1][1]["lo"].utcoffset() == timedelta(hours=-5)
    assert summer.calls[-1][1]["lo"].utcoffset() == timedelta(hours=-4)


def test_a_newest_bar_of_TODAY_costs_no_query_at_all() -> None:
    """The common case is the empty range — it must short-circuit, not scan.

    Nearly every symbol on a live watchlist has a bar from today. `lo >= hi` there, and a query
    that can only return 0 is a query worth not sending.
    """
    bot = _bot()
    sess = _CapturingSession(result=99)  # would be a WRONG non-zero answer if it ever ran
    now_et = datetime.now(UTC)
    assert bot._missed_sessions_before_today(sess, now_et) == 0
    assert sess.calls == [], "an empty range must not reach the database"


# ------------------------------------------------------------------ 2. the cascade
def test_a_TIMED_OUT_lookup_ROLLS_BACK_so_the_next_one_is_its_own_verdict() -> None:
    """⛔⭐⭐ THE CASCADE. One aborted transaction made every later lookup 'fail' too."""
    bot = _bot()
    sess = _CapturingSession(result=3, fail_first=1)

    assert bot._missed_sessions_before_today(sess, datetime(2026, 7, 1, tzinfo=UTC)) == 0
    assert sess.rollbacks == 1, "an aborted transaction must be cleared, or it poisons the session"

    # The SECOND lookup, on the SAME session, must now get its own real answer.
    assert bot._missed_sessions_between(sess, datetime(2026, 7, 1, tzinfo=UTC),
                                        datetime(2026, 8, 1, tzinfo=UTC)) == 2  # 3 dates - 1


def test_the_sibling_lookup_also_rolls_back() -> None:
    """Both helpers share the session, so both must leave it usable."""
    bot = _bot()
    sess = _CapturingSession(fail_first=1)
    assert bot._missed_sessions_between(sess, datetime(2026, 7, 1, tzinfo=UTC),
                                        datetime(2026, 8, 1, tzinfo=UTC)) == 0
    assert sess.rollbacks == 1


def test_a_failed_lookup_still_biases_towards_SEEDING_not_dropping() -> None:
    """⛔ The direction of the bias is the whole safety argument — pin it, do not assume it.

    A calendar we cannot read must never be read as "history is stale". 0 means
    "no sessions missed" means "seed unchanged".
    """
    bot = _bot()
    assert bot._missed_sessions_before_today(_CapturingSession(fail_first=1),
                                             datetime(2026, 6, 1, tzinfo=UTC)) == 0
    assert bot._missed_sessions_between(_CapturingSession(fail_first=1),
                                        datetime(2026, 6, 1, tzinfo=UTC),
                                        datetime(2026, 8, 1, tzinfo=UTC)) == 0


def test_a_rollback_that_itself_fails_does_not_escape() -> None:
    """The rollback is best-effort. It must not convert a soft failure into a hard one."""
    bot = _bot()

    class _BadRollback(_CapturingSession):
        def rollback(self):
            raise RuntimeError("connection already gone")

    assert bot._missed_sessions_before_today(_BadRollback(fail_first=1),
                                             datetime(2026, 6, 1, tzinfo=UTC)) == 0


# ------------------------------------------------------------------ 3. the census denominator
class _Row:
    def __init__(self, bar_time: datetime) -> None:
        self.bar_time = bar_time
        self.open_price = self.high_price = self.low_price = self.close_price = 1.0
        self.volume = 100


class _SeedSession:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.closed = True
        return False

    def execute(self, *_a, **_k):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self._rows))

    def rollback(self):
        pass


def _seed_bot(rows):
    bot = object.__new__(SchwabV2BotService)
    bot.session_factory = lambda: _SeedSession(rows)
    bot._db_seeded = set()
    bot._db_seed_gap_truncations = 0
    bot._db_seed_evaluations = 0
    bot.strategy = SimpleNamespace(
        on_bar=lambda *_a: None,
        watchlist_state=lambda _s: SimpleNamespace(
            pending_path_macd=False, pending_path_vwap=False, pending_cross_bar_ts_ms=0, bars=[]
        ),
    )
    bot._cap_reconstructed_segment = lambda _sym, stage=None: None
    bot._missed_sessions_between = lambda *_a: 0
    bot._missed_sessions_before_today = lambda *_a: 0
    return bot


def test_the_denominator_SURVIVES_a_watchlist_prune() -> None:
    """⛔⭐⭐ THE `7 of 0` SHAPE, reproduced exactly.

    `_db_seeded` is intersected with the live watchlist on every selection pass. Any census that
    reads its length reports the CURRENT watchlist, never the population measured since boot.
    """
    now = datetime.now(UTC)
    bot = _seed_bot([_Row(now - timedelta(minutes=i)) for i in range(20)])

    bot._seed_strategy_bars_from_db("AAA")
    bot._seed_strategy_bars_from_db("BBB")
    assert bot._db_seed_evaluations == 2

    # The 04:00 roll: the watchlist turns over completely.
    bot._db_seeded &= set()
    assert bot._db_seeded == set(), "the fixture must reproduce the real prune"
    assert bot._db_seed_evaluations == 2, (
        "the census denominator must be MONOTONIC — pruning the dedup set is not un-seeding"
    )


def test_truncations_can_never_exceed_evaluations() -> None:
    """⛔⭐ The invariant that makes the ratio readable: both halves count the same unit.

    `7 of 0` was unreadable because the two halves counted different things. A numerator that can
    outrun its denominator is not a ratio.
    """
    now = datetime.now(UTC)
    bot = _seed_bot([_Row(now - timedelta(minutes=i)) for i in range(10)])
    bot._missed_sessions_before_today = lambda *_a: 5  # every seed truncates

    for sym in ("AAA", "BBB", "CCC"):
        bot._seed_strategy_bars_from_db(sym)

    assert bot._db_seed_gap_truncations == 3
    assert bot._db_seed_evaluations == 3
    assert bot._db_seed_gap_truncations <= bot._db_seed_evaluations


def test_an_EMPTY_history_is_a_NON_EVENT_in_both_halves() -> None:
    """A symbol with no stored bars was never evaluated — it must not inflate the denominator."""
    bot = _seed_bot([])
    bot._seed_strategy_bars_from_db("AAA")
    assert bot._db_seed_evaluations == 0
    assert bot._db_seed_gap_truncations == 0


# ------------------------------------------------------------------ 4. the session scope
def test_the_calendar_LOOKUPS_RUN_INSIDE_THE_OPEN_SESSION() -> None:
    """⛔⭐⭐ They used to run against a session the `with` block had already CLOSED.

    SQLAlchemy hides this — a closed Session re-opens a connection and begins a fresh transaction
    on next use — so it "worked" while leaking one uncommitted transaction per seeded symbol, and
    put the two lookups outside the transaction they are supposed to share with the seed read.
    """
    now = datetime.now(UTC)
    seen: list[bool] = []
    bot = _seed_bot([_Row(now - timedelta(minutes=i)) for i in range(10)])

    def _record(session, *_a):
        seen.append(session.closed)
        return 0

    bot._missed_sessions_before_today = _record
    bot._seed_strategy_bars_from_db("AAA")

    assert seen == [False], "the calendar lookup ran against a CLOSED session"


@pytest.mark.parametrize("helper", ["_missed_sessions_before_today", "_missed_sessions_between"])
def test_both_helpers_receive_the_SAME_session_object_the_seed_read_used(helper: str) -> None:
    """One transaction per seed, not three."""
    now = datetime.now(UTC)
    rows = [_Row(now - timedelta(minutes=i)) for i in range(5)]
    rows += [_Row(now - timedelta(days=40) - timedelta(minutes=i)) for i in range(5)]
    got: list[object] = []
    bot = _seed_bot(rows)
    setattr(bot, helper, lambda session, *_a: (got.append(session), 0)[1])
    bot._seed_strategy_bars_from_db("AAA")
    assert got, f"{helper} was never called"
    assert all(s is got[0] for s in got)


# ------------------------------------------------------------------ 5. the census LINE itself
def test_the_CENSUS_LINE_prints_the_monotonic_counter_not_the_pruned_set(caplog) -> None:
    """⛔⭐⭐ THE MUTANT THIS EXISTS TO KILL: reverting the line to `len(self._db_seeded)`.

    Counting the evaluations is only half the fix — the census has to PRINT them. Built through
    the real constructor so the emitting path is production's, and the dedup set is left EMPTY:
    that is the state the 04:00 roll produces, and it is what turned `7` into `7 of 0`.
    """
    from project_mai_tai.settings import Settings

    bot = SchwabV2BotService(
        Settings(
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
            strategy_schwab_1m_v2_session_time_roll_enabled=True,
        ),
        session_factory=None,
    )
    bot._db_seed_gap_truncations = 7
    bot._db_seed_evaluations = 12
    bot._db_seeded = set()  # the post-roll prune — the exact live state on 2026-08-20

    with caplog.at_level(logging.INFO):
        bot._roll_stale_session_state({}, {})

    census = [r.getMessage() for r in caplog.records if "SEED-GAP-CENSUS" in r.getMessage()]
    assert census, "the census must be emitted on the boundary crossing"
    assert "truncations=7 of 12" in census[0]
    assert "7 of 0" not in census[0], "the census read the watchlist-pruned dedup set again"
