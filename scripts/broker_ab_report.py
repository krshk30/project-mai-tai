#!/usr/bin/env python3
"""broker_ab_report.py — Schwab-vs-Webull broker bake-off for the v2 dual-broker mirror.

When the v2 mirror is enabled a single ``schwab_1m_v2`` buy-open fans out to BOTH a
primary (Schwab) account and a mirror (Webull) account, so each mirrored entry has TWO
legs on TWO ``broker_accounts``. This report pairs those legs (same symbol, nearest
``submitted_at`` within a window) and compares which broker executed better —
fill-rate, slippage vs the intent reference price, and REAL fill latency
(``fills.filled_at − broker_orders.submitted_at``) — PLUS a Webull coverage section
answering "can Webull even trade these names" (fill vs reject rate + grouped reasons).

READ-ONLY. Issues SELECTs only; never trades, never writes.

Usage:
  MAI_TAI_DATABASE_URL=... python scripts/broker_ab_report.py [--date 2026-07-10]
  MAI_TAI_DATABASE_URL=... python scripts/broker_ab_report.py --start 2026-07-01 --end 2026-07-10
  python scripts/broker_ab_report.py --dsn postgresql://... --primary live:schwab_1m_v2 --mirror live:v2_webull

Defaults: last 5 ET days; primary/mirror account names from the v2 settings.
The empty case ("no mirrored trades in range") is expected until the mirror is enabled.
"""
from __future__ import annotations

import argparse
import os
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# Fallback defaults mirroring project_mai_tai.settings (kept literal so the read-only
# script has no hard dependency on importing the service settings module).
DEFAULT_PRIMARY_ACCOUNT = "live:schwab_1m_v2"
DEFAULT_MIRROR_ACCOUNT = "live:v2_webull"
DEFAULT_PAIR_WINDOW_S = 15.0
DEFAULT_LOOKBACK_DAYS = 5
STRATEGY_CODE = "schwab_1m_v2"


# --------------------------------------------------------------------------------------
# Pure, testable data model + logic (no DB access below this line until fetch_legs()).
# --------------------------------------------------------------------------------------
@dataclass
class FillRow:
    """One row from ``fills`` for a leg (partials → multiple rows)."""

    quantity: float
    price: float
    filled_at: datetime | None
    webull_place_time: datetime | None = None  # payload->>'webull_broker_place_time'
    webull_fill_time: datetime | None = None  # payload->>'webull_broker_filled_time'


@dataclass
class OrderLeg:
    """One ``broker_orders`` row (a single broker's leg of a mirrored entry)."""

    account: str
    symbol: str
    status: str
    submitted_at: datetime | None
    reference_price: float | None
    is_webull: bool = False
    reject_reason: str | None = None
    fills: list[FillRow] = field(default_factory=list)


@dataclass
class LegMetrics:
    account: str
    symbol: str
    outcome: str
    fill_qty: float
    fill_price: float | None
    slippage_pct: float | None
    slippage_dollars: float | None
    fill_latency_s: float | None
    webull_internal_s: float | None


@dataclass
class Pair:
    primary: OrderLeg
    mirror: OrderLeg | None


def leg_fill_qty(leg: OrderLeg) -> float:
    return sum(f.quantity for f in leg.fills)


def leg_fill_price(leg: OrderLeg) -> float | None:
    """Quantity-weighted average fill price across partials, or None if unfilled."""
    total_qty = leg_fill_qty(leg)
    if total_qty <= 0:
        return None
    return sum(f.quantity * f.price for f in leg.fills) / total_qty


def leg_last_fill_time(leg: OrderLeg) -> datetime | None:
    """Latest real broker fill timestamp across partials (None if unfilled/missing)."""
    times = [f.filled_at for f in leg.fills if f.filled_at is not None]
    return max(times) if times else None


def leg_outcome(leg: OrderLeg) -> str:
    """Classify the leg. A row can be status=filled yet carry no fill rows; the
    presence of real fills wins so slippage/latency stay honest."""
    if leg_fill_qty(leg) > 0:
        status = (leg.status or "").lower()
        return "partially_filled" if status == "partially_filled" else "filled"
    status = (leg.status or "").lower()
    if status == "rejected":
        return "rejected"
    if status == "cancelled":
        return "cancelled"
    return "no-fill"


