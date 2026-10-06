#!/usr/bin/env python3
"""Reviewer-owned acceptance runner for LINE=CHART Restoration (#1097).

For every recorded restoration-class event since 2026-09-28 (watchlist re-add with a stored hole,
gap hold) it replays the symbol's stored bars through the REAL strategy + bot service via
tests.line_restore_acceptance_factory, acting as the history provider, and compares the admitted
line with ONE continuous line (backtest.atr_oracle.compute_atr_trail) over the same stored bars.

Pass mark (pre-registered 2026-10-06 09:00 ET): every re-add / bars-missing case ADMITTED with
state == oracle state and |trail - oracle trail| <= 1e-6 at the first live bar after the event AND
10 bars later; buy_count == 0 while incomplete. Real market pauses are reported, not scored.
Inputs: --bars CSV (strategy_bar_history export, UTC) --events (v2 log extract, UTC stamps).
"""
from __future__ import annotations
import argparse, asyncio, bisect, collections, csv, json, re, sys
from datetime import datetime, timedelta, timezone, UTC
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

def ms(iso): return int(datetime.fromisoformat(iso).timestamp() * 1000)
def et(ms_): return datetime.fromtimestamp(ms_/1000, UTC).astimezone(ET).strftime("%m-%d %H:%M:%S")
def et_date(ms_): return datetime.fromtimestamp(ms_/1000, UTC).astimezone(ET).date().isoformat()

def load_bars(path):
    out = collections.defaultdict(list)
    with open(path) as fh:
        for r in csv.DictReader(fh):
            bt = ms(r["bar_time"])
            out[(r["symbol"], et_date(bt))].append({
                "symbol": r["symbol"], "bar_time": r["bar_time"], "open_price": r["open_price"],
                "high_price": r["high_price"], "low_price": r["low_price"], "close_price": r["close_price"],
                "volume": r["volume"], "source": r["source"], "created_ms": ms(r["created_at"]), "ts": bt})
    for k in out: out[k].sort(key=lambda x: x["ts"])
    return out

LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) \[(V2-[A-Z-]+)\] (.*)$")
def load_events(path):
    adds = collections.defaultdict(list); holds = collections.defaultdict(list); probes = collections.defaultdict(dict); removes = collections.defaultdict(list)
    with open(path, errors="ignore") as fh:
        for line in fh:
            m = LINE.match(line.strip())
            if not m: continue
            t = int(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()*1000) + int(m.group(2))
            tag, rest = m.group(3), m.group(4)
            if tag == "V2-WS-SUB":
                cmd = re.search(r"cmd=(\w+)", rest).group(1); sample = re.search(r"sample=([A-Z,]+)", rest)
                for s in (sample.group(1).split(",") if sample else []):
                    (adds if cmd in ("ADD", "SUBS") else removes)[(s, et_date(t))].append(t)
            elif tag == "V2-GAP-HOLD":
                mm = re.search(r"last_bar_age_s=([\d.]+)", rest)
                if mm:
                    holds[(rest.split()[0], et_date(t))].append((t, float(mm.group(1))))
            elif tag == "V2-ATR-PROBE":
                mm = re.search(r"sym=(\w+) ts_ms=(\d+) .*state=(\w+) age=(\d+)", rest); tr = re.search(r"trail=([\d.]+)", rest)
                if mm and tr: probes[(mm.group(1), et_date(int(mm.group(2))))][int(mm.group(2))] = (mm.group(3), float(tr.group(1)), int(mm.group(4)), t)
    return adds, removes, holds, probes

def oracle_rows(bars):
    from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
    return compute_atr_trail([Bar(b["ts"], float(b["open_price"]), float(b["high_price"]), float(b["low_price"]), float(b["close_price"]), int(b["volume"])) for b in bars])

