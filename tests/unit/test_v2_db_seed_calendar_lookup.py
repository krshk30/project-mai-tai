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
    DB_SEED_MAX_MISSED_SESSIONS,
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

    # The SECOND lookup, on the SAME session, must now get its own real answer. At the configured
    # zero threshold the return is deliberately saturated: truthy means at least one session.
    assert bot._missed_sessions_between(sess, datetime(2026, 7, 1, tzinfo=UTC),
                                        datetime(2026, 8, 1, tzinfo=UTC)) == 1


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


# ------------------------------------------------ 2b. the internal-gap decision is ONE BIT (08-25)
def test_the_internal_gap_lookup_asks_for_ONE_BIT_not_a_cardinality() -> None:
    """The plain gap lookup was untouched by #765 and failed open at 16:34 ET on 08-25."""
    bot = _bot()
    sess = _CapturingSession(result=True)
    older = datetime(2026, 8, 17, 19, 59, tzinfo=UTC)  # Mon 15:59 ET
    newer = datetime(2026, 8, 19, 13, 30, tzinfo=UTC)  # Wed 09:30 ET

    assert bot._missed_sessions_between(sess, older, newer) == 1
    sql, params = sess.calls[-1]
    assert "EXISTS (SELECT 1" in sql
    assert "count(DISTINCT" not in sql
    assert "bar_time >= :lo" in sql and "bar_time < :hi" in sql
    assert params["lo"] == datetime(2026, 8, 18, 0, 0, tzinfo=EASTERN_TZ)
    assert params["hi"] == datetime(2026, 8, 19, 0, 0, tzinfo=EASTERN_TZ)


def test_the_internal_EXISTS_matches_the_old_answer_on_a_known_window() -> None:
    """Known Mon->Wed window: Tuesday is the one intervening session in both formulations.

    This control computes the PRIOR query literally: exact timestamp bounds, distinct ET dates,
    then ``max(0, count - 1)``.  It must not reuse the rewrite's bounds, because then a boundary
    defect could certify itself.  Monday has a bar after the older endpoint, so the prior query
    sees Monday + Tuesday and returns one; the rewrite sees Tuesday and also returns one.
    """
    older = datetime(2026, 8, 17, 19, 59, tzinfo=UTC)  # Mon 15:59 ET
    newer = datetime(2026, 8, 19, 13, 30, tzinfo=UTC)  # Wed 09:30 ET
    bars = [
        datetime(2026, 8, 17, 20, 0, tzinfo=UTC),  # older endpoint's date, prior count's offset
        datetime(2026, 8, 18, 13, 30, tzinfo=UTC),
        datetime(2026, 8, 18, 15, 0, tzinfo=UTC),
        datetime(2026, 8, 19, 13, 30, tzinfo=UTC),  # endpoint date: outside the open window
    ]

    class _KnownWindowSession(_CapturingSession):
        def execute(self, stmt, params=None):
            params = dict(params or {})
            self.calls.append((str(stmt), params))
            matched = [bar for bar in bars if params["lo"] <= bar < params["hi"]]
            return SimpleNamespace(scalar=lambda: bool(matched))

    sess = _KnownWindowSession()
    new_answer = _bot()._missed_sessions_between(sess, older, newer)
    _, params = sess.calls[-1]
    prior_distinct_dates = len(
        {
            bar.astimezone(EASTERN_TZ).date()
            for bar in bars
            if older < bar < newer
        }
    )
    prior_answer = max(0, prior_distinct_dates - 1)
    assert prior_distinct_dates == 2, "the prior query must see Monday and Tuesday"
    assert prior_answer == 1
    assert (prior_answer > DB_SEED_MAX_MISSED_SESSIONS) == (
        new_answer > DB_SEED_MAX_MISSED_SESSIONS
    )

    # Mutation control: moving the new lower boundary forward one date excludes Tuesday and
    # reverses the verdict.  A production mutant from +1 day to +2 days therefore turns the
    # equivalence assertion above red instead of letting the fixture pass vacuously.
    mutated_lo = params["lo"] + timedelta(days=1)
    mutated_found = any(mutated_lo <= bar < params["hi"] for bar in bars)
    assert mutated_found is False
    assert (prior_answer > DB_SEED_MAX_MISSED_SESSIONS) != mutated_found


