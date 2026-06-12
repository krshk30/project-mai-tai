"""Phase 1 replay study — schwab_1m_v2 signal expectancy (bar-only, read-only).

Per docs/replay-study-design.md (merged #285). Computes, for every fired v2
signal, the forward MFE/MAE over 5/15/30/60-min horizons and the outcome under a
small target/stop grid, across three fill assumptions. **MFE/MAE distributions
lead; the grid is secondary.** Both-hit ambiguous candles are reported as a
BOUNDED RANGE (target-first vs stop-first), never a point estimate. No tick data
→ intra-candle ordering is left explicit, not guessed (that's Phase 2).

⚠️ At the current N every aggregate is DIRECTIONAL, not statistical (wide CIs).

Entry = the signal bar CLOSE (== metadata.entry_price == the new reference_price).
Forward bars are strictly AFTER the signal bar. Source per signal: Schwab
pricehistory (complete/exact) where it reaches, else strategy_bar_history (v2's
stored bars, audit-proven byte-exact); the source + coverage are logged.

READ-ONLY. Invocation: env-sourced (DSN + Schwab token), same as the audit.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import psycopg

from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient
from project_mai_tai.settings import Settings

STRATEGY = "schwab_1m_v2"
HORIZONS = (5, 15, 30, 60)            # minutes
GRID = [(s, t) for s in (5, 10) for t in (10, 20)]   # (stop%, target%)
RTH_START_UTC_MIN = 13 * 60 + 30     # 13:30 UTC = 09:30 ET = RTH open


@dataclass
class Bar:
    ts: int  # bar-start epoch ms UTC
    high: float
    low: float
    close: float


def _dsn() -> str:
    return os.environ["MAI_TAI_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")


def load_signals(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ti.symbol,
                   (ti.payload->'metadata'->>'bar_time_ms')::bigint AS bar_ms,
                   (ti.payload->'metadata'->>'entry_price')::numeric AS entry,
                   ti.payload->'metadata'->>'path' AS path,
                   ti.created_at
            FROM trade_intents ti JOIN strategies s ON s.id = ti.strategy_id
            WHERE s.code = %s
              AND ti.payload->'metadata'->>'bar_time_ms' IS NOT NULL
              AND ti.payload->'metadata'->>'entry_price' IS NOT NULL
            ORDER BY ti.created_at
            """,
            (STRATEGY,),
        )
        out = []
        for sym, bar_ms, entry, path, created in cur.fetchall():
            out.append({"symbol": sym, "bar_ms": int(bar_ms), "entry": float(entry),
                        "path": path or "?", "created_at": created})
        return out


def vendor_day_bars(client, settings, sym, day_ms, cache) -> list[Bar]:
    """pricehistory 1m bars for sym on the UTC day containing day_ms. {} if dry."""
    day = datetime.fromtimestamp(day_ms / 1000, UTC).strftime("%Y-%m-%d")
    key = (sym, day)
    if key in cache:
        return cache[key]
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    params = urlencode({"symbol": sym, "periodType": "day", "frequencyType": "minute",
                        "frequency": 1, "startDate": int(d.timestamp() * 1000),
                        "endDate": int((d + timedelta(days=1)).timestamp() * 1000),
                        "needExtendedHoursData": "true"})
    url = f"{settings.schwab_base_url.rstrip('/')}{client.PRICE_HISTORY_PATH}?{params}"
    bars: list[Bar] = []
    try:
        payload = client._authorized_get(url)  # noqa: SLF001
        for c in (payload.get("candles") or []):
            if isinstance(c, dict):
                bars.append(Bar(int(c.get("datetime", 0) or 0), float(c.get("high", 0) or 0),
                                float(c.get("low", 0) or 0), float(c.get("close", 0) or 0)))
        time.sleep(0.25)  # be polite to the ~120 RPM quota
    except Exception:
        bars = []
    bars.sort(key=lambda b: b.ts)
    cache[key] = bars
    return bars


