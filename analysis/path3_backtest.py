"""Path 3 — Phase 1 backtest (scalp lens). Read-only, no production code.

Per docs/path3-atr-flip-plan.md + the operator's scalp objective. For every
hypothetical Path-3 entry (4 variants) AND the existing Path 1/2 signals
(baseline), measures the **scalp headline: P(reach +10% before the stop is hit)**
for stops {3%, 5%, 10%}, plus time-to-+10% (minutes) and MFE/MAE (secondary).

Variants (entry from the ATR-flip series; forward bars = strictly after the entry
bar, scored to ET-session close):
  A confirmed flip   — BUY flip bar; entry = its close
  B intrabar touch   — first bar (while short) whose HIGH ≥ prior trail; entry = that trail level
  C proximity X%     — first bar (while short) whose close enters [trail×(1−X), trail); entry = close  (X ∈ 0.5/1/2)
  D flip+continuation— A's flip AND next bar closes > the flip trail; entry = next bar close

Both RAW and RAW+liquidity-floor (entry-bar vol > 5000) are reported. Ambiguous
(one candle spans target AND stop) is bounded, never point-estimated.

⚠️ DIRECTIONAL, NOT STATISTICAL — per-cell N is small.
⚠️ COST WARNING: at a +10% target, a 1–2% round-trip cost is 10–20% of gross per
trade. Scalping is the cost-sensitive profile; the Phase-2 measured-spread upgrade
is decisive for it. These are idealized (fill at entry ref, no slippage).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import UTC, datetime

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atr_flip import ET, Bar, compute_atr_trail, fetch_day  # noqa: E402

from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient  # noqa: E402
from project_mai_tai.settings import Settings  # noqa: E402

STOPS = (3, 5, 10)
TARGET = 10
VOL_FLOOR = 5000


def _dsn() -> str:
    return os.environ["MAI_TAI_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")


def et_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).astimezone(ET).strftime("%Y-%m-%d")


def load_path12(conn) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT ti.symbol, (ti.payload->'metadata'->>'bar_time_ms')::bigint,
                      (ti.payload->'metadata'->>'entry_price')::numeric,
                      ti.payload->'metadata'->>'path'
               FROM trade_intents ti JOIN strategies s ON s.id=ti.strategy_id
               WHERE s.code='schwab_1m_v2'
                 AND ti.payload->'metadata'->>'bar_time_ms' IS NOT NULL
                 AND ti.payload->'metadata'->>'entry_price' IS NOT NULL""")
        for sym, bar_ms, entry, path in cur.fetchall():
            out.setdefault((sym, et_date(int(bar_ms))), []).append(
                {"bar_ms": int(bar_ms), "entry": float(entry), "path": path or "?"})
    return out


def score_scalp(bars: list[Bar], entry_idx: int, entry_price: float) -> dict:
    """Race +10% vs each stop over forward bars (strictly after entry_idx) to
    session close. Returns per-stop outcome + time-to-target + MFE/MAE."""
    fwd = bars[entry_idx + 1:]
    target = entry_price * (1 + TARGET / 100)
    res: dict = {"fwd_bars": len(fwd)}
    if fwd:
        res["mfe"] = round((max(b.high for b in fwd) - entry_price) / entry_price * 100, 3)
        res["mae"] = round((entry_price - min(b.low for b in fwd)) / entry_price * 100, 3)
    else:
        res["mfe"] = res["mae"] = None
    for s in STOPS:
        stop = entry_price * (1 - s / 100)
        outcome, tmin = "censored", None
        for k, b in enumerate(fwd, start=1):
            ht, hs = b.high >= target, b.low <= stop
            if ht and hs:
                outcome, tmin = "ambiguous", k
                break
            if ht:
                outcome, tmin = "target", k
                break
            if hs:
                outcome = "stop"
                break
        res[s] = {"outcome": outcome, "t": tmin if outcome == "target" else None}
    return res


# ----------------------------- variant extraction --------------------------

def _short_segments(rows: list[dict]) -> list[tuple[int, int]]:
    """(start, end) inclusive bar indices of each maximal short-state run."""
    segs, i, n = [], 0, len(rows)
    while i < n:
        if rows[i]["state"] == "short":
            j = i
            while j + 1 < n and rows[j + 1]["state"] == "short":
                j += 1
            segs.append((i, j))
            i = j + 1
        else:
            i += 1
    return segs


