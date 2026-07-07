#!/usr/bin/env python3
"""Fleet FUNCTION-health checks (F3) — validate FUNCTION, not process.

Why this exists: every silent failure we've had was a component reporting *healthy*
while its function was dead — the OMS up-but-zombied, #388 deployed-but-reconcile-never-
fired, a position record-said-held while the broker was flat. The self-report is the thing
that lies. F3 checks "is it doing its job" against GROUND TRUTH (DB / fills / independent
capture), never the component's own heartbeat/snapshot.

Independence: stdlib + `psql`/`redis-cli` subprocess only — NO app imports, so a frozen
service (or a hung DB) can't take this check down the same way. Runs from an independent
cron (see fleet_health_cron.sh), like the pre-open readiness check and the OMS-liveness
watchdog — never as a long-running service.

Registry: `CHECKS` lists the enabled checks; each returns (level, name, detail). main()
prints one `VERDICT: <LEVEL> <name> <detail>` line per check + an aggregate, and exits with
the worst level (0=GREEN, 1=AMBER, 2=RED) so the cron routes to ntfy.

DESIGN CONSTRAINT (load-bearing): alert only on a signal that is RED *only* when genuinely
broken. A check that false-alarms on normal quiet gets ignored, which defeats the purpose.
So "strategy bars are stale" is RED only when the upstream feed is SIMULTANEOUSLY LIVE
(trades flowing) — i.e. it cannot be a quiet market or a feed outage; it's a frozen loop.
"""
from __future__ import annotations

import glob
import subprocess
import sys

# --- ground-truth access (independent: subprocess, no app import) ------------- #

_DSN_CACHE: list[str | None] = []


def _dsn() -> str | None:
    """The DB DSN, read from a running project service's /proc environ (the root-only env
    file is injected there by systemd; trader owns the service processes). No app import,
    no secret written to disk. None if no service is running / not found."""
    if _DSN_CACHE:
        return _DSN_CACHE[0]
    dsn = None
    try:
        pids = subprocess.run(
            ["pgrep", "-f", "mai-tai-"], capture_output=True, text=True, timeout=5
        ).stdout.split()
    except Exception:
        pids = []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/environ", "rb") as fh:
                for kv in fh.read().split(b"\0"):
                    if kv.startswith(b"MAI_TAI_DATABASE_URL="):
                        dsn = kv.split(b"=", 1)[1].decode().replace(
                            "postgresql+psycopg://", "postgresql://"
                        )
                        break
        except OSError:
            continue
        if dsn:
            break
    _DSN_CACHE.append(dsn)
    return dsn


def _scalar_int(sql: str) -> int | None:
    """Run a single-value SQL and return it as int, or None (no DSN / error / NULL)."""
    dsn = _dsn()
    if not dsn:
        return None
    try:
        out = subprocess.run(
            ["psql", dsn, "-tAc", sql], capture_output=True, text=True, timeout=15
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    val = out.stdout.strip()
    if not val:
        return None
    try:
        return int(float(val))
    except ValueError:
        return None


# --- pure decision logic (unit-tested; no I/O) -------------------------------- #

def classify_bar_freshness(
    bar_age_s: int | None,
    feed_age_s: int | None,
    *,
    stale_amber_s: int = 120,
    stale_red_s: int = 240,
    feed_fresh_max_s: int = 120,
) -> tuple[str, str]:
    """Strategy-engine bar-freshness verdict — the frozen-loop detector.

    polygon_30s persists a 30s bar per interval from the live Polygon feed. If the
    upstream feed is LIVE (market_capture_trades fresh) but bars have stopped advancing,
    the strategy loop is frozen (the exact 'reports healthy while dead' class). If the
    feed is quiet/stale, bars legitimately don't advance — NOT a strategy fault → GREEN
    (this is the no-false-alarm guard: a quiet market never reds)."""
    if bar_age_s is None:
        return ("AMBER", "no polygon_30s bars in strategy_bar_history (cannot assess)")
    if feed_age_s is None or feed_age_s > feed_fresh_max_s:
        return (
            "GREEN",
            f"bars {bar_age_s}s old but upstream feed quiet/stale "
            f"(feed_age={feed_age_s}s) — staleness not attributable to the strategy",
        )
    # Feed is LIVE → any bar staleness IS attributable to the strategy loop.
    if bar_age_s < stale_amber_s:
        return ("GREEN", f"strategy bars fresh ({bar_age_s}s) with live feed")
    if bar_age_s < stale_red_s:
        return ("AMBER", f"strategy bars slowing ({bar_age_s}s) while feed live (feed_age={feed_age_s}s)")
    return (
        "RED",
        f"strategy bars STALE {bar_age_s}s while upstream feed LIVE "
        f"(feed_age={feed_age_s}s) — polygon_30s loop likely FROZEN",
    )


# --- checks (I/O + decision) -------------------------------------------------- #

def check_strategy_bar_freshness() -> tuple[str, str, str]:
    """Check #1: strategy-engine is actually producing bars (function), cross-checked
    against the independent Polygon capture (ground truth), not its own snapshot."""
    bar_age = _scalar_int(
        "SELECT round(extract(epoch FROM (now()-max(bar_time))))::int "
        "FROM strategy_bar_history WHERE strategy_code='polygon_30s'"
    )
    feed_age = _scalar_int(
        "SELECT round(extract(epoch FROM (now()-max(received_at))))::int "
        "FROM market_capture_trades WHERE received_at > now() - interval '10 min'"
    )
    level, detail = classify_bar_freshness(bar_age, feed_age)
    return (level, "strategy-bar-freshness", detail)


CHECKS = [
    check_strategy_bar_freshness,
    # #2 oms-order-lifecycle, #3 stops-armed — added incrementally after #1 validates.
]

_RANK = {"GREEN": 0, "AMBER": 1, "RED": 2}
_EXIT = {"GREEN": 0, "AMBER": 1, "RED": 2}


def main() -> int:
    worst = "GREEN"
    for check in CHECKS:
        try:
            level, name, detail = check()
        except Exception as exc:  # noqa: BLE001 — a check crash must not crash the runner
            level, name, detail = ("AMBER", getattr(check, "__name__", "check"), f"check errored: {exc}")
        print(f"VERDICT: {level} {name} {detail}")
        if _RANK[level] > _RANK[worst]:
            worst = level
    print(f"VERDICT: {worst} fleet-function-health ({len(CHECKS)} check(s))")
    return _EXIT[worst]


if __name__ == "__main__":
    sys.exit(main())
