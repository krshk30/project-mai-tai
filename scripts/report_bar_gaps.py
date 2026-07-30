"""REPORT holes in `strategy_bar_history` — locate and size them; do NOT fill them.

⭐⭐ WHY (2026-07-30). A v2 outage leaves a hole in the persisted bar series, and **the REST warmup
repairs only the strategy's IN-MEMORY deque — it never writes the missing bars back**. So after a
restart the bot trades correctly again while the DATABASE stays holed, and every consumer that
reads bars from the DB keeps reading a discontinuous series:

  * `project_mai_tai.backtest` / the replay engine — the bar source is `strategy_bar_history`
  * `ops/health/trade_recorder.py` — `mfe_pct` / `mae_pct` / `n_bars` / every what-if exit
  * the backtest-vs-live parity study

Measured that day: v2 was stopped 10:12-11:33 ET and EVERY watchlist symbol carried a single
85-minute hole (10:11 -> 11:36). Live trading was fixed by a restart; the DB was not.

⛔ Gaps are NOT restart-only. Same day, no outage: CRWU 25 min (09:30-09:55), AXTU 2-13 min
repeatedly, SNDG 3 min.

⛔⭐ THIS SCRIPT ONLY EVER **INSERTS MISSING** BARS — it never updates or deletes an existing row.
A bar we recorded live is the truth for what the bot actually saw; overwriting it with a later REST
snapshot would rewrite history and silently invalidate the parity study. Fail-safe: a bar REST does
not return simply stays missing.

⛔ DRY-RUN BY DEFAULT, `--go` to write — matching `prune_strategy_bar_history.py`.

⛔⭐ WHY IT DOES NOT FILL. Filling from Schwab REST would put a DIFFERENT PROVENANCE into the table
the backtest treats as ground truth, and `strategy_bar_history` has no provenance column to tell
them apart. That is the bar-source defect all over again — Polygon vs Schwab bars agreed on only
54.2% of ATR flips. A hole you can SEE is safer than a hole silently filled from another source.

The two honest options, operator's call:
  (a) leave holes; EXCLUDE those windows from any study        <- what this script enables
  (b) backfill AND add a provenance column so studies can filter   <- needs a migration

  cd /home/trader/project-mai-tai && .venv/bin/python scripts/backfill_bar_gaps.py --day 2026-07-30
"""
from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

ET = ZoneInfo("America/New_York")
STRATEGY_CODE = "schwab_1m_v2"
INTERVAL_SECS = 60

# A hole this small is ordinary bar jitter, not a data loss worth a REST round trip.
MIN_GAP_MINUTES = 2


def _dsn(arg: str | None) -> str:
    raw = arg or os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not raw:
        raise SystemExit("no DSN: pass --dsn or set MAI_TAI_DATABASE_URL")
    return raw.replace("postgresql+psycopg://", "postgresql://")


GAPS_SQL = """
WITH g AS (
  SELECT symbol, bar_time,
         lead(bar_time) OVER (PARTITION BY symbol ORDER BY bar_time) AS nxt
  FROM strategy_bar_history
  WHERE strategy_code = %(code)s AND interval_secs = %(iv)s
    AND bar_time >= %(lo)s AND bar_time < %(hi)s
)
SELECT symbol, bar_time, nxt,
       (EXTRACT(EPOCH FROM (nxt - bar_time)) / 60)::int AS gap_min
FROM g
WHERE nxt - bar_time > make_interval(mins => %(min_gap)s)
ORDER BY symbol, bar_time
"""


def find_gaps(conn, day: str) -> list[tuple[str, datetime, datetime, int]]:
    """Holes in one ET trading day's persisted series."""
    lo_et = datetime.fromisoformat(day).replace(tzinfo=ET)
    hi_et = lo_et + timedelta(days=1)
    with conn.cursor() as cur:
        cur.execute(GAPS_SQL, {
            "code": STRATEGY_CODE, "iv": INTERVAL_SECS,
            "lo": lo_et.astimezone(UTC), "hi": hi_et.astimezone(UTC),
            "min_gap": MIN_GAP_MINUTES,
        })
        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]


def missing_minutes(start: datetime, end: datetime) -> list[datetime]:
    """The bar timestamps absent BETWEEN two present bars (exclusive of both)."""
    out, t = [], start + timedelta(seconds=INTERVAL_SECS)
    while t < end:
        out.append(t)
        t += timedelta(seconds=INTERVAL_SECS)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="ET trading day, YYYY-MM-DD")
    ap.add_argument("--dsn", default=None)
    a = ap.parse_args()

    with psycopg.connect(_dsn(a.dsn)) as conn:
        gaps = find_gaps(conn, a.day)

    if not gaps:
        print(f"[backfill] {a.day}: no gaps >= {MIN_GAP_MINUTES}min — series is contiguous")
        return 0

    total_missing = 0
    for symbol, start, end, gap_min in gaps:
        missing = missing_minutes(start, end)
        total_missing += len(missing)
        print(
            f"[backfill] {a.day} {symbol}: {gap_min}min hole "
            f"{start.astimezone(ET):%H:%M} -> {end.astimezone(ET):%H:%M} "
            f"({len(missing)} bars missing)"
        )

    print(f"[backfill] {a.day}: {len(gaps)} gap(s), {total_missing} bar(s) missing in total")

    print("[backfill] ⛔ NOT FILLED — by design. Exclude these windows from any backtest, parity "
          "study, or recorder what-if. See the module docstring for why.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
