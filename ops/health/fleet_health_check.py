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


# ⛔⭐ REAL-MONEY SCOPE — the allowlist that keeps a SIM trade out of a real-money pager.
#
# 2026-08-03: unscoped checks let `polygon_30s` (PAPER/sim) drive pages. It reached the entries
# counter, the sawtooth check and #628 before this sweep. A pager that fires on something you will
# never act on trains you to ignore it — and this pager guards NAKED POSITIONS.
#
# ⛔ `name LIKE 'live:%'` is NOT a safe proxy: `live:polygon_30s` and `live:webull_30s` both exist
# as rows. Verified 2026-08-03 — only three accounts had any order in 30 days: live:schwab_1m_v2
# (1200), live:orb (584), paper:polygon_30s (3868). The first two are real money (v2 plus its
# Webull fan-out leg); the other `live:` rows are dormant legacy names.
REAL_MONEY_ACCOUNTS: tuple[str, ...] = ("live:schwab_1m_v2", "live:orb")
_REAL_MONEY_SQL_LIST = ", ".join(f"'{a}'" for a in REAL_MONEY_ACCOUNTS)


def _with_paper_note(detail: str, paper_count: int | None, noun: str) -> str:
    """Append a paper/sim count to a verdict WITHOUT letting it change the level.

    Visible so a real sim defect is not hidden (polygon_30s really does reject every STOP sell
    with `missing reference_price`), but it can never page. Same shape as `sawtooth_paper` in the
    P0a watch."""
    if not paper_count:
        return detail
    plural = "" if paper_count == 1 else "s"
    return f"{detail} [paper/sim: {paper_count} {noun}{plural} — not paged]"


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
    # REAL MONEY ONLY drives the verdict; paper is counted and shown, never paged.
    joined = (
        "FROM trade_intents ti "
        "LEFT JOIN broker_orders bo ON bo.intent_id = ti.id "
        "JOIN broker_accounts ba ON ba.id = ti.broker_account_id"
    )
    real = f"ba.name IN ({_REAL_MONEY_SQL_LIST})"
    stuck = _scalar_int(f"SELECT count(*) {joined} WHERE {where} AND {real}")
    oldest = _scalar_int(
        "SELECT round(extract(epoch FROM (now()-min(ti.created_at)))/60)::int "
        f"{joined} WHERE {where} AND {real}"
    )
    paper_stuck = _scalar_int(f"SELECT count(*) {joined} WHERE {where} AND NOT ({real})")
    level, detail = classify_order_lifecycle(stuck, oldest)
    return (level, "oms-order-lifecycle", _with_paper_note(detail, paper_stuck, "stuck paper intent"))


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
    # ⛔ REAL MONEY ONLY. `paper:polygon_30s` NEVER gets an `oms_managed_positions` row (verified
    # 2026-08-03: that table holds live:orb / live:schwab_1m_v2 / paper:schwab_1m_v2 and nothing
    # else), so ANY polygon_30s position held past the 2-min settle guard counted as "NAKED" and
    # RED-paged — for a simulated position that cannot lose a cent.
    real = f"ba.name IN ({_REAL_MONEY_SQL_LIST})"
    unprotected = _scalar_int(
        f"SELECT count(*) {joins} AND {real} AND a.id IS NULL AND m.id IS NULL"
    )
    owned_open = _scalar_int(f"SELECT count(*) {joins} AND {real}")
    paper_unprotected = _scalar_int(
        f"SELECT count(*) {joins} AND NOT ({real}) AND a.id IS NULL AND m.id IS NULL"
    )
    level, detail = classify_stops_armed(unprotected, owned_open)
    return (level, "stops-armed",
            _with_paper_note(detail, paper_unprotected, "unprotected sim position"))


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



def classify_bar_continuity(
    worst_gap_min: int | None,
    gap_symbols: int | None,
    bars_seen: int | None,
    *,
    amber_gap_min: int = 2,
    red_gap_min: int = 10,
) -> tuple[str, str]:
    """schwab_1m_v2 bar-continuity verdict — the SILENT ATR-corruption detector.

    ⭐ WHY IT EXISTS (2026-07-30, live money). A hole in `strategy_bar_history` makes true range
    span the gap: `href`/`lref` reference `prev.close`, so ONE bar carries the whole outage. v2 was
    stopped 10:12-11:33 ET and NUWE's ATR read 0.149 against a true 1-minute ATR of ~0.06 —
    `loss = 3.5 * ATR` put the resting buy-stop at 4.74 while the operator's chart showed ~4.40.
    Every resting order on a gap-spanning symbol sits too high until the bad TR ages out.

    ⛔ Gaps are NOT restart-only. Same day, no outage: CRWU 25 min, AXTU 2-13 min, SNDG 3 min.
    Nobody was checking, so nobody knew. That is the whole reason this check exists.

    ⛔ NO-FALSE-ALARM GUARD: no bars at all means off-hours or an empty watchlist, NOT a fault —
    GREEN. This check must never red a quiet market (same rule as the freshness check)."""
    if not bars_seen:
        return ("GREEN", "no schwab_1m_v2 bars in the window (off-hours or empty watchlist)")
    if worst_gap_min is None or worst_gap_min <= 1:
        return ("GREEN", f"schwab_1m_v2 bars contiguous ({bars_seen} bars, no gaps)")
    if worst_gap_min < amber_gap_min:
        return ("GREEN", f"schwab_1m_v2 bars contiguous within tolerance (worst {worst_gap_min}min)")
    if worst_gap_min < red_gap_min:
        return (
            "AMBER",
            f"schwab_1m_v2 BAR GAP {worst_gap_min}min on {gap_symbols} symbol(s) — "
            f"DB series holed (backtest/parity read it); live ATR is guarded by #620",
        )
    return (
        "RED",
        f"schwab_1m_v2 BAR HOLE {worst_gap_min}min on {gap_symbols} symbol(s) — "
        f"DB series holed; backtest/parity/recorder read it. Live ATR guarded by #620 — "
        f"confirm [V2-ATR-BAR-GAP] fired for these names. Do NOT restart on this alert alone.",
    )


def check_bar_continuity() -> tuple[str, str, str]:
    """Check #4: the v2 bar series has no holes — a hole silently corrupts ATR and
    misprices every resting order on that symbol."""
    window = (
        "FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 "
        "AND bar_time >= now() - interval '30 minutes'"
    )
    bars_seen = _scalar_int(f"SELECT count(*) {window}")
    worst = _scalar_int(
        "WITH g AS (SELECT symbol, bar_time, lead(bar_time) OVER "
        "(PARTITION BY symbol ORDER BY bar_time) nxt "
        f"{window}) "
        "SELECT coalesce(max(extract(epoch FROM (nxt-bar_time))/60),0)::int FROM g "
        "WHERE nxt - bar_time > interval '1 minute'"
    )
    syms = _scalar_int(
        "WITH g AS (SELECT symbol, bar_time, lead(bar_time) OVER "
        "(PARTITION BY symbol ORDER BY bar_time) nxt "
        f"{window}) "
        "SELECT count(DISTINCT symbol) FROM g WHERE nxt - bar_time > interval '1 minute'"
    )
    level, detail = classify_bar_continuity(worst, syms, bars_seen)
    return (level, "v2-bar-continuity", detail)


CHECKS = [
    check_strategy_bar_freshness,   # #1 frozen-loop detector
    check_oms_order_lifecycle,      # #2 alive-but-not-executing detector
    check_stops_armed,              # #3 every OMS-owned open position has an armed stop
    check_bar_continuity,           # #4 v2 bar holes -> ATR spans them -> orders mispriced
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