def test_internal_EXISTS_is_guarded_by_the_zero_threshold(monkeypatch) -> None:
    """Above zero, EXISTS cannot answer the caller's question; exact counting must return."""
    import project_mai_tai.services.schwab_1m_v2_bot as mod

    assert mod.DB_SEED_MAX_MISSED_SESSIONS == 0
    monkeypatch.setattr(mod, "DB_SEED_MAX_MISSED_SESSIONS", 2)
    sess = _CapturingSession(result=7)
    got = _bot()._missed_sessions_between(
        sess,
        datetime(2026, 7, 1, tzinfo=UTC),
        datetime(2026, 8, 1, tzinfo=UTC),
    )
    sql = sess.calls[-1][0]
    assert "count(DISTINCT" in sql and "EXISTS (SELECT 1" not in sql
    assert got == 7


def test_internal_EXISTS_keeps_closures_and_failures_fail_open() -> None:
    bot = _bot()
    # Same ET date is an empty interval and must not query.
    same_day = _CapturingSession(result=True)
    assert bot._missed_sessions_between(
        same_day,
        datetime(2026, 8, 18, 14, 0, tzinfo=UTC),
        datetime(2026, 8, 18, 20, 0, tzinfo=UTC),
    ) == 0
    assert same_day.calls == []

    # Fri->Mon has no intervening trading session; the database's false answer preserves it.
    weekend = _CapturingSession(result=False)
    assert bot._missed_sessions_between(
        weekend,
        datetime(2026, 8, 14, 19, 59, tzinfo=UTC),
        datetime(2026, 8, 17, 13, 30, tzinfo=UTC),
    ) == 0

    failed = _CapturingSession(result=True, fail_first=1)
    assert bot._missed_sessions_between(
        failed,
        datetime(2026, 8, 17, 19, 59, tzinfo=UTC),
        datetime(2026, 8, 19, 13, 30, tzinfo=UTC),
    ) == 0
    assert failed.rollbacks == 1


def test_internal_refusal_does_not_claim_a_cardinality_it_no_longer_has(caplog) -> None:
    bot = _bot()
    bot._missed_sessions_before_today = lambda *_a: 0
    bot._missed_sessions_between = lambda *_a: 1
    newer = _Row(datetime(2026, 8, 19, 13, 30, tzinfo=UTC))
    older = _Row(datetime(2026, 8, 17, 19, 59, tzinfo=UTC))

    with caplog.at_level(logging.WARNING):
        kept = bot._truncate_seed_rows_at_gap(_CapturingSession(), "TEST", [newer, older])

    assert kept == [newer]
    msg = "\n".join(record.getMessage() for record in caplog.records)
    assert "the series SKIPS at least one trading session" in msg, (
        "the return saturates at one, so the refusal may not print it as an exact count"
    )
    assert "the series SKIPS 1 trading" not in msg


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


# ---------------------------------------------------- 4. §256: the decision is ONE BIT (08-23)
#
# ⛔⭐⭐ THE GUARD TIMED OUT PRECISELY IN THE CASE IT EXISTS TO CATCH. `lo` is the day AFTER the
# newest stored bar, so the window width IS the staleness being measured. #743 made the predicate
# sargable and that held — the Index Cond is still there — but a sargable scan over an 83-day
# window is still 214,470 rows plus an external merge sort. Measured on the box 2026-08-23 on the
# exact window of both 08-21 failures (LSTA, 2026-05-30..2026-08-21):
#
#     count(DISTINCT ((bar_time AT TIME ZONE ...)::date))  →  3580 ms   (72% of the 5 s timeout,
#                                                                        on an IDLE Sunday box)
#     EXISTS (SELECT 1 ...)                                →  0.182 ms  (same Index Cond, 1 row)
#     SELECT DISTINCT ... LIMIT 1                          →   523 ms   (NOT a fix: HashAggregate
#                                                                        consumed all 214,470 rows)
#
# Same answer on that window: counted=56, exists=true. The 3.6 s bought one bit.

def test_the_boundary_lookup_asks_for_ONE_BIT_not_a_cardinality() -> None:
    """⛔⭐⭐ THE §256 FIX, PINNED AT ITS SHAPE — this is the test the mutant must turn red.

    The performance property IS the query shape, so the shape is what a test can hold. Restoring
    `count(DISTINCT ...)` here re-creates a 3580 ms statement that fail-opens under a 5 s timeout,
    and no fixture-sized dataset would ever reveal that. Assert the shape, name the cost.
    """
    bot = _bot()
    sess = _CapturingSession(result=True)
    bot._missed_sessions_before_today(sess, datetime(2026, 5, 29, 21, 1, tzinfo=UTC))

    sql = sess.calls[-1][0]
    assert "EXISTS (SELECT 1" in sql, (
        "the caller compares against DB_SEED_MAX_MISSED_SESSIONS == 0, so the question is "
        "existence; counting 214,470 rows to answer it is what timed out live on 08-21"
    )
    assert "count(DISTINCT" not in sql, (
        "a cardinality this caller never reads costs an external merge sort — 3580 ms measured, "
        "against a 5 s statement_timeout"
    )


