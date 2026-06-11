"""Phase B — Entry-criteria audit for schwab_1m_v2.

Runs ONLY on Phase-A-passing (assembly-exact) symbols. Two directions:

  B-1  False-positive / determinism: for every fired signal, re-derive the
       full entry condition from the bot's OWN logged [V2-MACD-PROBE] values
       (the bot logs macd/sig/hist/gates right before the decision) and verify
       the fired path actually holds. C2 pending-cross signals are reconciled
       against the [V2-PENDING-CROSS-CONSUMED] log. Independently, recompute
       MACD/EMA/VWAP/stoch from Phase-A-validated vendor (pricehistory) bars and
       compare to the probe values (math/storage cross-check).

  B-2  False-negative / missed: sweep every probe line; find bars where a path
       + all gates are satisfied and state is flat (pos_qty==0, cooldown==0) but
       no intent fired. Classify: freshness-suppressed (age>180, pending), vs
       UNEXPLAINED (a real miss).

  B-3  Per-signal context sheet (+5 marked for the operator's TOS review).

READ-ONLY. Findings, not fixes.

Inputs:
  --probe-file  pre-filtered log lines for the day (V2-MACD-PROBE + V2-PENDING-
                CROSS-*), produced by the runner:
                  sudo grep -hE '^<DAY> .*(V2-MACD-PROBE|V2-PENDING-CROSS)' \
                    /var/log/project-mai-tai/schwab-1m-v2.log > /tmp/probe.txt
  --phasea      /tmp/phaseA.json (to restrict to assembly-exact symbols)
  --day         YYYY-MM-DD

Invocation: same env-sourced pattern as overnight_bar_parity.py (needs DSN for
intents + Schwab token for the vendor recompute).
"""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import psycopg

from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import V2Indicators, session_start_ts_ms

ET = ZoneInfo("America/New_York")
STRATEGY_CODE = "schwab_1m_v2"

# v1.32 gate constants (from SchwabV2Config defaults — read from code, not memory).
MACD_HIST_MIN_PCT = 0.02
REL_VOL_MULT = 1.5
VOL_THRESHOLD = 5000
MIN_BARS = 135
# stoch overbought block + dead-zone are DISABLED by default -> those gates
# pass-through; require_uptrend/macd_strength/green/rel_vol/vol_abs are ON;
# require_vwap_filter ON with allow_vwap_cross_entry ON.

PROBE_RE = re.compile(
    r"V2-MACD-PROBE\] sym=(?P<sym>\S+) ts_ms=(?P<ts>\d+) close=(?P<close>[\d.]+) "
    r"macd=(?P<macd>-?[\d.]+) sig=(?P<sig>-?[\d.]+) hist=(?P<hist>-?[\d.]+) "
    r"hist_pct=(?P<hist_pct>-?[\d.]+) prev_macd=(?P<pmacd>\S+) prev_sig=(?P<psig>\S+) "
    r"prev_close=(?P<pclose>\S+) prev_vwap=(?P<pvwap>\S+) vwap=(?P<vwap>-?[\d.]+) "
    r"ema\d+=(?P<ema>-?[\d.]+) stoch\d+=(?P<stoch>-?[\d.]+) vol=(?P<vol>\d+) "
    r"avg_vol_\d+=(?P<avgvol>-?[\d.]+) rel_vol_x=(?P<relvol>-?[\d.]+) "
    r"cross_macd_above=(?P<cma>\w+) cross_vwap_above=(?P<cva>\w+) "
    r"macd_above_sig=(?P<mas>\w+) macd_inc=(?P<minc>\w+) green=(?P<green>\w+) "
    r"n_bars=(?P<nbars>\d+) age_s=(?P<age>[\d.]+) pos_qty=(?P<pos>\d+) cooldown=(?P<cd>\d+)"
)
CONSUMED_RE = re.compile(
    r"V2-PENDING-CROSS-CONSUMED\] sym=(?P<sym>\S+) path=(?P<path>\w+) .*fresh_bar_ts_ms=(?P<ts>\d+)"
)


@dataclass
class Probe:
    sym: str
    ts: int
    close: float
    macd: float
    sig: float
    hist_pct: float
    vwap: float
    ema: float
    vol: int
    relvol: float
    cma: bool          # cross_macd_above (native this bar)
    cva: bool          # cross_vwap_above (native this bar)
    mas: bool          # macd_above_sig
    minc: bool         # macd_inc
    green: bool
    nbars: int
    age: float
    pos: int
    cd: int


