"""§131 — SEED-EXPOSURE DETECTOR. Read-only. No restart, no deploy, no order flow.

Which symbols could `_seed_strategy_bars_from_db` hydrate from a series that reaches PAST today?

⛔⭐ THE CRITERION IS BAR COUNT AGAINST THE SEED LIMIT, NOT CLOCK TIME. A symbol is exposed while it
has FEWER than DB_SEED_BAR_LIMIT bars, because the seed then reaches back past today to make up the
count. The thinner the name the longer it stays exposed — and thin names are the entire universe
this strategy trades. CAST armed at 11:28 ET with ~38 bars; BIVI was still exposed at 07:11 ET with
11. A symbol can be exposed for most of a session.

⇒ Run PRE-OPEN and again at EVERY WATCHLIST ADD: a symbol joining mid-session has almost no bars at
  exactly that moment, which is the CAST case precisely.

## ⛔⭐⭐ B13 — THE LOG-LINE PATH IS RETIRED, AND MUST NOT COME BACK (2026-08-19)

The first version of this detector read the watchlist by grepping

    schwab_1m_v2 watchlist updated count=%d sample=%s warmed=%d

and the emitter builds that field as `",".join(sorted(selected)[:5])` — **`sample=` IS CAPPED AT
FIVE.** The detector therefore *could not see* a watchlist longer than five symbols, and would have
reported a clean bill on symbols it never read. On 2026-08-19 the watchlist happened to be 3-4 long,
so it read completely. That is LUCK, NOT A PASSING TEST.

It also read only the LAST such line, so it missed adds entirely: at 06:50 ET it showed
BIVI/TNON/ZNB while EHGO had been seeded and truncated at 06:46 ET and never appeared at all.

Same family as `journalctl -u <nonexistent>` (exit 0, no output), Schwab's saturating `maxResults`,
and `broker_order_events` having no account column: **a source that structurally cannot hold the
whole answer, returning a confident clean.** Note `redis-cli XLEN <nonexistent-key>` returns `0`
rather than an error and belongs to the same family — which is why `_load_watchlist` below refuses
an empty/absent stream instead of reading it as "no symbols".

⇒ The watchlist now comes from the bot's own published state, which carries `sorted(self._watchlist)`
  UNCAPPED (publisher verified in `schwab_1m_v2_bot.py`, not merely present in the schema).

## ⛔⭐⭐ THIS SCRIPT PROVES ITS OWN COVERAGE OR REFUSES

A detector that cannot state its denominator has the *same* defect one layer up. So it prints
`swept N of M` on every run and **exits 2 if N != M**, and exits 2 rather than guessing whenever it
cannot see: no stream, no parseable event, wrong strategy, or an event older than --max-age-seconds.

⛔ AN UNKNOWN MUST NEVER DECAY INTO A PASS. Exit 0 means "swept everything, found nothing"; it never
means "could not look". An EMPTY watchlist is reported as NOTHING TO SWEEP, never as "no exposure".

## ⛔⭐ IT PREDICTS; THE CENSUS CONFIRMS. THEY STAY INDEPENDENT.

This script deliberately DOES NOT reimplement `_missed_sessions_between`. Its flag is a plain
wall-clock heuristic on the age of the limit-th-newest bar, which is a DIFFERENT rule from the one
#721 enforces. That independence is the whole value: on 2026-08-19 the detector predicted FCUV,
HKIT and KIDZ were exposed, and a later `[V2-DB-SEED-GAP]` line is what would CONFIRM it. A detector
that shared the mechanism's logic would agree with it even when both were wrong.

Known and accepted consequence: a long holiday weekend (Fri -> Tue, 4.x days, ZERO missed sessions)
flags here but is correctly seeded across by #721. That is a FALSE POSITIVE, which is the safe
direction for a detector — the census is the adjudicator, not this script.

## ⛔⭐ COUNT WITH THE PREDICATE THE CONSUMER USES

The seed selects on `strategy_code` AND `interval_secs`, so this script does too. As of 2026-08-19
`schwab_1m_v2` has only `interval_secs=60`, so that half is not load-bearing *today* — it becomes so
the moment a second interval appears. `strategy_code` IS load-bearing right now: `polygon_30s` holds
925k rows in the same table against v2's 258k.

Exit codes:  0 = swept, no exposure  ·  1 = swept, EXPOSURE found  ·  2 = CANNOT SEE (refused)
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

ET = ZoneInfo("America/New_York")

STRATEGY_CODE = "schwab_1m_v2"
# Mirrors schwab_1m_v2_bot.DB_SEED_BAR_LIMIT / INTERVAL_SECS. Kept as literals so this script can run
# standalone on the box without importing the service; `--assert-constants` re-checks them against
# the source of truth so the mirror cannot drift silently.
SEED_LIMIT = 250
INTERVAL_SECS = 60

# The wall-clock heuristic — see "IT PREDICTS; THE CENSUS CONFIRMS" above. 4 days clears an ordinary
# weekend (Fri close -> Mon open is ~2.4 days plus overnight) without clearing a missed session.
EXPOSURE_AGE = timedelta(days=4)

# How stale a published bot-state event may be before this script refuses to trust it. The bot
# republishes on a short cadence; a minute of slack absorbs a slow tick without accepting a snapshot
# from a bot that died an hour ago.
DEFAULT_MAX_AGE_SECONDS = 90

# Bound the on-deck scan to names plausibly still tradeable, so it forecasts rather than dumping the
# historical universe. The printed list is capped but the COUNT never is — a silent truncation here
# would rebuild the very defect this script exists to kill.
ONDECK_LOOKBACK_DAYS = 30
ONDECK_PRINT_CAP = 25


class DetectorBlind(Exception):
    """Raised whenever the detector cannot see. Always becomes exit 2, never a verdict."""


@dataclass(frozen=True)
class BotState:
    """One published isolated-bot-state event, reduced to what the detector needs."""

    watchlist: list[str]  # UNCAPPED — see B13 above
    warm: frozenset[str]  # symbols the bot holds bar buffers for THIS session
    produced_at: datetime


@dataclass(frozen=True)
class SweepRow:
    """One symbol, described by THE WINDOW THE SEED WILL ACTUALLY TAKE.

    ⛔⭐⭐ REWRITTEN 2026-08-19 (P10). The first version classified on BAR COUNT, and the criterion
    was wrong in both directions. It is kept here as a warning, because I shipped it in #724:

        "SHORT history (< 250 bars ever) — short is NOT holed"

    That is FALSE IN GENERAL. AIXC (104 bars) and NTWOW (160) were safe because **nothing sat behind
    them** — their history started that day — not because they were short. A symbol with 241 bars
    from a single session 41 days ago is equally "short" and entirely stale. **SHORT IS NOT
    CONTIGUOUS, and bar count says nothing about either.**

    ⇒ Never infer safety from a count. Describe the window the seed will load and look for a gap in
      it — of which there are TWO kinds, and the old criterion could see neither reliably:

      INTERNAL  — a gap BETWEEN two adjacent loaded bars. `_seed_strategy_bars_from_db` truncates
                  at these (#721), so they are the covered case.
      BOUNDARY  — the gap between the NEWEST loaded bar and now. ⛔ #721 does NOT see this: its loop
                  only ever compares adjacent loaded bars, so a wholly-stale, internally-contiguous
                  history seeds IN FULL with no truncation and no log line. Measured 2026-08-19:
                  **178 symbols** in that state, some 35-62 days stale with 600-780 bars each.
    """

    symbol: str
    window_bars: int  # bars the seed would load = min(SEED_LIMIT, available)
    bars_ever: int  # informational ONLY — never a safety signal
    newest_bar: datetime | None
    max_internal_gap: timedelta | None  # largest gap between adjacent bars INSIDE that window

    def classify(self, now: datetime) -> tuple[bool, str]:
        """(exposed, human-readable reason). Boundary is checked FIRST — it is the uncovered kind."""
        if self.newest_bar is None or self.window_bars == 0:
            return False, "OK   no stored history — nothing to seed"

        boundary = now - self.newest_bar
        if boundary > EXPOSURE_AGE:
            stamp = self.newest_bar.astimezone(ET).strftime("%m-%d %H:%M")
            return True, (
                f"*** EXPOSED (BOUNDARY) *** newest bar {stamp} ET "
                f"({boundary.total_seconds() / 86400.0:.1f}d) — the WHOLE {self.window_bars}-bar "
                f"window is stale. ⛔ #721 does NOT truncate this: no internal gap to find."
            )
        if self.max_internal_gap is not None and self.max_internal_gap > EXPOSURE_AGE:
            return True, (
                f"*** EXPOSED (INTERNAL) *** {self.max_internal_gap.total_seconds() / 86400.0:.1f}d "
                f"gap inside the {self.window_bars}-bar window — #721 truncates at this."
            )
        return False, f"OK   {self.window_bars}-bar window is contiguous and current"


def parse_watchlist_event(raw: str | None, now: datetime, max_age_seconds: int) -> BotState:
    """Pull the UNCAPPED watchlist out of one published isolated-bot-state event.

    ⛔ Every failure path raises DetectorBlind. None of them may return an empty list, because an
    empty list is indistinguishable from a genuinely empty watchlist at the call site.

    Also lifts `bar_counts` KEYS as the WARM set — the symbols this bot has touched since boot.
    ⛔⭐ ONLY THE KEYS. The VALUES are a different unit and must never be compared with a DB row
    count: `bar_counts` is a monotonic `+1` per bar seen since boot, not a buffer size, and it
    counts REST-warmup bars that were never persisted. On 2026-08-19 it read ZNB=1118 against 20
    persisted rows. The seed reads `strategy_bar_history`, so the DB is the only valid numerator.
    """
    if not raw:
        raise DetectorBlind(
            "no isolated-bot-state event on the stream — the bot may be down, or the stream may be "
            "misnamed. ⛔ An absent/empty Redis stream reads as XLEN 0, which is NOT 'no symbols'."
        )
    try:
        event = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise DetectorBlind(f"bot-state event is not parseable JSON: {exc}") from exc

    if event.get("event_type") != "isolated_bot_state":
        raise DetectorBlind(f"unexpected event_type {event.get('event_type')!r} on the stream")

    payload = event.get("payload") or {}
    code = payload.get("strategy_code")
    if code != STRATEGY_CODE:
        raise DetectorBlind(
            f"newest event is for strategy {code!r}, not {STRATEGY_CODE!r} — this stream is shared, "
            "so reading it without checking the code would sweep another bot's watchlist"
        )

    raw_produced = event.get("produced_at")
    if not raw_produced:
        raise DetectorBlind("event carries no produced_at — freshness cannot be established")
    produced_at = datetime.fromisoformat(str(raw_produced).replace("Z", "+00:00"))
    if produced_at.tzinfo is None:
        produced_at = produced_at.replace(tzinfo=UTC)
    age = (now - produced_at).total_seconds()
    if age > max_age_seconds:
        raise DetectorBlind(
            f"newest bot-state event is {age:.0f}s old (limit {max_age_seconds}s) — refusing to "
            "sweep a stale watchlist. A snapshot from a dead bot would sweep the WRONG symbols and "
            "still print a confident clean."
        )

    if "watchlist" not in payload:
        raise DetectorBlind("event has no 'watchlist' field — schema changed under the detector")

    return BotState(
        watchlist=sorted({str(s).upper() for s in payload["watchlist"]}),
        warm=frozenset(str(s).upper() for s in (payload.get("bar_counts") or {})),
        produced_at=produced_at,
    )


# ⛔⭐ THE WINDOW, NOT A COUNT. `LIMIT %(limit)s` mirrors the seed's own `.limit(DB_SEED_BAR_LIMIT)`,
# so this reads exactly the rows `_seed_strategy_bars_from_db` would load — including the case where
# fewer than the limit exist, which is `min(limit, available)` on both sides.
# `lag(...) OVER (ORDER BY bar_time DESC)` gives each bar its NEWER neighbour, so `newer - bar_time`
# is the gap between adjacent loaded bars. The BOUNDARY gap is computed from `max(bar_time)` against
# the caller's clock, not here — the DB's `now()` and the detector's `now` must not disagree.
WINDOW_SQL = """
WITH win AS (
  SELECT h.bar_time,
         lag(h.bar_time) OVER (ORDER BY h.bar_time DESC) AS newer
  FROM strategy_bar_history h
  WHERE h.strategy_code = %(code)s AND h.interval_secs = %(iv)s AND h.symbol = %(sym)s
  ORDER BY h.bar_time DESC
  LIMIT %(limit)s
)
SELECT count(*) AS window_bars,
       max(bar_time) AS newest_bar,
       max(newer - bar_time) AS max_internal_gap
FROM win
"""

BARS_EVER_SQL = """
SELECT count(*) FROM strategy_bar_history
WHERE strategy_code = %(code)s AND interval_secs = %(iv)s AND symbol = %(sym)s
"""

# The on-deck sweep, same window logic applied across the recent universe in one pass.
ONDECK_SQL = """
WITH recent AS (
  SELECT DISTINCT symbol FROM strategy_bar_history
  WHERE strategy_code = %(code)s AND interval_secs = %(iv)s AND bar_time >= %(lookback)s
), w AS (
  SELECT r.symbol, x.window_bars, x.newest_bar, x.max_internal_gap
  FROM recent r
  CROSS JOIN LATERAL (
    SELECT count(*) AS window_bars, max(bar_time) AS newest_bar,
           max(newer - bar_time) AS max_internal_gap
    FROM (
      SELECT h.bar_time, lag(h.bar_time) OVER (ORDER BY h.bar_time DESC) AS newer
      FROM strategy_bar_history h
      WHERE h.strategy_code = %(code)s AND h.interval_secs = %(iv)s AND h.symbol = r.symbol
      ORDER BY h.bar_time DESC LIMIT %(limit)s
    ) q
  ) x
)
SELECT symbol, window_bars, newest_bar, max_internal_gap FROM w
WHERE window_bars > 0
  AND (newest_bar < %(cutoff)s OR max_internal_gap > %(maxgap)s)
ORDER BY newest_bar
"""


@dataclass(frozen=True)
class OnDeckRow:
    symbol: str
    window_bars: int
    newest_bar: datetime
    max_internal_gap: timedelta | None

    def kind(self, now: datetime) -> str:
        """BOUNDARY is named separately because #721 does not cover it."""
        return "BOUNDARY" if (now - self.newest_bar) > EXPOSURE_AGE else "internal"


def sweep(conn, symbols: list[str], now: datetime) -> list[SweepRow]:
    """Describe each symbol by the window the seed would load. No bar-count inference."""
    rows: list[SweepRow] = []
    with conn.cursor() as cur:
        for sym in symbols:
            params = {"code": STRATEGY_CODE, "iv": INTERVAL_SECS, "sym": sym, "limit": SEED_LIMIT}
            cur.execute(WINDOW_SQL, params)
            window_bars, newest_bar, max_internal_gap = cur.fetchone()
            cur.execute(BARS_EVER_SQL, params)
            (bars_ever,) = cur.fetchone()
            rows.append(
                SweepRow(
                    symbol=sym,
                    window_bars=int(window_bars or 0),
                    bars_ever=int(bars_ever or 0),
                    newest_bar=newest_bar,
                    max_internal_gap=max_internal_gap,
                )
            )
    return rows


def on_deck(conn, now: datetime, exclude: set[str]) -> list[OnDeckRow]:
    """§132 — exposed symbols NOT yet on the watchlist. These are PREDICTIONS.

    Each one is exposed the moment it joins the watchlist, so a later `[V2-DB-SEED-GAP]` naming it
    is a CONFIRMED FORECAST. ⛔ A BOUNDARY-exposed name will NOT produce that log line — #721 cannot
    see it — so for those the forecast can only be confirmed by reading the seeded series, never by
    waiting for a truncation that will never come.
    """
    with conn.cursor() as cur:
        cur.execute(
            ONDECK_SQL,
            {
                "code": STRATEGY_CODE,
                "iv": INTERVAL_SECS,
                "limit": SEED_LIMIT,
                "lookback": now - timedelta(days=ONDECK_LOOKBACK_DAYS),
                "cutoff": now - EXPOSURE_AGE,
                "maxgap": EXPOSURE_AGE,
            },
        )
        return [
            OnDeckRow(s, int(w or 0), n, g) for s, w, n, g in cur.fetchall() if s not in exclude
        ]


DEFAULT_SERVICE_SOURCE = "src/project_mai_tai/services/schwab_1m_v2_bot.py"


def check_constants(source_path: str) -> str:
    """Re-read SEED_LIMIT / INTERVAL_SECS from the service and report any drift ('' == in step).

    ⛔ The two constants here are a MIRROR, kept so the script runs standalone on the box without
    importing the service. A mirror that drifts silently is exactly the failure this whole file
    exists to prevent: the sweep would keep printing confident verdicts measured against a threshold
    the seed no longer uses. Parsed textually — importing the service would drag in its settings and
    connections, which a read-only detector must not do.
    """
    wanted = {"DB_SEED_BAR_LIMIT": SEED_LIMIT, "INTERVAL_SECS": INTERVAL_SECS}
    found: dict[str, int] = {}
    with open(source_path, encoding="utf-8") as fh:
        for line in fh:
            for name in wanted:
                prefix = f"{name} = "
                if line.startswith(prefix):
                    try:
                        found[name] = int(line[len(prefix) :].split("#")[0].strip())
                    except ValueError:
                        pass
    problems = [f"{n} not found in {source_path}" for n in wanted if n not in found]
    problems += [
        f"{n}: service says {found[n]}, this script mirrors {wanted[n]}"
        for n in wanted
        if n in found and found[n] != wanted[n]
    ]
    return "; ".join(problems)


def _dsn(arg: str | None) -> str:
    """⛔ Raises DetectorBlind, NOT SystemExit.

    Caught live on 2026-08-19: a missing DSN raised `SystemExit(str)`, which exits **1** — the exact
    code this script uses for EXPOSURE FOUND. A cron reading exit codes would have read "cannot
    reach the database" as "a symbol is exposed", and the inverse reading is worse: someone
    treating 1 as routine would swallow a real finding. Same defect class as the log-line cap, found
    in the detector itself. Every inability to see is exit 2.
    """
    raw = arg or os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not raw:
        raise DetectorBlind("no DSN: pass --dsn or set MAI_TAI_DATABASE_URL")
    return raw.replace("postgresql+psycopg://", "postgresql://")


def _read_newest_event(redis_url: str, stream: str) -> str | None:
    import redis  # imported lazily so the pure functions stay testable without the dependency

    client = redis.Redis.from_url(redis_url, decode_responses=True)
    entries = client.xrevrange(stream, "+", "-", count=1)
    if not entries:
        return None
    _entry_id, fields = entries[0]
    return fields.get("data")


def main() -> int:
    ap = argparse.ArgumentParser(description="§131 seed-exposure detector (read-only)")
    ap.add_argument("--dsn", default=None)
    ap.add_argument(
        "--redis-url", default=os.environ.get("MAI_TAI_REDIS_URL", "redis://localhost:6379/0")
    )
    ap.add_argument(
        "--stream-prefix", default=os.environ.get("MAI_TAI_REDIS_STREAM_PREFIX", "mai_tai")
    )
    ap.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    ap.add_argument(
        "--assert-constants",
        action="store_true",
        help="re-check SEED_LIMIT/INTERVAL_SECS against the service source and refuse on drift",
    )
    ap.add_argument("--service-source", default=DEFAULT_SERVICE_SOURCE)
    a = ap.parse_args()

    now = datetime.now(UTC)
    stream = f"{a.stream_prefix}:strategy-state-isolated"
    print(f"seed-exposure detector — {now.astimezone(ET):%Y-%m-%d %a %H:%M:%S} ET")
    if a.assert_constants:
        try:
            drift = check_constants(a.service_source)
        except OSError as exc:
            print(f"  ⛔ CANNOT SEE — REFUSING: cannot read {a.service_source}: {exc}")
            return 2
        if drift:
            print(f"  ⛔ CONSTANT DRIFT — REFUSING: {drift}")
            print(
                "  ⛔ The mirrored limit no longer matches the service. Every verdict below would"
            )
            print("     be measured against the wrong threshold. Exit 2.")
            return 2
        print(
            f"  constants : SEED_LIMIT={SEED_LIMIT} INTERVAL_SECS={INTERVAL_SECS} ✅ match the service"
        )
    print(f"  source    : redis {stream}   ⛔ log-line `sample=` path RETIRED (B13, capped at 5)")
    print(
        f"  predicate : strategy_code={STRATEGY_CODE} AND interval_secs={INTERVAL_SECS} (the seed's own)"
    )

    try:
        raw = _read_newest_event(a.redis_url, stream)
        state = parse_watchlist_event(raw, now, a.max_age_seconds)
        watchlist, produced_at = state.watchlist, state.produced_at
    except DetectorBlind as exc:
        print(f"  ⛔ CANNOT SEE — REFUSING: {exc}")
        print("  ⛔ This is UNKNOWN, not clean. Exit 2.")
        return 2
    except Exception as exc:  # noqa: BLE001 — any failure to read is still a refusal, never a pass
        print(f"  ⛔ CANNOT SEE — REFUSING: {type(exc).__name__}: {exc}")
        print("  ⛔ This is UNKNOWN, not clean. Exit 2.")
        return 2

    age = (now - produced_at).total_seconds()
    print(
        f"  watchlist : {len(watchlist)} symbol(s), published {age:.0f}s ago (limit {a.max_age_seconds}s)"
    )

    # ⛔ The DB phase fails CLOSED too. A connection refused, a permission error or a renamed column
    # must refuse, never fall through to a verdict — see _dsn() for the live exit-code near-miss.
    try:
        with psycopg.connect(_dsn(a.dsn)) as conn:
            rows = sweep(conn, watchlist, now)
            ondeck = on_deck(conn, now, exclude=set(watchlist))
    except DetectorBlind as exc:
        print(f"  ⛔ CANNOT SEE — REFUSING: {exc}")
        print("  ⛔ This is UNKNOWN, not clean. Exit 2.")
        return 2
    except Exception as exc:  # noqa: BLE001 — a failed read is a refusal, never a pass
        print(f"  ⛔ CANNOT SEE — REFUSING: database read failed: {type(exc).__name__}: {exc}")
        print("  ⛔ This is UNKNOWN, not clean. Exit 2.")
        return 2

    # ⛔ COVERAGE PROOF. A detector that cannot state its denominator has the same defect it hunts.
    if len(rows) != len(watchlist):
        print(f"  ⛔ COVERAGE FAILURE: swept {len(rows)} of {len(watchlist)} — REFUSING to report.")
        return 2

    if not watchlist:
        print("  swept 0 of 0 — NOTHING TO SWEEP (the watchlist is empty).")
        print("  ⛔ This is not a clean bill of health; there was simply nothing to look at.")
        return 0

    print(f"  swept {len(rows)} of {len(watchlist)} ✅ coverage proven")

    # ⛔⭐ POPULATION FIRST, INSTANCE SECOND. The count and its denominator lead; symbol names are
    # the appendix. A report that opens with a ticker invites the reader to treat the finding as
    # one symbol's problem.
    classified = [(r, *r.classify(now)) for r in rows]
    exposed_rows = [(r, why) for r, is_exp, why in classified if is_exp]
    boundary_n = sum(1 for r, why in exposed_rows if "BOUNDARY" in why)
    print(
        f"  VERDICT   : {len(exposed_rows)} EXPOSED of {len(rows)} swept "
        f"({boundary_n} boundary, {len(exposed_rows) - boundary_n} internal)"
    )
    for row, _is_exposed, why in classified:
        print(f"    {row.symbol:<7} window={row.window_bars:<4} ever={row.bars_ever:<6} {why}")

    if ondeck:
        warm = [r for r in ondeck if r.symbol in state.warm]
        cold = [r for r in ondeck if r.symbol not in state.warm]
        b_all = sum(1 for r in ondeck if r.kind(now) == "BOUNDARY")
        print(
            f"  ON DECK   : {len(ondeck)} exposed and NOT on the watchlist (§132 PREDICTIONS) — "
            f"{b_all} boundary, {len(ondeck) - b_all} internal"
        )
        print(
            f"              {len(warm)} WARM (bot holds a buffer now) · {len(cold)} COLD (dormant)"
        )
        shown = warm + cold[: max(0, ONDECK_PRINT_CAP - len(warm))]
        for r in shown:
            age = (now - r.newest_bar).total_seconds() / 86400.0
            tag = "WARM" if r.symbol in state.warm else "cold"
            print(
                f"    {tag} {r.symbol:<7} window={r.window_bars:<4} "
                f"newest {age:.1f}d old  [{r.kind(now)}]"
            )
        if len(ondeck) > len(shown):
            print(f"    ... showing {len(shown)} of {len(ondeck)} — the COUNT above is complete.")
        print(
            "    ⛔ A BOUNDARY name produces NO [V2-DB-SEED-GAP] line when it joins — #721 cannot"
        )
        print("       see it. Absence of a truncation is NOT evidence it was safe.")

    return 1 if exposed_rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
