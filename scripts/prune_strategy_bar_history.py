"""Delete `strategy_bar_history` rows belonging to bots that no longer exist.

⭐ WHY THIS IS NOT AN AGE-BASED PRUNE. Measured 2026-07-29: the table is 2,103,233 rows / 1955 MB
across 87 trading days, and **52% of it belongs to six bots that stopped writing between April and
2026-06-09**. Across the whole codebase only TWO strategy_codes are ever read back —
`schwab_1m_v2` (the backtest bar source: `backtest/data.py` filters
`WHERE strategy_code='schwab_1m_v2' AND interval_secs=60`) and `polygon_30s`. The dead codes have
zero readers at any age, so ownership is the right axis, not `bar_time`.

    dead codes   1,091,270 rows  52%  ~1014 MB   <- this script
    polygon_30s    811,896 rows  39%   ~755 MB   deliberately NOT touched (paper bot, still writing)
    schwab_1m_v2   200,067 rows  10%   ~186 MB   ⛔ NEVER delete: it IS the backtest bar source

⛔ DENYLIST, NOT ALLOWLIST — on purpose. An allowlist ("delete anything not in KEEP") would silently
destroy a NEW bot's history the day it starts writing. With an explicit denylist the worst case is
that some dead data survives, which is harmless. For a DELETE, fail-safe beats fail-tidy.

⛔ SILENCE GUARD. Each target must have written nothing for `--min-silent-days` (default 14) or the
script refuses. If one of these bots is ever revived, its fresh rows are protected without anyone
remembering to edit this file.

Dry-run by DEFAULT: it reports and deletes nothing unless `--go` is passed.

  cd /home/trader/project-mai-tai && .venv/bin/python scripts/prune_strategy_bar_history.py
  cd /home/trader/project-mai-tai && .venv/bin/python scripts/prune_strategy_bar_history.py --go
"""
from __future__ import annotations

import argparse
import os

import psycopg

# Bots that stopped writing between 2026-04-22 and 2026-06-09 and have no reader anywhere.
DEAD_CODES = (
    "macd_30s",
    "schwab_1m",
    "webull_30s",
    "tos",
    "macd_1m",
    "macd_30s_reclaim",
)

# Codes some reader still filters on. Deleting these is a bug, not a policy choice.
PROTECTED_CODES = ("schwab_1m_v2", "polygon_30s")


def validate_codes(codes: tuple[str, ...]) -> tuple[str, ...]:
    """Raise unless every code is on the reviewed dead list. Extracted so the refusal is unit-
    testable without a DSN — the guard is the whole point of the script, so it must be provable."""
    for c in codes:
        if c in PROTECTED_CODES:
            raise SystemExit(f"refusing to prune a code that is still READ: {c!r}")
        if c not in DEAD_CODES:
            raise SystemExit(f"refusing to prune a code not on the reviewed dead list: {c!r}")
    return codes


def _dsn(arg: str | None) -> str:
    raw = arg or os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not raw:
        raise SystemExit("no DSN: pass --dsn or set MAI_TAI_DATABASE_URL")
    return raw.replace("postgresql+psycopg://", "postgresql://")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="actually delete (default is a dry run)")
    ap.add_argument("--batch", type=int, default=50_000)
    ap.add_argument("--min-silent-days", type=int, default=14)
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--codes", default=",".join(DEAD_CODES))
    args = ap.parse_args()

    codes = validate_codes(tuple(c.strip() for c in args.codes.split(",") if c.strip()))

    total = 0
    with psycopg.connect(_dsn(args.dsn)) as conn:
        for code in codes:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*), max(bar_time), "
                    "       max(bar_time) < now() - interval '%s days' "
                    "FROM strategy_bar_history WHERE strategy_code = %%s"
                    % int(args.min_silent_days),
                    (code,),
                )
                rows, newest, silent = cur.fetchone()
            if not rows:
                print(f"  {code:18} 0 rows (nothing to do)")
                continue
            if not silent:
                print(f"  {code:18} SKIPPED — wrote as recently as {newest} "
                      f"(< {args.min_silent_days}d); it is not dead")
                continue
            if not args.go:
                print(f"  {code:18} {rows:>9,} rows would be deleted (newest {newest})")
                total += rows
                continue

            deleted = 0
            while True:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM strategy_bar_history WHERE id IN ("
                        "  SELECT id FROM strategy_bar_history WHERE strategy_code = %s "
                        "  ORDER BY id LIMIT %s)",
                        (code, args.batch),
                    )
                    n = cur.rowcount
                conn.commit()
                deleted += n
                if n < args.batch:
                    break
            print(f"  {code:18} deleted {deleted:,} rows")
            total += deleted

    verb = "deleted" if args.go else "would be deleted (DRY RUN — pass --go)"
    print(f"\ntotal {verb}: {total:,} rows")
    if args.go:
        print("NOTE: run VACUUM (ANALYZE) strategy_bar_history to return the space to the OS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