def enumerate_cases(bars, adds, holds):
    cases = []
    for key, rows in bars.items():
        sym, day = key
        tss = [r["ts"] for r in rows]
        for t in sorted(adds.get(key, [])):
            before = [r for r in rows if r["created_ms"] <= t]
            after = [r for r in rows if r["ts"] >= t - 60_000]
            if not before or not after: continue
            hole = (after[0]["ts"] - before[-1]["ts"]) if before[-1]["ts"] < after[0]["ts"] else 0
            if hole > 90_000:
                cases.append(dict(kind="readd", symbol=sym, day=day, t=t, hole_min=round(hole/60000, 1)))
        for t, age in holds.get(key, []):
            i = bisect.bisect_left(tss, t - 60_000)
            prev_live = [r for r in rows if r["ts"] < t - 60_000 and r["created_ms"] <= t]
            gap_bars = [r for r in rows if prev_live and prev_live[-1]["ts"] < r["ts"] < t - 60_000]
            kind = "hold_bars_missing" if any(r["source"] != "live" or r["created_ms"] > t for r in gap_bars) else ("hold_pause" if not gap_bars else "hold_unknown")
            cases.append(dict(kind=kind, symbol=sym, day=day, t=t, hole_min=round(age/60, 1)))
    # a hold detected within 5 s of a re-add is the same episode: keep the re-add row, note the hold
    out = []
    for c in sorted(cases, key=lambda c: (c["day"], c["t"], c["kind"] != "readd")):
        if c["kind"].startswith("hold") and out and out[-1]["symbol"] == c["symbol"] and abs(out[-1]["t"] - c["t"]) <= 5000 and out[-1]["kind"] == "readd":
            out[-1]["kind"] = "readd+hold"; continue
        out.append(c)
    return out

async def run_case(c, rows, probes, period, factor):
    from tests.line_restore_acceptance_factory import make_line_restore_case
    from project_mai_tai.strategy_core.session_line_restore import SessionCoverage, history_fingerprint
    sym, t = c["symbol"], c["t"]
    pre = [r for r in rows if r["created_ms"] <= t and r["ts"] + 60_000 <= t]
    pre_ts = {r["ts"] for r in pre}
    # everything else arrives in availability order: live bars when they close, REST/backfill rows when written
    post = [r for r in rows if r["ts"] not in pre_ts]
    post.sort(key=lambda r: (r["ts"] + 61_000) if r["source"] == "live" else max(r["created_ms"], r["ts"] + 61_000))
    case = make_line_restore_case(symbol=sym, now_ms=t)
    fed = []
    for r in pre:
        await case.feed_bar(r, now_ms=t, phase="replay", source="callback"); fed.append(r)
    if c["kind"].startswith("hold"):
        case.hold(detected_at_ms=t, last_bar_age_s=c["hole_min"]*60, last_print_age_s=0.3)
    results = {"first": None, "plus10": None}
    live_count = 0; attested = False; buy_before_complete = 0; incomplete_reasons = collections.Counter()
    for r in post:
        now = max(r["ts"] + 61_000, t + 1)
        src = "rest" if r["source"] != "live" else "streamer"
        if src == "rest": now = max(now, r["created_ms"])
        await case.feed_bar(r, now_ms=now, phase="live", source=src); fed.append(r)
        known = sorted({x["ts"]: x for x in fed}.values(), key=lambda x: x["ts"])
        ledger = case.bot._line_sessions.get(sym)
        if ledger is not None:
            bars = [case.bar(x) for x in known]
            case.attest(SessionCoverage("schwab_rest_full_session", ledger.anchor_ms, r["ts"] + 60_000,
                                        tuple(b.timestamp_ms for b in bars), True, history_fingerprint(bars), prefix_complete=True))
            await case.rebuild(now_ms=now)
        snap = case.snapshot()
        if src == "streamer":
            live_count += 1
            if snap["incomplete_reason"]: incomplete_reasons[snap["incomplete_reason"]] += 1
            if snap["incomplete_reason"] and snap["buy_count"]: buy_before_complete = snap["buy_count"]
            if live_count in (1, 11):
                orc = oracle_rows([x for x in rows if x["ts"] <= r["ts"]])
                o = orc[-1] if orc else {}
                rec = probes.get((sym, c["day"]), {}).get(r["ts"])
                gaps = [(et(a["ts"]), round((b["ts"]-a["ts"])/60000)) for a, b in zip(known, known[1:]) if b["ts"] - a["ts"] > 60_000]
                results["first" if live_count == 1 else "plus10"] = dict(gaps=gaps[:6], n_known=len(known), anchor=et(ledger.anchor_ms) if ledger else None,
                    bar=et(r["ts"]), admitted=not snap["incomplete_reason"], reason=snap["incomplete_reason"],
                    state=snap["state"], trail=round(snap["trail"] or 0, 4), oracle_state=o.get("state"), oracle_trail=round(o.get("trail") or 0, 4),
                    recorded_state=rec[0] if rec else None, recorded_trail=rec[1] if rec else None, entry_allowed=snap["entry_allowed"], buy_count=snap["buy_count"])
            if live_count >= 11: break
    return dict(c, et=et(t), results=results, buy_before_complete=buy_before_complete, incomplete=dict(incomplete_reasons))