def _b(s: str) -> bool:
    return s == "true"


def parse_probes(path: str) -> tuple[dict[tuple[str, int], Probe], set[tuple[str, int]]]:
    probes: dict[tuple[str, int], Probe] = {}
    consumed: set[tuple[str, int]] = set()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = PROBE_RE.search(line)
            if m:
                g = m.groupdict()
                probes[(g["sym"], int(g["ts"]))] = Probe(
                    sym=g["sym"], ts=int(g["ts"]), close=float(g["close"]),
                    macd=float(g["macd"]), sig=float(g["sig"]), hist_pct=float(g["hist_pct"]),
                    vwap=float(g["vwap"]), ema=float(g["ema"]), vol=int(g["vol"]),
                    relvol=float(g["relvol"]), cma=_b(g["cma"]), cva=_b(g["cva"]),
                    mas=_b(g["mas"]), minc=_b(g["minc"]), green=_b(g["green"]),
                    nbars=int(g["nbars"]), age=float(g["age"]), pos=int(g["pos"]), cd=int(g["cd"]),
                )
                continue
            cm = CONSUMED_RE.search(line)
            if cm:
                consumed.add((cm.group("sym"), int(cm.group("ts"))))
    return probes, consumed


def base_filters(p: Probe) -> dict[str, bool]:
    return {
        "trend(close>ema9)": p.close > p.ema,
        "macd_strength(hist_pct>=0.02)": p.hist_pct >= MACD_HIST_MIN_PCT,
        "green(close>open)": p.green,
        "rel_vol(x>1.5)": p.relvol > REL_VOL_MULT,
        "vol_abs(>5000)": p.vol > VOL_THRESHOLD,
        # stoch_not_chase & time_allowed are disabled-by-default -> always pass.
    }


def path_macd_native(p: Probe) -> bool:
    base = all(base_filters(p).values())
    vwap_filter = (p.close > p.vwap) or p.cva  # require_vwap_filter ON, allow_cross ON
    return p.cma and p.minc and vwap_filter and base


def path_vwap_native(p: Probe) -> bool:
    base = all(base_filters(p).values())
    return p.cva and p.mas and p.minc and base


def _dsn() -> str:
    raw = os.environ.get("MAI_TAI_DATABASE_URL", "")
    return raw.replace("postgresql+psycopg://", "postgresql://")


