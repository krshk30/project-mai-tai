#!/usr/bin/env python3
"""ENTRY SLIPPAGE — what we actually PAID vs the level we DECIDED, split by resting slot.

THE QUESTION THIS ANSWERS
-------------------------
#676 moved the RECLAIM entry from a chasing MARKET order to a resting STOP_LIMIT at the segment
high. Its acceptance is a PRICE comparison: reactive fills should show the resting path's dispersion
(SD ~25 bps, nothing past ~60) instead of the old market path's **SD 57.0 / worst +351.7 bps**.

⭐ The data was there all along. `fills.price` is the broker's execution price, populated by every
adapter via `ExecutionReport.fill_price` and persisted at `oms/store.py:629` — 100% coverage on both
real-money accounts. What was missing was this query, not the data. (An earlier claim that "we never
store the fill price anywhere" came from checking `broker_orders.payload` and generalising from one
table to the database. It was wrong.)

HOW SLOT IS ATTRIBUTED — and why not by time
--------------------------------------------
⭐ #821 made `trade_intents.metadata.cw_entry_slot=first|reclaim` the durable authority. Order style
(`resting_entry`) is not economic slot: a reclaim can itself rest. Pre-#821 fills lack the durable
field, so only that historical population falls back to `[V2-RESTING-PLACE]` logs.

For the historical fallback, we join on **(symbol, stop_price, limit_price)** — an EXACT match
against the placement line — not on "nearest preceding placement". A resting order reprices every
few minutes, so a time-nearest join would silently mis-attribute a fill to the wrong placement, and
the two slots rest at levels that move in OPPOSITE directions (first tracks the ATR trail down,
reclaim tracks the segment high up). Getting that backwards would invert the comparison.

Usage (read-only; safe any time):
  python scripts/resting_entry_slippage.py --days 11
  python scripts/resting_entry_slippage.py --days 11 --account live:schwab_1m_v2
"""
from __future__ import annotations

import argparse
import glob
import gzip
import re
import statistics
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import text

from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

LOG_GLOB = "/var/log/project-mai-tai/schwab-1m-v2.log*"
PLACE_RE = re.compile(
    r"\[V2-RESTING-PLACE\]\s+(?P<sym>[A-Z.]+)\s+slot=(?P<slot>\w+)\s+"
    r"stop=(?P<stop>[0-9.]+)\s+limit=(?P<limit>[0-9.]+)"
)
VALID_ENTRY_SLOTS = {"first", "reclaim"}


def load_slot_index() -> tuple[dict[tuple[str, str, str], str], str]:
    """Return the historical slot index and a machine-readable service-log verdict.

    ⚠️ Logs rotate at 00:00 UTC = 20:00 ET and are kept ~7 days. A fill older than that loses its
    slot attribution entirely — reported below as UNATTRIBUTED rather than silently dropped.
    """
    index: dict[tuple[str, str, str], str] = {}
    paths = sorted(glob.glob(LOG_GLOB))
    if not paths:
        print(
            "⛔ SERVICE LOGS MISSING_OR_ROTATED (0 files) — historical slot fallback is "
            "COULD_NOT_TELL, not a zero placement population."
        )
        return index, "MISSING_OR_ROTATED"
    unreadable: list[str] = []
    for path in paths:
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", errors="replace") as fh:
                for line in fh:
                    if "V2-RESTING-PLACE" not in line:
                        continue
                    m = PLACE_RE.search(line)
                    if m:
                        for sk, lk in _keys(m["stop"], m["limit"]):
                            index.setdefault((m["sym"], sk, lk), m["slot"])
        except (OSError, PermissionError) as exc:
            unreadable.append(f"{path} ({type(exc).__name__})")
    # ⛔ NEVER FAIL SILENTLY TO AN EMPTY INDEX. Missing/rotated files, unreadable files, and
    # readable files containing zero placement markers are different evidence states. None may be
    # laundered into a claim that the historical denominator itself was zero.
    if unreadable:
        print(f"⛔ {len(unreadable)}/{len(paths)} v2 log file(s) UNREADABLE — slot attribution will")
        print("   be WRONG, not merely absent. Logs are root:root 640; run this with sudo.")
        for u in unreadable[:4]:
            print(f"     {u}")
    if not index:
        if unreadable:
            print("⛔ SLOT INDEX IS EMPTY because retained logs were unreadable — TOOL failure.")
        else:
            print(
                "SLOT INDEX has 0 placement markers across readable retained service logs; "
                "historical_fallback=AVAILABLE_NO_MARKERS."
            )
    if len(unreadable) == len(paths):
        evidence = "UNREADABLE"
    elif unreadable:
        evidence = "PARTIAL_UNREADABLE"
    elif not index:
        evidence = "AVAILABLE_NO_MARKERS"
    else:
        evidence = "AVAILABLE"
    return index, evidence


