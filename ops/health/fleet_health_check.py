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

def classify_order_lifecycle(
    stuck_count: int | None,
    oldest_stuck_min: float | None,
) -> tuple[str, str]:
    """OMS order-lifecycle verdict — the alive-but-not-executing detector.

    The OMS creates a trade_intents row when it CONSUMES an intent, then resolves it to a
    terminal status (filled/rejected/cancelled/...) or an order within sub-seconds; a healthy
    OMS also terminalizes orphaned intents each sync cycle. So a trade_intents row that is
    NON-terminal AND has NO broker_order AND has aged well past that (>threshold) means the
    OMS consumed the intent but never placed or resolved it — it is beating but not executing
    (the 07-01 class the liveness watchdog can't see: dead-OMS = no heartbeat = watchdog's job;
    this is alive-but-stuck). NO recent stuck intents -> GREEN (a quiet market simply produces
    no intents; that must never red — the key no-false-alarm guard)."""
    if stuck_count is None:
        return ("AMBER", "could not read trade_intents (cannot assess)")
    if stuck_count <= 0:
        return ("GREEN", "no intents stuck pre-order (OMS executing or idle)")
    age = f"{oldest_stuck_min:.0f}m" if oldest_stuck_min is not None else "?"
    return (
        "RED",
        f"{stuck_count} intent(s) CONSUMED but stuck non-terminal with NO order "
        f"(oldest {age}) — OMS alive-but-not-executing / terminalize not running",
    )


def check_oms_order_lifecycle() -> tuple[str, str, str]:
    """Check #2: the OMS is actually EXECUTING (intent -> order/terminal), not just beating.
    Ground truth = trade_intents (what the OMS consumed) LEFT JOIN broker_orders (what it
    placed). Only reds when intents exist AND are stuck — never on a quiet market."""
    # Stuck = non-terminal status, no broker_order, aged past a generous 10-min bound
    # (normal resolution is sub-second; a resting LIMIT order has an order row so it is
    # excluded; a rejected intent is terminal so it is excluded). 6h upper bound skips
    # ancient rows from a prior day.
    terminal = "('filled','rejected','cancelled','expired','abandoned')"
    where = (
        f"ti.status NOT IN {terminal} "
        "AND bo.id IS NULL "
        "AND ti.created_at < now() - interval '10 min' "
        "AND ti.created_at > now() - interval '6 hours'"
    )
    stuck = _scalar_int(
        "SELECT count(*) FROM trade_intents ti "
        f"LEFT JOIN broker_orders bo ON bo.intent_id = ti.id WHERE {where}"
    )
    oldest = _scalar_int(
        "SELECT round(extract(epoch FROM (now()-min(ti.created_at)))/60)::int "
        "FROM trade_intents ti "
        f"LEFT JOIN broker_orders bo ON bo.intent_id = ti.id WHERE {where}"
    )
    level, detail = classify_order_lifecycle(stuck, oldest)
    return (level, "oms-order-lifecycle", detail)


def classify_stops_armed(
    unprotected_count: int | None,
    owned_open_count: int | None,
) -> tuple[str, str]:
    """Stops-armed verdict — every OMS-OWNED open position must have an armed stop.

    OMS-owned = a per-strategy `virtual_positions` row (the OMS's ledger of what IT placed).
    A manual holding has NO such row → it is never counted → NEVER trips 'unprotected' (the
    scoping invariant: it's not the OMS's to protect). Protection = an `oms_armed_stops` row
    (ORB) OR an open `oms_managed_positions` row (v2's exit ladder). This is the ongoing
    observability that a stop is always armed on what the OMS holds — the check that would
    have caught a naked position before F2 fixed the restart gap.

    No-false-alarm guard: 0 unprotected → GREEN whether the fleet is flat (nothing to
    protect) or every owned position is armed. Flat must be GREEN, never RED."""
    if unprotected_count is None:
        return ("AMBER", "could not read positions/stops (cannot assess)")
    if unprotected_count <= 0:
        if not owned_open_count:
            return ("GREEN", "no OMS-owned open positions (flat — nothing to protect)")
        return ("GREEN", f"all {owned_open_count} OMS-owned open position(s) have an armed stop")
    return (
        "RED",
        f"{unprotected_count} OMS-owned open position(s) have NO armed stop — NAKED "
        "(unprotected; a manual holding can't trip this — OMS-owned only)",
    )


def check_stops_armed() -> tuple[str, str, str]:
    """Check #3: every OMS-owned open position is protected by an armed stop. Ground truth =
    virtual_positions (OMS ownership) LEFT JOIN oms_armed_stops (ORB) + oms_managed_positions
    (v2 ladder). OMS-owned ONLY — manual positions have no virtual_positions row (invariant)."""
    # 2-min settle guard on opened_at: skip a just-opened position (its arm is written in the
    # same fill-processing commit, but this margin guarantees no false RED on the open path).
    joins = (
        "FROM virtual_positions vp "
        "JOIN strategies s ON s.id = vp.strategy_id "
        "JOIN broker_accounts ba ON ba.id = vp.broker_account_id "
        "LEFT JOIN oms_armed_stops a "
        "ON a.strategy_code = s.code AND a.broker_account_name = ba.name AND a.symbol = vp.symbol "
        "LEFT JOIN oms_managed_positions m "
        "ON m.broker_account_name = ba.name AND m.symbol = vp.symbol AND m.status = 'open' "
        "WHERE vp.quantity <> 0 AND vp.opened_at < now() - interval '2 min'"
    )
    unprotected = _scalar_int(f"SELECT count(*) {joins} AND a.id IS NULL AND m.id IS NULL")
    owned_open = _scalar_int(
        "SELECT count(*) FROM virtual_positions WHERE quantity <> 0 "
        "AND opened_at < now() - interval '2 min'"
    )
    level, detail = classify_stops_armed(unprotected, owned_open)
    return (level, "stops-armed", detail)


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
    check_strategy_bar_freshness,   # #1 frozen-loop detector
    check_oms_order_lifecycle,      # #2 alive-but-not-executing detector
    check_stops_armed,              # #3 every OMS-owned open position has an armed stop
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