def store_day_bars(conn, sym, day_ms, cache) -> list[Bar]:
    day = datetime.fromtimestamp(day_ms / 1000, UTC).strftime("%Y-%m-%d")
    key = (sym, day)
    if key in cache:
        return cache[key]
    with conn.cursor() as cur:
        cur.execute(
            """SELECT EXTRACT(EPOCH FROM bar_time)*1000, high_price, low_price, close_price
               FROM strategy_bar_history WHERE strategy_code=%s AND symbol=%s
                 AND bar_time >= %s AND bar_time < %s ORDER BY bar_time""",
            (STRATEGY, sym, f"{day} 00:00:00+00", f"{day} 23:59:59+00"),
        )
        bars = [Bar(int(ts), float(h), float(l), float(c)) for ts, h, l, c in cur.fetchall()]
    cache[key] = bars
    return bars


def forward_bars(signal, conn, client, settings, vcache, scache) -> tuple[list[Bar], str]:
    """Bars strictly after the signal bar, within +60min. Prefer vendor; fall
    back to v2 stored bars. Returns (bars, source)."""
    bar_ms, sym = signal["bar_ms"], signal["symbol"]
    hi = bar_ms + 60 * 60_000
    vb = [b for b in vendor_day_bars(client, settings, sym, bar_ms, vcache) if bar_ms < b.ts <= hi]
    if vb:
        return vb, "vendor"
    sb = [b for b in store_day_bars(conn, sym, bar_ms, scache) if bar_ms < b.ts <= hi]
    return sb, ("store" if sb else "none")


def excursions(entry: float, bars: list[Bar], bar_ms: int, horizon_min: int) -> dict:
    """MFE/MAE over forward bars within [entry, entry + horizon_min]. Window is
    by TIMESTAMP cutoff (gap-safe), not by bar count."""
    cutoff = bar_ms + horizon_min * 60_000
    win = [b for b in bars if b.ts <= cutoff]
    if not win:
        return {"n": 0, "mfe_pct": None, "mae_pct": None, "ttm_mfe": None, "ttm_mae": None}
    mfe_bar = max(win, key=lambda b: b.high)
    mae_bar = min(win, key=lambda b: b.low)
    return {"n": len(win),
            "mfe_pct": round((mfe_bar.high - entry) / entry * 100, 3),
            "mae_pct": round((entry - mae_bar.low) / entry * 100, 3),
            "ttm_mfe": round((mfe_bar.ts - bar_ms) / 60_000),
            "ttm_mae": round((mae_bar.ts - bar_ms) / 60_000)}