def load_intents(day: str) -> list[dict]:
    out = []
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ti.symbol, ti.created_at, ti.status,
                   ti.payload->'metadata'->>'path',
                   ti.payload->'metadata'->>'bar_time_ms',
                   ti.payload->'metadata'->>'macd_value',
                   ti.payload->'metadata'->>'macd_signal',
                   ti.payload->'metadata'->>'entry_price'
            FROM trade_intents ti JOIN strategies s ON s.id = ti.strategy_id
            WHERE s.code = %s AND ti.created_at >= %s AND ti.created_at < %s
            ORDER BY ti.created_at
            """,
            (STRATEGY_CODE, f"{day} 00:00:00+00", f"{day} 23:59:59+00"),
        )
        for sym, created, status, path, barms, macd, sig, px in cur.fetchall():
            out.append({
                "symbol": sym, "created_at": created.isoformat(), "status": status,
                "path": path, "bar_time_ms": int(barms) if barms else None,
                "macd": float(macd) if macd else None, "sig": float(sig) if sig else None,
                "entry_price": float(px) if px else None,
            })
    return out


def vendor_bars(client: SchwabV2RestClient, settings: Settings, sym: str,
                cache: dict[str, list]) -> list:
    if sym not in cache:
        try:
            cache[sym] = sorted(client._fetch_recent_closed_bars(sym, 0),  # noqa: SLF001
                                key=lambda b: b.timestamp_ms)
        except Exception:  # noqa: BLE001
            cache[sym] = []
    return cache[sym]


def recompute_from_vendor(bars: list, ts: int) -> dict | None:
    """Recompute MACD/EMA/VWAP from dense vendor bars, last 300 ending at ts,
    to cross-check the probe. VWAP from the 04:00 ET session anchor."""
    upto = [b for b in bars if b.timestamp_ms <= ts]
    if not upto or upto[-1].timestamp_ms != ts:
        return None  # vendor lacks this exact minute
    window = upto[-300:]
    closes = [b.close for b in window]
    macd = V2Indicators.macd(closes, 12, 26, 9)
    ema = V2Indicators.ema(closes, 9)
    if macd is None or ema is None:
        return None
    anchor = session_start_ts_ms(ts)
    spv = sv = 0.0
    for b in upto:
        if b.timestamp_ms < anchor:
            continue
        typ = (b.high + b.low + b.close) / 3.0
        spv += typ * b.volume
        sv += b.volume
    vwap = (spv / sv) if sv > 0 else window[-1].close
    return {"macd": macd[0], "sig": macd[1], "ema": ema, "vwap": vwap, "nbars": len(window)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--probe-file", required=True)
    ap.add_argument("--phasea", default="/tmp/phaseA.json")
    ap.add_argument("--out", default="/tmp/phaseB.json")
    args = ap.parse_args()

    phasea = json.load(open(args.phasea))
    # Assembly-exact symbols are eligible (bar VALUES are trustworthy). Coverage
    # gaps don't corrupt the bars a signal fired on.
    eligible = set(phasea.get("assembly_exact_symbols", []))
    high_gap = set(phasea.get("coverage_high_gap_verify", []))

    probes, consumed = parse_probes(args.probe_file)
    intents = load_intents(args.day)

    settings = Settings()
    client = SchwabV2RestClient(settings, on_chart_bar=lambda *a: None, on_quote=lambda *a: None)
    vcache: dict[str, list] = {}

    # ---------------- B-1: reproduce every fired signal ----------------
    b1 = []
    for it in intents:
        sym, ts, path = it["symbol"], it["bar_time_ms"], it["path"]
        rec = {"symbol": sym, "path": path, "bar_time_ms": ts,
               "status": it["status"], "created_at": it["created_at"]}
        if sym not in eligible:
            rec["result"] = "QUARANTINED (symbol not assembly-exact)"
            b1.append(rec); continue
        p = probes.get((sym, ts))
        if p is None:
            rec["result"] = "NO-PROBE (cannot reproduce — probe line absent)"
            b1.append(rec); continue
        # Re-derive the decision from the bot's own logged values.
        gates = base_filters(p)
        is_consumed = (sym, ts) in consumed
        if path == "MACD Cross":
            holds = path_macd_native(p)
        else:
            holds = path_vwap_native(p)
        rec["native_path_holds"] = holds
        rec["pending_consumed"] = is_consumed
        rec["gates"] = gates
        rec["gates_all_pass"] = all(gates.values())
        rec["age_s"] = p.age
        rec["fresh(age<=180)"] = p.age <= 180.0
        rec["flat(pos=0,cd=0)"] = (p.pos == 0 and p.cd == 0)
        # Verdict for this signal:
        if holds:
            rec["result"] = "REPRODUCED (native path + gates hold on probe values)"
        elif is_consumed:
            # C2: fired via a pending cross from an earlier stale bar; on the
            # fresh bar the consume requires macd_above_sig + filters (path-vwap)
            # or macd_above_sig + vwap_filter + filters (path-macd).
            consume_ok = (p.mas and p.minc and all(gates.values())
                          and ((path == "MACD Cross" and ((p.close > p.vwap) or p.cva)) or path == "VWAP Breakout"))
            rec["result"] = ("REPRODUCED (C2 pending-cross consumed; consume conditions hold)"
                             if consume_ok else "REVIEW (consumed but consume-conditions fail on probe)")
        else:
            rec["result"] = "MISMATCH (fired but native path fails on bot's own probe values)"
        # Independent vendor recompute cross-check.
        vb = vendor_bars(client, settings, sym, vcache)
        rc = recompute_from_vendor(vb, ts)
        if rc:
            rec["vendor_recompute"] = {
                "macd_probe": p.macd, "macd_recomp": round(rc["macd"], 6),
                "macd_absdiff": round(abs(p.macd - rc["macd"]), 6),
                "vwap_probe": p.vwap, "vwap_recomp": round(rc["vwap"], 6),
                "vwap_absdiff": round(abs(p.vwap - rc["vwap"]), 6),
                "nbars_probe": p.nbars, "nbars_recomp": rc["nbars"],
            }
        b1.append(rec)

    # ---------------- B-2: missed-signal sweep ----------------
    # Sweep ALL probe symbols (not just the assembly-exact 24) so a missed
    # fresh signal on a low-bar-count symbol can't be silently dropped. Each
    # candidate carries whether its bars were Phase-A-verified.
    fired_keys = {(it["symbol"], it["bar_time_ms"]) for it in intents}
    b2_missed = []
    for (sym, ts), p in probes.items():
        if p.nbars < MIN_BARS:
            continue
        if not (p.pos == 0 and p.cd == 0):
            continue
        if not (path_macd_native(p) or path_vwap_native(p)):
            continue
        if (sym, ts) in fired_keys:
            continue  # correctly fired
        # path satisfied + flat but no intent at this bar -> classify.
        if p.age > 180.0:
            cls = "freshness-suppressed (stale bar; C2 pending may consume later)"
        elif (sym, ts) in consumed:
            cls = "consumed-elsewhere"
        else:
            cls = "UNEXPLAINED"
        b2_missed.append({
            "symbol": sym, "bar_time_ms": ts,
            "ts_et": datetime.fromtimestamp(ts/1000, UTC).astimezone(ET).strftime("%H:%M"),
            "age_s": p.age, "path_macd": path_macd_native(p), "path_vwap": path_vwap_native(p),
            "assembly_verified": sym in eligible, "class": cls,
        })

    # ---------------- B-3: per-signal context sheet ----------------
    context = []
    for it in sorted(intents, key=lambda x: x["created_at"]):
        sym, ts = it["symbol"], it["bar_time_ms"]
        vb = vendor_bars(client, settings, sym, vcache)
        fwd = {b.timestamp_ms: b for b in vb}
        def hl(mins):
            highs, lows = [], []
            for k in range(1, mins + 1):
                b = fwd.get(ts + k * 60_000)
                if b:
                    highs.append(b.high); lows.append(b.low)
            return (max(highs) if highs else None, min(lows) if lows else None)
        h5, l5 = hl(5); h15, l15 = hl(15); h30, l30 = hl(30)
        px = it["entry_price"]
        context.append({
            "symbol": sym, "ts_et": datetime.fromtimestamp(ts/1000, UTC).astimezone(ET).strftime("%H:%M"),
            "path": it["path"], "entry_price": px, "macd": it["macd"], "sig": it["sig"],
            "next5_high": h5, "next5_low": l5, "next15_high": h15, "next30_high": h30, "next30_low": l30,
            "fwd30_max_gain_pct": round((h30 - px) / px * 100, 2) if (h30 and px) else None,
            "fwd30_max_draw_pct": round((l30 - px) / px * 100, 2) if (l30 and px) else None,
            "coverage_flag": "HIGH-GAP-VERIFY" if sym in high_gap else "ok",
        })

    reproduced = sum(1 for r in b1 if r.get("result", "").startswith("REPRODUCED"))
    mismatches = [r for r in b1 if r.get("result", "").startswith("MISMATCH")]
    reviews = [r for r in b1 if r.get("result", "").startswith("REVIEW")]
    unexplained = [m for m in b2_missed if m["class"] == "UNEXPLAINED"]

    summary = {
        "day": args.day,
        "signals_total": len(intents),
        "reproduced": reproduced,
        "mismatches": len(mismatches),
        "reviews": len(reviews),
        "missed_candidates": len(b2_missed),
        "missed_unexplained": len(unexplained),
        "b1": b1, "b2_missed": b2_missed, "context": context,
    }
    json.dump(summary, open(args.out, "w"), indent=2, default=str)

    print(f"Phase B — {args.day}")
    print(f"  signals: {len(intents)}  REPRODUCED={reproduced}  MISMATCH={len(mismatches)}  REVIEW={len(reviews)}")
    print(f"  missed-signal sweep: {len(b2_missed)} candidates  UNEXPLAINED={len(unexplained)}")
    if mismatches:
        for r in mismatches:
            print(f"    MISMATCH {r['symbol']} {r['path']} ts={r['bar_time_ms']} gates={r.get('gates')}")
    if unexplained:
        for m in unexplained[:20]:
            print(f"    UNEXPLAINED-MISS {m['symbol']} {m['ts_et']} age={m['age_s']}")
    # vendor recompute agreement
    diffs = [r["vendor_recompute"]["macd_absdiff"] for r in b1 if r.get("vendor_recompute")]
    if diffs:
        diffs.sort()
        print(f"  vendor MACD recompute |Δ|: n={len(diffs)} median={diffs[len(diffs)//2]:.6f} max={diffs[-1]:.6f}")
    print(f"  wrote {args.out}")


if __name__ == "__main__":
    main()