def leg_slippage(leg: OrderLeg) -> tuple[float | None, float | None]:
    """(pct, dollars) of the weighted fill price vs the reference price.

    Positive = paid MORE than reference (worse for a buy). None when either the
    reference price or a real fill is missing (guards NULL reference_price / rejects).
    """
    ref = leg.reference_price
    fill_px = leg_fill_price(leg)
    if ref is None or ref == 0 or fill_px is None:
        return (None, None)
    dollars = fill_px - ref
    return (dollars / ref * 100.0, dollars)


def leg_fill_latency_s(leg: OrderLeg) -> float | None:
    """REAL fill latency = last fills.filled_at − broker_orders.submitted_at (seconds)."""
    submitted = leg.submitted_at
    filled = leg_last_fill_time(leg)
    if submitted is None or filled is None:
        return None
    return (filled - submitted).total_seconds()


def leg_webull_internal_s(leg: OrderLeg) -> float | None:
    """Webull's internal place→fill latency from the fill payload times (bonus metric).

    Uses the latest fill row that carries both broker-stamped times. None otherwise.
    """
    if not leg.is_webull:
        return None
    best: float | None = None
    best_time: datetime | None = None
    for f in leg.fills:
        if f.webull_place_time is None or f.webull_fill_time is None:
            continue
        delta = (f.webull_fill_time - f.webull_place_time).total_seconds()
        if best_time is None or (f.webull_fill_time and f.webull_fill_time >= best_time):
            best = delta
            best_time = f.webull_fill_time
    return best


def leg_to_metrics(leg: OrderLeg) -> LegMetrics:
    slip_pct, slip_usd = leg_slippage(leg)
    return LegMetrics(
        account=leg.account,
        symbol=leg.symbol,
        outcome=leg_outcome(leg),
        fill_qty=leg_fill_qty(leg),
        fill_price=leg_fill_price(leg),
        slippage_pct=slip_pct,
        slippage_dollars=slip_usd,
        fill_latency_s=leg_fill_latency_s(leg),
        webull_internal_s=leg_webull_internal_s(leg),
    )


def pair_legs(
    primaries: list[OrderLeg],
    mirrors: list[OrderLeg],
    window_s: float = DEFAULT_PAIR_WINDOW_S,
) -> list[Pair]:
    """One-to-one greedy pairing of primary→mirror legs on the SAME symbol whose
    ``submitted_at`` are nearest within ``window_s`` (mirror submits right after the
    primary). Primaries are processed oldest-first; each takes its closest unused mirror
    within the window. Unpaired primaries are returned with ``mirror=None``.
    """
    remaining = [m for m in mirrors if m.submitted_at is not None]
    used: set[int] = set()
    pairs: list[Pair] = []

    ordered = sorted(
        primaries,
        key=lambda p: (p.submitted_at is None, p.submitted_at or datetime.max.replace(tzinfo=UTC)),
    )
    for prim in ordered:
        best_idx: int | None = None
        best_delta: float | None = None
        if prim.submitted_at is not None:
            for idx, mir in enumerate(remaining):
                if idx in used or mir.symbol != prim.symbol:
                    continue
                delta = abs((mir.submitted_at - prim.submitted_at).total_seconds())
                if delta > window_s:
                    continue
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best_idx = idx
        if best_idx is None:
            pairs.append(Pair(primary=prim, mirror=None))
        else:
            used.add(best_idx)
            pairs.append(Pair(primary=prim, mirror=remaining[best_idx]))
    return pairs


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


@dataclass
class BrokerAggregate:
    label: str
    count: int
    fill_count: int
    fill_rate: float | None
    avg_slippage_pct: float | None
    median_slippage_pct: float | None
    avg_latency_s: float | None
    median_latency_s: float | None
    avg_webull_internal_s: float | None