def grid_outcome(entry: float, bars: list[Bar], stop_pct: float, target_pct: float) -> str:
    """Walk forward bars; first to hit. Both-in-one-candle => AMBIGUOUS."""
    target = entry * (1 + target_pct / 100)
    stop = entry * (1 - stop_pct / 100)
    for b in bars:
        hit_t = b.high >= target
        hit_s = b.low <= stop
        if hit_t and hit_s:
            return "AMBIGUOUS"
        if hit_t:
            return "TARGET"
        if hit_s:
            return "STOP"
    return "NO_HIT"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/replay.json")
    ap.add_argument("--assumed-spread-pct", type=float, default=1.0)   # Phase-1 placeholder
    ap.add_argument("--slippage-pct-per-side", type=float, default=0.5)
    args = ap.parse_args()

    settings = Settings()
    client = SchwabV2RestClient(settings, on_chart_bar=lambda *a: None, on_quote=lambda *a: None)
    vcache: dict = {}
    scache: dict = {}

    with psycopg.connect(_dsn()) as conn:
        signals = load_signals(conn)
        rows = []
        for sig in signals:
            bars, source = forward_bars(sig, conn, client, settings, vcache, scache)
            entry = sig["entry"]
            ex = {h: excursions(entry, bars, sig["bar_ms"], h) for h in HORIZONS}
            grid = {f"s{s}_t{t}": grid_outcome(entry, bars, s, t) for s, t in GRID}
            bar_min_utc = (datetime.fromtimestamp(sig["bar_ms"] / 1000, UTC).hour * 60
                           + datetime.fromtimestamp(sig["bar_ms"] / 1000, UTC).minute)
            rows.append({
                "symbol": sig["symbol"], "path": sig["path"], "entry": entry,
                "bar_ms": sig["bar_ms"], "source": source,
                "session": "premarket" if bar_min_utc < RTH_START_UTC_MIN else "RTH",
                "coverage_60m": ex[60]["n"], "excursions": ex, "grid": grid,
            })

    # ---- fill-assumption net returns for the grid (idealized/spread/slippage) ----
    costs = {"idealized": 0.0, "spread": args.assumed_spread_pct,
             "slippage": args.assumed_spread_pct + 2 * args.slippage_pct_per_side}

    def cell_expectancy(rows_subset, s, t, cost):
        """Return (best, worst) expectancy% over a subset, ambiguous bounded."""
        key = f"s{s}_t{t}"
        best, worst, n = [], [], 0
        for r in rows_subset:
            o = r["grid"][key]
            if o == "NO_HIT" or r["coverage_60m"] == 0:
                continue  # exclude undecided/no-coverage from the grid stat
            n += 1
            if o == "TARGET":
                best.append(t - cost); worst.append(t - cost)
            elif o == "STOP":
                best.append(-s - cost); worst.append(-s - cost)
            else:  # AMBIGUOUS
                best.append(t - cost); worst.append(-s - cost)
        if n == 0:
            return None
        return {"n": n, "exp_best_pct": round(sum(best) / n, 3),
                "exp_worst_pct": round(sum(worst) / n, 3),
                "ambiguous": sum(1 for r in rows_subset if r["grid"][key] == "AMBIGUOUS")}

    def pctiles(vals):
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return None
        def p(q):
            return round(vals[min(len(vals) - 1, int(q * len(vals)))], 3)
        return {"n": len(vals), "p25": p(.25), "p50": p(.50), "p75": p(.75), "p90": p(.90)}

    def mfe_mae_block(subset):
        return {h: {"mfe": pctiles([r["excursions"][h]["mfe_pct"] for r in subset]),
                    "mae": pctiles([r["excursions"][h]["mae_pct"] for r in subset])}
                for h in HORIZONS}

    def grid_block(subset):
        return {f"s{s}_t{t}": {fa: cell_expectancy(subset, s, t, c) for fa, c in costs.items()}
                for s, t in GRID}

    by = lambda f: {k: [r for r in rows if f(r) == k] for k in sorted({f(r) for r in rows})}
    summary = {
        "generated_for": "schwab_1m_v2 replay study Phase 1",
        "signals": len(rows),
        "coverage": {"vendor": sum(r["source"] == "vendor" for r in rows),
                     "store": sum(r["source"] == "store" for r in rows),
                     "none": sum(r["source"] == "none" for r in rows)},
        "fill_costs_roundtrip_pct": costs,
        "grid_cells": [f"stop{s}%/target{t}%" for s, t in GRID],
        "mfe_mae_overall": mfe_mae_block(rows),
        "mfe_mae_by_path": {k: mfe_mae_block(v) for k, v in by(lambda r: r["path"]).items()},
        "mfe_mae_by_session": {k: mfe_mae_block(v) for k, v in by(lambda r: r["session"]).items()},
        "grid_overall": grid_block(rows),
        "grid_by_path": {k: grid_block(v) for k, v in by(lambda r: r["path"]).items()},
        "rows": rows,
    }
    json.dump(summary, open(args.out, "w"), default=str, indent=2)

    print(f"Replay study Phase 1 — {len(rows)} signals  "
          f"(vendor={summary['coverage']['vendor']} store={summary['coverage']['store']} "
          f"none={summary['coverage']['none']})")
    print("MFE/MAE %ile (overall) — LEAD METRIC:")
    for h in HORIZONS:
        m = summary["mfe_mae_overall"][h]
        if m["mfe"] and m["mae"]:
            print(f"  {h:2}m  MFE p50={m['mfe']['p50']:>6}% p75={m['mfe']['p75']:>6}% p90={m['mfe']['p90']:>6}%"
                  f"   MAE p50={m['mae']['p50']:>6}% p75={m['mae']['p75']:>6}% p90={m['mae']['p90']:>6}%  n={m['mfe']['n']}")
    print("Grid expectancy (idealized; best..worst with ambiguous bounded):")
    for s, t in GRID:
        c = summary["grid_overall"][f"s{s}_t{t}"]["idealized"]
        if c:
            print(f"  stop{s}%/target{t}%  exp {c['exp_worst_pct']:>6}%..{c['exp_best_pct']:>6}%  "
                  f"(n={c['n']}, ambiguous={c['ambiguous']})")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