def extract_signals(bars: list[Bar], rows: list[dict], variant: str, prox: float | None = None):
    """Return list of (entry_idx, entry_price)."""
    n = len(bars)
    out: list[tuple[int, float]] = []
    if variant == "A":
        for i in range(n):
            if rows[i]["flip"] == "BUY":
                out.append((i, bars[i].close))
    elif variant == "D":
        for i in range(n):
            if rows[i]["flip"] == "BUY" and i + 1 < n and rows[i]["trail"] is not None \
               and bars[i + 1].close > rows[i]["trail"]:
                out.append((i + 1, bars[i + 1].close))
    elif variant == "B":
        for (s, e) in _short_segments(rows):
            for i in range(s + 1, e + 2):           # bars while prior state is short
                if i >= n:
                    break
                tp = rows[i - 1]["trail"]
                if tp is not None and bars[i].high >= tp:
                    out.append((i, tp))             # first touch of the resting level
                    break
    elif variant == "C":
        assert prox is not None
        for (s, e) in _short_segments(rows):
            for i in range(s + 1, e + 2):
                if i >= n:
                    break
                tp = rows[i - 1]["trail"]
                if tp is None:
                    continue
                if tp * (1 - prox / 100) <= bars[i].close < tp:   # entered the band from below
                    out.append((i, bars[i].close))
                    break
    return out


def c_conversion(bars: list[Bar], rows: list[dict], prox: float) -> dict:
    """Of first-approach events into the band per short segment, fraction that
    flipped to long within 1 / 3 / 5 bars vs rejected."""
    within = {1: 0, 3: 0, 5: 0}
    total = 0
    n = len(bars)
    for (s, e) in _short_segments(rows):
        appr_idx = None
        for i in range(s + 1, e + 2):
            if i >= n:
                break
            tp = rows[i - 1]["trail"]
            if tp is not None and tp * (1 - prox / 100) <= bars[i].close < tp:
                appr_idx = i
                break
        if appr_idx is None:
            continue
        total += 1
        # find the flip (BUY) index in/after this segment
        flip_idx = next((k for k in range(appr_idx, min(e + 3, n)) if rows[k]["flip"] == "BUY"), None)
        if flip_idx is not None:
            d = flip_idx - appr_idx
            for w in (1, 3, 5):
                if d <= w:
                    within[w] += 1
    return {"approaches": total,
            "crossed_within": within,
            "rejected_5": total - within[5]}


# ----------------------------- aggregation ---------------------------------