def aggregate_side(label: str, legs: list[OrderLeg]) -> BrokerAggregate:
    """Aggregate one broker's legs over the PAIRED set (count/fill-rate/slippage/latency)."""
    metrics = [leg_to_metrics(leg) for leg in legs]
    count = len(metrics)
    fill_count = sum(1 for m in metrics if m.outcome in ("filled", "partially_filled"))
    slips = [m.slippage_pct for m in metrics if m.slippage_pct is not None]
    lats = [m.fill_latency_s for m in metrics if m.fill_latency_s is not None]
    internals = [m.webull_internal_s for m in metrics if m.webull_internal_s is not None]
    return BrokerAggregate(
        label=label,
        count=count,
        fill_count=fill_count,
        fill_rate=(fill_count / count) if count else None,
        avg_slippage_pct=_mean(slips),
        median_slippage_pct=_median(slips),
        avg_latency_s=_mean(lats),
        median_latency_s=_median(lats),
        avg_webull_internal_s=_mean(internals),
    )


@dataclass
class WebullCoverage:
    attempts: int
    filled: int
    rejected: int
    cancelled: int
    no_fill: int
    reject_reasons: dict[str, int]


def webull_coverage(mirror_legs: list[OrderLeg]) -> WebullCoverage:
    """Of ALL v2 mirror attempts: how many Webull filled vs rejected, with grouped
    reject reasons. Answers "can Webull trade these names". Counts every mirror leg
    (paired or not) — a reject has no partner in the fill comparison but still counts
    here."""
    filled = rejected = cancelled = no_fill = 0
    reasons: dict[str, int] = {}
    for leg in mirror_legs:
        outcome = leg_outcome(leg)
        if outcome in ("filled", "partially_filled"):
            filled += 1
        elif outcome == "rejected":
            rejected += 1
            reason = (leg.reject_reason or "unspecified").strip() or "unspecified"
            reasons[reason] = reasons.get(reason, 0) + 1
        elif outcome == "cancelled":
            cancelled += 1
        else:
            no_fill += 1
    return WebullCoverage(
        attempts=len(mirror_legs),
        filled=filled,
        rejected=rejected,
        cancelled=cancelled,
        no_fill=no_fill,
        reject_reasons=dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
    )


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------
def _fmt(value: float | None, spec: str = ".2f", suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value:{spec}}{suffix}"


