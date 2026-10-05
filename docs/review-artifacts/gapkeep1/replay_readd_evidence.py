"""Retrospective bar-proxy comparison, not decision-time completeness or fill P&L."""
import bisect
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail

data = json.load(open(sys.argv[1]))
daily = defaultdict(Counter)
series = {}
for day, raw in data["bars"].items():
    symbols = defaultdict(list)
    for row in raw:
        symbols[row["symbol"]].append(row)
    for symbol, rows in symbols.items():
        bars = [Bar(int(datetime.fromisoformat(r["bar_time"]).timestamp() * 1000),
                    *(float(r[k]) for k in ("open_price", "high_price", "low_price", "close_price")),
                    int(r["volume"])) for r in rows]
        series[day, symbol] = {r["ts"]: r for r in compute_atr_trail(bars)}

flips = []
probes_by_symbol = defaultdict(list)
for probe in data["probes"]:
    day, symbol = probe["day"], probe["sym"]
    probes_by_symbol[day, symbol].append(probe)
    daily[day]["fresh_age_probes"] += 1
    oracle = series.get((day, symbol), {}).get(int(probe["ts_ms"]))
    if not oracle or oracle["state"] is None:
        daily[day]["unmeasured_probes"] += 1
        continue
    daily[day]["measured_probes"] += 1
    if probe["state"] != oracle["state"]:
        daily[day]["state_mismatch"] += 1
    if probe["flip"] in {"BUY", "SELL"}:
        daily[day]["measured_flip_observations"] += 1
        if probe["flip"] != oracle["flip"]:
            daily[day]["bot_only_flips"] += 1
            flips.append({"day": day, "symbol": symbol, "probe": probe, "oracle": oracle})

placements = []
last_visible = None
ever_visible = set()
for event in sorted(data["events"], key=lambda r: r["at"]):
    day, line = event["day"], event["line"]
    if "watchlist updated" in line:
        daily[day]["watchlist_updates"] += 1
        m = re.search(r"count=(\d+) sample=([^ ]*)", line)
        current = set(filter(None, m[2].split(",")))
        if int(m[1]) > 5:
            daily[day]["truncated_watchlists"] += 1
            last_visible = None
        else:
            if last_visible is not None:
                added = current - last_visible
                daily[day]["proven_adds_lower_bound"] += len(added)
                daily[day]["proven_readds_lower_bound"] += len(added & ever_visible)
            ever_visible |= current
            last_visible = current
    if "db-seed:" in line:
        daily[day]["successful_db_seed_exposures"] += 1
    if "[V2-GAP-HOLD]" in line:
        daily[day]["recovery_holds" if "reason=recovery_gap" in line else "initial_holds"] += 1
    if "[V2-ATR-BAR-GAP]" in line:
        daily[day]["atr_bar_gap_lines"] += 1
    m = re.search(r"\[V2-RESTING-PLACE\] (\S+)", line)
    if not m:
        continue
    symbol = m[1]
    daily[day]["resting_place_attempts"] += 1
    probes = probes_by_symbol[day, symbol]
    times = [p["at"] for p in probes]
    index = bisect.bisect_right(times, event["at"]) - 1
    probe = probes[index] if index >= 0 else None
    oracle = series.get((day, symbol), {}).get(int(probe["ts_ms"])) if probe else None
    if not oracle or oracle["state"] is None or (datetime.fromisoformat(event["at"]) - datetime.fromisoformat(probe["at"])).total_seconds() > 180:
        daily[day]["unmeasured_places"] += 1
        continue
    daily[day]["measured_places"] += 1
    if oracle["state"] == "long":
        daily[day]["place_against_continuous_long"] += 1
        placements.append({"day": day, "symbol": symbol, "event": event, "probe": probe, "oracle": oracle})

reto_bars = [r for r in data["bars"].get("2026-10-05", []) if r["symbol"] == "RETO"]
late_reto = [r for r in reto_bars if "2026-10-05 14:49" <= r["bar_time"][:16] <= "2026-10-05 15:04"]
key_reto = [p for p in data["probes"] if p["sym"] == "RETO" and p["day"] == "2026-10-05" and "15:06" <= p["at"][11:16] <= "15:22"]
out = {"read_at": data["read_at"], "as_of": data["as_of"], "queries_failed": data["query_errors"],
       "daily": dict(sorted(daily.items())), "bot_only_flips": flips,
       "places_against_long": placements, "reto_late_bars": late_reto,
       "reto_probes": [{"probe": p, "oracle": series.get((p["day"], "RETO"), {}).get(int(p["ts_ms"]))} for p in key_reto],
       "reto_intents": data.get("reto_intents", [])}
print(json.dumps(out, indent=2))