def verdict(r):
    out = []
    for k in ("first", "plus10"):
        x = r["results"][k]
        if not x: out.append("n/a"); continue
        if not x["admitted"]: out.append(f"HELD({x['reason']})"); continue
        ok = x["state"] == x["oracle_state"] and abs((x["trail"] or 0) - (x["oracle_trail"] or 0)) <= 1e-6
        out.append("MATCH" if ok else f"MISMATCH(code {x['state']}/{x['trail']} vs oracle {x['oracle_state']}/{x['oracle_trail']})")
    return out

async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--bars", required=True); ap.add_argument("--events", required=True)
    ap.add_argument("--only", default=""); ap.add_argument("--json", default=""); ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    bars = load_bars(a.bars); adds, removes, holds, probes = load_events(a.events)
    cases = enumerate_cases(bars, adds, holds)
    if a.only: cases = [c for c in cases if f"{c['symbol']} {c['day']}" .startswith(a.only) or c["symbol"] == a.only]
    if a.limit: cases = cases[:a.limit]
    print(f"cases: {len(cases)}  by kind: {dict(collections.Counter(c['kind'] for c in cases))}")
    out = []; tally = collections.Counter()
    for c in cases:
        try:
            r = await run_case(c, bars[(c["symbol"], c["day"])], probes, 5, 3.5)
        except Exception as e:  # noqa: BLE001
            r = dict(c, et=et(c["t"]), error=f"{type(e).__name__}: {e}", results={"first": None, "plus10": None}, buy_before_complete=0, incomplete={})
        v = verdict(r) if "error" not in r else ["ERROR", r["error"][:80]]
        rec = r["results"]["first"] or {}
        before = "" if not rec.get("recorded_state") else ("rec=" + ("same" if rec["recorded_state"] == rec["oracle_state"] else f"{rec['recorded_state']}≠{rec['oracle_state']}"))
        tally[(c["kind"], "first:" + v[0].split("(")[0])] += 1
        tally[(c["kind"], "plus10:" + v[1].split("(")[0])] += 1
        rec10 = r["results"]["plus10"] or {}
        if rec10.get("recorded_state") and rec10.get("oracle_state"):
            tally[(c["kind"], "installed_bot_at_plus10:" + ("same_as_oracle" if rec10["recorded_state"] == rec10["oracle_state"] and abs((rec10["recorded_trail"] or 0) - (rec10["oracle_trail"] or 0)) < 1e-3 else "DIFFERS"))] += 1
        print(f"{r['et']} {c['symbol']:5} {c['kind']:17} hole={c['hole_min']:>5}m first={v[0]:<40} +10={v[1]:<28} buys_while_incomplete={r['buy_before_complete']} {before}")
        out.append(dict(r, verdict=v))
    print("\nTALLY:", json.dumps({f"{k[0]}:{k[1]}": n for k, n in sorted(tally.items())}, indent=0))
    if a.json: json.dump(out, open(a.json, "w"), indent=1, default=str)

if __name__ == "__main__":
    asyncio.run(main())