def _keys(stop: object, limit: object) -> list[tuple[str, str]]:
    """Candidate (stop, limit) keys, most specific first.

    ⛔ THE JOIN THAT FAILED FIRST TIME. The log prints the RAW computed level
    (`stop=1.3742 limit=1.3811`); the order carries it ROUNDED TO TICK (`1.37` / `1.38`). An
    exact string/value match therefore missed 48 of 48 resting fills — the script ran, produced
    confident-looking numbers, and attributed nothing. Match at tick precision too.
    """
    out: list[tuple[str, str]] = []
    for places in (None, 2, 4):
        try:
            sd, ld = Decimal(str(stop)), Decimal(str(limit))
            if places is not None:
                q = Decimal("1." + "0" * places)
                sd, ld = sd.quantize(q), ld.quantize(q)
            out.append((f"{sd.normalize():f}", f"{ld.normalize():f}"))
        except Exception:  # noqa: BLE001
            continue
    return out


def bps(fill: Decimal, ref: Decimal) -> float:
    """POSITIVE = we paid MORE than the decided level (worse). Negative = better than decided."""
    return float((fill - ref) / ref * Decimal("10000"))


def resolve_entry_slot(row, slot_index: dict[tuple[str, str, str], str]) -> tuple[str, bool]:
    """Return (slot, unattributed), preferring the durable #821 field.

    The log join is historical fallback only. `resting_entry` is deliberately absent: using order
    style as a slot proxy is the defect this report must not reintroduce.
    """
    durable = str(row.get("entry_slot") or "").strip().lower()
    if durable in VALID_ENTRY_SLOTS:
        return durable, False
    for stop_key, limit_key in _keys(row.get("stop_price"), row.get("limit_price")):
        historical = str(
            slot_index.get((str(row.get("symbol") or ""), stop_key, limit_key), "")
        ).strip().lower()
        if historical in VALID_ENTRY_SLOTS:
            return historical, False
    return "unattributed", True


def format_slot_coverage(attributed: int, total: int) -> str:
    """Refuse a numeric coverage result when the population is empty or partly unknown."""
    if attributed < 0 or total < 0 or attributed > total:
        return (
            f"cw_entry_slot coverage={attributed}/{total} -- COULD_NOT_TELL "
            "(invalid counts: numerator must not exceed denominator)"
        )
    if total == 0:
        return (
            "cw_entry_slot coverage=0/0 -- COULD_NOT_TELL "
            "(denominator=0; no price-comparable BUY fills)"
        )
    unknown = total - attributed
    if unknown:
        return (
            f"cw_entry_slot coverage={attributed}/{total} -- COULD_NOT_TELL "
            f"({unknown} fill{' has' if unknown == 1 else 's have'} unknown classification; "
            "percentage withheld)"
        )
    return f"cw_entry_slot coverage={attributed}/{total} = 100.0% -- GRADEABLE"