def test_the_EXISTS_equivalence_is_CONDITIONAL_and_the_CODE_KNOWS_IT() -> None:
    """⛔⭐⭐ PIN THE ASSUMPTION, NOT A COMMENT ABOUT IT.

    `count(DISTINCT date) > 0` is `EXISTS` **only while the threshold is 0**. Raise the constant
    and "at least one" stops answering the question the caller asks. That is a live trap for a
    future edit, so it is a branch in the code and an assertion here — not a note someone reads.
    """
    import project_mai_tai.services.schwab_1m_v2_bot as mod

    assert mod.DB_SEED_MAX_MISSED_SESSIONS == 0, (
        "the EXISTS rewrite in _missed_sessions_before_today is EXACT only at 0 — if this "
        "constant moved, that branch must fall back to the counting form (it does; see below)"
    )


def test_raising_the_threshold_FALLS_BACK_to_the_exact_count(monkeypatch) -> None:
    """The fallback is what makes the fast path safe to have written at all."""
    import project_mai_tai.services.schwab_1m_v2_bot as mod

    monkeypatch.setattr(mod, "DB_SEED_MAX_MISSED_SESSIONS", 2)
    bot = _bot()
    sess = _CapturingSession(result=7)
    got = bot._missed_sessions_before_today(sess, datetime(2026, 5, 29, tzinfo=UTC))

    sql = sess.calls[-1][0]
    assert "count(DISTINCT" in sql, "above 0 the verdict needs the real cardinality"
    assert "EXISTS (SELECT 1" not in sql
    assert got == 7, "the counting branch must return the count, unsaturated"


def test_the_one_bit_answer_still_TRIPS_the_caller_threshold() -> None:
    """Saturating at 1 must not soften the verdict — 1 > 0 is still a refusal."""
    bot = _bot()
    assert bot._missed_sessions_before_today(
        _CapturingSession(result=True), datetime(2026, 5, 29, tzinfo=UTC)
    ) == 1
    assert bot._missed_sessions_before_today(
        _CapturingSession(result=False), datetime(2026, 5, 29, tzinfo=UTC)
    ) == 0


def test_the_EXISTS_branch_still_FAILS_OPEN_and_rolls_back() -> None:
    """⛔ The bias argument is unchanged by the rewrite, so re-pin it ON the new branch.

    A faster query that fails CLOSED would be a worse defect than the slow one it replaced.
    """
    bot = _bot()
    sess = _CapturingSession(result=True, fail_first=1)
    assert bot._missed_sessions_before_today(sess, datetime(2026, 5, 29, tzinfo=UTC)) == 0
    assert sess.rollbacks == 1


def test_the_refusal_message_reports_ET_DATES_not_a_session_count(caplog) -> None:
    """⛔⭐ THE NUMBER IS GONE, SO THE MESSAGE MUST NOT PRETEND TO HAVE IT.

    The old line printed `%d trading session(s)`. Fed a saturating 1 it would have said "1
    trading session" about a 56-session hole — a WRONG REASON, which is worse than a missing one.
    Two ET dates cost no extra query and say more than the count ever did.
    """
    bot = _bot()
    bot._missed_sessions_before_today = lambda *_a: 1
    bot._missed_sessions_between = lambda *_a: 0

    stale = datetime(2026, 5, 29, 21, 1, tzinfo=UTC)
    rows = [_Row(stale), _Row(stale - timedelta(minutes=1))]

    with caplog.at_level(logging.WARNING):
        kept = bot._truncate_seed_rows_at_gap(_CapturingSession(), "LSTA", rows)

    assert kept == [], "a wholly stale series has no contiguous tail to keep"
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "dropped ALL" in msg, (
        r"ops/health/collect_deploy_evidence.sh greps 'V2-DB-SEED-GAP\].*dropped ALL' — "
        "changing this substring silently zeroes signal 6a"
    )
    assert "2026-05-29" in msg, "the newest stored bar's ET date is the fact that matters"
    assert "trading session(s) before today" not in msg, (
        "the saturating return cannot support a session COUNT"
    )
    assert bot._db_seed_gap_truncations == 1