def _fmt_pct_rate(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def render_report(
    *,
    range_label: str,
    primary_account: str,
    mirror_account: str,
    pairs: list[Pair],
    all_mirror_legs: list[OrderLeg],
    window_s: float,
) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("v2 BROKER A/B REPORT - Schwab (primary) vs Webull (mirror)")
    lines.append(f"range: {range_label}   strategy: {STRATEGY_CODE}   pair-window: {window_s:g}s")
    lines.append(f"primary account: {primary_account}   mirror account: {mirror_account}")
    lines.append("=" * 78)

    paired = [p for p in pairs if p.mirror is not None]
    unpaired = [p for p in pairs if p.mirror is None]

    if not pairs and not all_mirror_legs:
        lines.append("")
        lines.append("no mirrored trades in range (expected until the v2 mirror is enabled).")
        lines.append("")
        return "\n".join(lines)

    # --- Per-pair detail --------------------------------------------------------------
    lines.append("")
    lines.append(f"PAIRED ENTRIES: {len(paired)}   UNPAIRED primaries (mirror missing): {len(unpaired)}")
    lines.append("")
    header = (
        f"{'SYMBOL':8}{'BROKER':10}{'OUTCOME':16}{'FILL$':>10}"
        f"{'SLIP%':>8}{'SLIP$':>9}{'LAT(s)':>8}{'WB-INT(s)':>10}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for pair in pairs:
        prim = leg_to_metrics(pair.primary)
        lines.append(_render_metric_row(pair.primary.symbol, "schwab", prim))
        if pair.mirror is not None:
            mir = leg_to_metrics(pair.mirror)
            lines.append(_render_metric_row(pair.mirror.symbol, "webull", mir))
        else:
            lines.append(f"{pair.primary.symbol:8}{'webull':10}{'(no mirror leg)':16}")
        lines.append("")

    # --- Aggregate comparison (paired set) --------------------------------------------
    schwab_agg = aggregate_side("Schwab", [p.primary for p in paired])
    webull_agg = aggregate_side("Webull", [p.mirror for p in paired if p.mirror is not None])
    lines.append("AGGREGATE (paired set only)")
    lines.append("-" * 60)
    col = f"{'METRIC':30}{'Schwab':>14}{'Webull':>14}"
    lines.append(col)
    lines.append("-" * len(col))
    lines.append(f"{'legs':30}{schwab_agg.count:>14}{webull_agg.count:>14}")
    lines.append(
        f"{'fill-rate':30}{_fmt_pct_rate(schwab_agg.fill_rate):>14}"
        f"{_fmt_pct_rate(webull_agg.fill_rate):>14}"
    )
    lines.append(
        f"{'avg slippage %':30}{_fmt(schwab_agg.avg_slippage_pct):>14}"
        f"{_fmt(webull_agg.avg_slippage_pct):>14}"
    )
    lines.append(
        f"{'median slippage %':30}{_fmt(schwab_agg.median_slippage_pct):>14}"
        f"{_fmt(webull_agg.median_slippage_pct):>14}"
    )
    lines.append(
        f"{'avg fill-latency s':30}{_fmt(schwab_agg.avg_latency_s):>14}"
        f"{_fmt(webull_agg.avg_latency_s):>14}"
    )
    lines.append(
        f"{'median fill-latency s':30}{_fmt(schwab_agg.median_latency_s):>14}"
        f"{_fmt(webull_agg.median_latency_s):>14}"
    )
    lines.append(
        f"{'avg Webull place->fill s':30}{'n/a':>14}"
        f"{_fmt(webull_agg.avg_webull_internal_s):>14}"
    )

    # --- Webull coverage / reject section ---------------------------------------------
    cov = webull_coverage(all_mirror_legs)
    lines.append("")
    lines.append("WEBULL COVERAGE / REJECTS (all v2 mirror attempts - can Webull trade these?)")
    lines.append("-" * 60)
    lines.append(
        f"attempts: {cov.attempts}   filled: {cov.filled}   rejected: {cov.rejected}"
        f"   cancelled: {cov.cancelled}   no-fill: {cov.no_fill}"
    )
    if cov.attempts:
        lines.append(f"Webull rejected {cov.rejected}/{cov.attempts} names.")
    if cov.reject_reasons:
        parts = [f"{reason} x{cnt}" for reason, cnt in cov.reject_reasons.items()]
        lines.append("  reasons: " + ", ".join(parts))
    lines.append("")
    return "\n".join(lines)


def _render_metric_row(symbol: str, broker: str, m: LegMetrics) -> str:
    return (
        f"{symbol:8}{broker:10}{m.outcome:16}"
        f"{_fmt(m.fill_price, '.4f'):>10}"
        f"{_fmt(m.slippage_pct, '+.2f'):>8}"
        f"{_fmt(m.slippage_dollars, '+.4f'):>9}"
        f"{_fmt(m.fill_latency_s, '.1f'):>8}"
        f"{_fmt(m.webull_internal_s, '.2f'):>10}"
    )


# --------------------------------------------------------------------------------------
# DB layer (READ-ONLY SELECTs)
# --------------------------------------------------------------------------------------
def _dsn(arg: str | None) -> str:
    raw = arg or os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not raw:
        raise SystemExit("no DSN: pass --dsn or set MAI_TAI_DATABASE_URL")
    return raw.replace("postgresql+psycopg://", "postgresql://")


# Legs for one account: open buy orders + their fills (LEFT JOIN so rejects/no-fills
# still appear). intent_type='open' comes from the linked trade_intents row; if the
# order has no intent link we fall back to side='buy' so nothing is silently dropped.
LEGS_SQL = """
SELECT bo.id,
       bo.symbol,
       bo.status,
       bo.submitted_at,
       (bo.payload->>'reference_price')::numeric        AS reference_price,
       bo.payload->>'reject_reason'                     AS reject_reason,
       bo.payload->>'broker_reason'                     AS broker_reason,
       f.quantity                                       AS fill_qty,
       f.price                                          AS fill_price,
       f.filled_at                                      AS filled_at,
       f.payload->>'webull_broker_place_time'           AS wb_place_time,
       f.payload->>'webull_broker_filled_time'          AS wb_fill_time
FROM broker_orders bo
JOIN broker_accounts ba ON ba.id = bo.broker_account_id
JOIN strategies s ON s.id = bo.strategy_id
LEFT JOIN trade_intents ti ON ti.id = bo.intent_id
LEFT JOIN fills f ON f.order_id = bo.id
WHERE ba.name = %(acct)s
  AND s.code = %(strategy)s
  AND bo.side = 'buy'
  AND (ti.intent_type = 'open' OR ti.intent_type IS NULL)
  AND (COALESCE(bo.submitted_at, bo.updated_at)
        AT TIME ZONE 'America/New_York')::date >= %(start)s
  AND (COALESCE(bo.submitted_at, bo.updated_at)
        AT TIME ZONE 'America/New_York')::date <= %(end)s
ORDER BY bo.submitted_at, bo.id, f.filled_at;
"""


def _parse_webull_time(raw: str | None) -> datetime | None:
    """Webull payload times are stored as strings (e.g. 'YYYY-MM-DD HH:MM:SS.mmm+0000')."""
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def fetch_legs(conn, account: str, is_webull: bool, start: str, end: str) -> list[OrderLeg]:
    """Load one account's open-buy legs (with fills folded in) as OrderLeg objects."""
    params = {"acct": account, "strategy": STRATEGY_CODE, "start": start, "end": end}
    legs: dict[object, OrderLeg] = {}
    with conn.cursor() as cur:
        cur.execute(LEGS_SQL, params)
        for row in cur.fetchall():
            (order_id, symbol, status, submitted_at, ref_price, reject_reason,
             broker_reason, fill_qty, fill_price, filled_at, wb_place, wb_fill) = row
            leg = legs.get(order_id)
            if leg is None:
                leg = OrderLeg(
                    account=account,
                    symbol=symbol,
                    status=status,
                    submitted_at=submitted_at,
                    reference_price=float(ref_price) if ref_price is not None else None,
                    is_webull=is_webull,
                    reject_reason=(reject_reason or broker_reason),
                )
                legs[order_id] = leg
            if fill_qty is not None and fill_price is not None:
                leg.fills.append(
                    FillRow(
                        quantity=float(fill_qty),
                        price=float(fill_price),
                        filled_at=filled_at,
                        webull_place_time=_parse_webull_time(wb_place),
                        webull_fill_time=_parse_webull_time(wb_fill),
                    )
                )
    return list(legs.values())


def _resolve_range(args: argparse.Namespace) -> tuple[str, str, str]:
    if args.date:
        return (args.date, args.date, args.date)
    if args.start or args.end:
        today = datetime.now(EASTERN).date()
        start = args.start or args.end or today.isoformat()
        end = args.end or args.start or today.isoformat()
        return (start, end, f"{start} -> {end}")
    today = datetime.now(EASTERN).date()
    start = (today - timedelta(days=DEFAULT_LOOKBACK_DAYS - 1)).isoformat()
    end = today.isoformat()
    return (start, end, f"{start} -> {end} (last {DEFAULT_LOOKBACK_DAYS} days)")


def main() -> int:
    ap = argparse.ArgumentParser(description="v2 Schwab-vs-Webull broker A/B report (read-only)")
    ap.add_argument("--date", help="single ET date YYYY-MM-DD (overrides --start/--end)")
    ap.add_argument("--start", help="ET range start YYYY-MM-DD")
    ap.add_argument("--end", help="ET range end YYYY-MM-DD")
    ap.add_argument("--primary", default=DEFAULT_PRIMARY_ACCOUNT, help="primary (Schwab) account name")
    ap.add_argument("--mirror", default=DEFAULT_MIRROR_ACCOUNT, help="mirror (Webull) account name")
    ap.add_argument("--window", type=float, default=DEFAULT_PAIR_WINDOW_S, help="pair window seconds")
    ap.add_argument("--dsn", default=None, help="DB DSN override (else MAI_TAI_DATABASE_URL)")
    args = ap.parse_args()

    start, end, range_label = _resolve_range(args)

    import psycopg  # local import so pure logic/tests need no DB driver

    with psycopg.connect(_dsn(args.dsn)) as conn:
        conn.read_only = True
        primaries = fetch_legs(conn, args.primary, is_webull=False, start=start, end=end)
        mirrors = fetch_legs(conn, args.mirror, is_webull=True, start=start, end=end)

    pairs = pair_legs(primaries, mirrors, window_s=args.window)
    print(
        render_report(
            range_label=range_label,
            primary_account=args.primary,
            mirror_account=args.mirror,
            pairs=pairs,
            all_mirror_legs=mirrors,
            window_s=args.window,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