def summarise(label: str, rows: list[tuple[str, float]]) -> None:
    """⛔ MEDIAN-FIRST, with a DROP-ONE by NAME. Never a bare total."""
    if not rows:
        print(f"  {label:<22} n=0   — UNEXERCISED (no fills on this path; not a pass, not a fail)")
        return
    vals = [v for _, v in rows]
    med = statistics.median(vals)
    line = (f"  {label:<22} n={len(vals):<3} median={med:+7.1f}bps"
            f"  worst={max(vals):+8.1f}  best={min(vals):+8.1f}")
    if len(vals) > 1:
        line += f"  SD={statistics.stdev(vals):6.1f}"
    print(line)
    # drop-one BY NAME: which single symbol is carrying the result?
    by_sym: dict[str, list[float]] = defaultdict(list)
    for sym, v in rows:
        by_sym[sym].append(v)
    if len(by_sym) > 1:
        for sym in sorted(by_sym):
            kept = [v for s, v in rows if s != sym]
            if kept:
                print(f"        drop {sym:<6} (n={len(by_sym[sym])}) -> median "
                      f"{statistics.median(kept):+7.1f}bps")
    else:
        only = next(iter(by_sym))
        print(f"        ⛔ ONE SYMBOL ONLY ({only}) — a drop-one is not possible; "
              f"this is a single-name result, not a population.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=11)
    ap.add_argument("--account", default="live:schwab_1m_v2")
    args = ap.parse_args()

    settings = get_settings()
    sf = build_session_factory(settings)
    slot_index, historical_log_verdict = load_slot_index()

    with sf() as s:
        rows = s.execute(text("""
            SELECT f.symbol                              AS symbol,
                   f.price                               AS fill_price,
                   bo.payload->>'reference_price'        AS ref_price,
                   bo.payload->>'stop_price'             AS stop_price,
                   bo.payload->>'limit_price'            AS limit_price,
                   COALESCE(ti.payload->'metadata'->>'cw_entry_slot','') AS entry_slot,
                   to_char(f.filled_at AT TIME ZONE 'America/New_York',
                           'MM-DD HH24:MI:SS')           AS et
            FROM fills f
            JOIN broker_orders bo   ON bo.id = f.order_id
            JOIN broker_accounts ba ON ba.id = f.broker_account_id
            LEFT JOIN trade_intents ti ON ti.id = bo.intent_id
            WHERE ba.name = :acct AND f.side = 'buy'
              AND f.filled_at >= now() - (:days || ' days')::interval
            ORDER BY f.filled_at
        """), {"acct": args.account, "days": args.days}).mappings().all()

    print("=" * 82)
    print(f"ENTRY SLIPPAGE — fill vs decided level     account={args.account}   last {args.days}d")
    print("⛔ ACCOUNT VISIBILITY: this ONE account. The other broker's fills are invisible here")
    print("   BY CONSTRUCTION — run again with --account live:orb to see the fan-out leg.")
    print("=" * 82)

    buckets: dict[str, list[tuple[str, float]]] = defaultdict(list)
    no_ref = unattributed = 0
    detail: list[str] = []
    for r in rows:
        if not r["ref_price"]:
            no_ref += 1
            continue
        b = bps(Decimal(str(r["fill_price"])), Decimal(str(r["ref_price"])))
        slot, is_unattributed = resolve_entry_slot(r, slot_index)
        unattributed += int(is_unattributed)
        buckets[slot].append((r["symbol"], b))
        detail.append(f"  {r['et']}  {r['symbol']:<6} {slot:<14} "
                      f"fill={r['fill_price']!s:<12} decided={r['ref_price']:<9} {b:+8.1f}bps")

    print("\nPER-FILL (ET)")
    print("\n".join(detail) if detail else "  (none)")

    slot_population = sum(len(values) for values in buckets.values())
    attributed = slot_population - unattributed
    slot_gradeable = slot_population > 0 and attributed == slot_population
    print("\nSLOT ATTRIBUTION COVERAGE — denominator = price-comparable BUY fills")
    print("  " + format_slot_coverage(attributed, slot_population))

    print("\nBY SLOT — ⭐ #676's acceptance is the `reclaim` row")
    for slot in sorted(buckets):
        summarise(slot, buckets[slot])

    print("\nBASELINE TO BEAT (old reactive MARKET path): SD 57.0 bps, worst +351.7 bps")
    print("⭐ STRUCTURAL FLOOR: a resting STOP_LIMIT carries a 0.50% band, so its fill CANNOT be")
    print("   worse than +50 bps. The tail is killed by construction, not by this measurement.")

    print("\n=== WHAT THIS CANNOT SEE ===")
    print(f"  - {no_ref} fill(s) had NO decided reference_price and are excluded (sell/exit legs")
    print("    carry none, so EXIT slippage is not computable this way at all).")
    print(f"  - {unattributed} fill(s) had neither durable cw_entry_slot nor a retained historical")
    print("    placement-log match. They are reported as unattributed, never inferred from order style.")
    print("  - pre-#821 history still depends on logs, which rotate at 20:00 ET and keep ~7 days.")
    print("  - It cannot say whether the DECIDED level was a good level. Price only, by design —")
    print("    the strategy is parked; this measures execution, never edge.")
    print("  - A median over a handful of fills from ONE symbol is not a population. Read n first.")
    print(
        f"\nVERDICT slot_attribution={'GRADEABLE' if slot_gradeable else 'COULD_NOT_TELL'} "
        f"cw_entry_slot_coverage={attributed}/{slot_population} "
        f"historical_log_verdict={historical_log_verdict}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