def agg_cell(scored: list[dict]) -> dict:
    """scored = list of score_scalp() results. Per stop: P(+10% before stop)
    bounded by ambiguous; plus censored rate + time-to-target."""
    out = {"n": len(scored)}
    for s in STOPS:
        c = {"target": 0, "stop": 0, "ambiguous": 0, "censored": 0}
        for r in scored:
            c[r[s]["outcome"]] += 1
        n = max(1, len(scored))
        out[f"stop{s}"] = {
            **c,
            "P_target_before_stop_worst": round(c["target"] / n, 3),
            "P_target_before_stop_best": round((c["target"] + c["ambiguous"]) / n, 3),
        }
    # time-to-target is stop-independent (target-hit time); collect from any stop's record
    tts = [r[s]["t"] for r in scored for s in STOPS if r[s]["outcome"] == "target" and r[s]["t"]]
    tts = sorted(set(tts)) if tts else []
    out["time_to_target_min"] = ({"n": len(tts), "p25": tts[len(tts)//4], "p50": tts[len(tts)//2],
                                  "p75": tts[(3*len(tts))//4]} if tts else None)
    mfes = sorted(r["mfe"] for r in scored if r["mfe"] is not None)
    maes = sorted(r["mae"] for r in scored if r["mae"] is not None)
    out["mfe_p50"] = mfes[len(mfes)//2] if mfes else None
    out["mae_p50"] = maes[len(maes)//2] if maes else None
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/path3_backtest.json")
    args = ap.parse_args()
    settings = Settings()
    client = SchwabV2RestClient(settings, on_chart_bar=lambda *a: None, on_quote=lambda *a: None)

    with psycopg.connect(_dsn()) as conn:
        path12 = load_path12(conn)
    universe = sorted(path12.keys())
    print(f"universe: {len(universe)} symbol-days (from Path 1/2 signals)")

    variants = {"A": (None,), "B": (None,), "C": (0.5, 1, 2), "D": (None,)}
    # collect per-variant-cell scored lists, raw + floored
    cells: dict[str, dict[str, list[dict]]] = {}
    def key(v, p): return v if p is None else f"C{p}"
    for v, ps in variants.items():
        for p in ps:
            cells[key(v, p)] = {"raw": [], "floor": []}
    p12_scored = {"raw": [], "floor": [], "by_path": {}}
    cconv = {0.5: {"approaches": 0, "crossed_within": {1: 0, 3: 0, 5: 0}, "rejected_5": 0},
             1: {"approaches": 0, "crossed_within": {1: 0, 3: 0, 5: 0}, "rejected_5": 0},
             2: {"approaches": 0, "crossed_within": {1: 0, 3: 0, 5: 0}, "rejected_5": 0}}

    bcache: dict[tuple[str, str], list[Bar]] = {}
    for (sym, day) in universe:
        if (sym, day) not in bcache:
            try:
                bcache[(sym, day)] = fetch_day(client, settings, sym, day)
            except Exception:
                bcache[(sym, day)] = []
        bars = bcache[(sym, day)]
        if len(bars) < 12:
            continue
        rows = compute_atr_trail(bars)
        bidx = {b.ts: i for i, b in enumerate(bars)}
        # Path-3 variants
        for v, ps in variants.items():
            for p in ps:
                for (ei, ep) in extract_signals(bars, rows, v, p):
                    sc = score_scalp(bars, ei, ep)
                    cells[key(v, p)]["raw"].append(sc)
                    if bars[ei].volume > VOL_FLOOR:
                        cells[key(v, p)]["floor"].append(sc)
        for p in (0.5, 1, 2):
            cc = c_conversion(bars, rows, p)
            cconv[p]["approaches"] += cc["approaches"]
            for w in (1, 3, 5):
                cconv[p]["crossed_within"][w] += cc["crossed_within"][w]
            cconv[p]["rejected_5"] += cc["rejected_5"]
        # Path 1/2 baseline (match the signal bar in this session)
        for sig in path12[(sym, day)]:
            i = bidx.get(sig["bar_ms"])
            if i is None:
                continue
            sc = score_scalp(bars, i, sig["entry"])
            p12_scored["raw"].append(sc)
            if bars[i].volume > VOL_FLOOR:
                p12_scored["floor"].append(sc)
            p12_scored["by_path"].setdefault(sig["path"], []).append(sc)

    report = {
        "target_pct": TARGET, "stops": list(STOPS),
        "path3": {k: {"raw": agg_cell(c["raw"]), "floor": agg_cell(c["floor"])} for k, c in cells.items()},
        "c_conversion": cconv,
        "path12_baseline": {"raw": agg_cell(p12_scored["raw"]), "floor": agg_cell(p12_scored["floor"]),
                            "by_path": {p: agg_cell(v) for p, v in p12_scored["by_path"].items()}},
    }
    json.dump(report, open(args.out, "w"), default=str, indent=2)

    def line(name, cell):
        s5 = cell[f"stop{5}"]
        tt = cell["time_to_target_min"]
        print(f"  {name:14} n={cell['n']:<5} stop5: P(+10%<stop)={s5['P_target_before_stop_worst']}.."
              f"{s5['P_target_before_stop_best']} (T{s5['target']}/S{s5['stop']}/A{s5['ambiguous']}/C{s5['censored']})"
              f"  t2tgt p50={tt['p50'] if tt else '-'}m  MFE/MAE p50={cell['mfe_p50']}/{cell['mae_p50']}")
    print(f"\n=== PATH 3 variants — P(reach +10% before stop), stop=5% shown (RAW) ===")
    for k in cells:
        line(k + " raw", report["path3"][k]["raw"])
    print("=== same, liquidity-floored (vol>5000 entry bar) ===")
    for k in cells:
        line(k + " floor", report["path3"][k]["floor"])
    print("=== Path 1/2 baseline ===")
    line("P1/2 raw", report["path12_baseline"]["raw"])
    line("P1/2 floor", report["path12_baseline"]["floor"])
    for p, v in report["path12_baseline"]["by_path"].items():
        line(p, v)
    print("=== Variant-C conversion (approach → crossed within N bars) ===")
    for p in (0.5, 1, 2):
        cc = cconv[p]
        a = max(1, cc["approaches"])
        print(f"  C{p}%: approaches={cc['approaches']}  within1={cc['crossed_within'][1]} "
              f"({cc['crossed_within'][1]/a:.0%})  within3={cc['crossed_within'][3]} ({cc['crossed_within'][3]/a:.0%})  "
              f"within5={cc['crossed_within'][5]} ({cc['crossed_within'][5]/a:.0%})  rejected={cc['rejected_5']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
