"""Rebuild a live BUY-flip cohort from local logs and bounded fill evidence."""
from __future__ import annotations

import argparse
import bisect
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def analyze(log_path: Path, fill_path: Path, reviewer_path: Path) -> dict:
    end = datetime.fromisoformat("2026-10-02T15:25:00-04:00")
    bars = {}
    for raw in log_path.open():
        row = json.loads(raw)
        line = row.get("text", "")
        if "[V2-ATR-PROBE]" not in line:
            continue
        fields = dict(re.findall(r"([a-z_]+)=([^\s]+)", line))
        observed = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
        at = datetime.fromtimestamp(int(fields["ts_ms"]) / 1000, timezone.utc)
        if at.astimezone(ET).date().isoformat() < "2026-09-08" or observed > end:
            continue
        key = fields["sym"], at
        record = {"at": at, "observed": observed, "fields": fields, "path": row["path"], "line": row["line"]}
        if key not in bars or observed < bars[key]["observed"]:
            bars[key] = record
    sells = defaultdict(list)
    fills = defaultdict(list)
    for (symbol, at), row in bars.items():
        if row["fields"].get("flip") == "SELL":
            sells[symbol].append(at)
    for times in sells.values():
        times.sort()
    fill_data = json.loads(fill_path.read_text())
    if fill_data["count"] >= fill_data["limit"]:
        raise ValueError("fill query may have been truncated")
    for row in fill_data["rows"]:
        row["at"] = datetime.fromisoformat(row["filled_at"])
        fills[row["symbol"]].append(row)
    cohort = []
    counts = Counter()
    for (symbol, at), row in sorted(bars.items(), key=lambda item: item[0][1]):
        if row["fields"].get("flip") != "BUY" or not 0 <= (row["observed"] - at).total_seconds() <= 120:
            continue
        local = at.astimezone(ET)
        time = local.strftime("%H:%M")
        session = "RTH" if "09:30" <= time < "16:00" else "PRE" if "04:00" <= time < "09:30" else "OTHER"
        counts[session] += 1
        if session != "RTH":
            continue
        index = bisect.bisect_left(sells[symbol], at)
        start = sells[symbol][index - 1] if index else None
        # Classify a traded cycle separately from the narrower at-flip outcome.
        # A later reactive confirmation is not a fill at the closing BUY bar.
        day_end = local.replace(hour=16, minute=0, second=0, microsecond=0)
        next_sell = sells[symbol][index] if index < len(sells[symbol]) else day_end
        cycle_end = min(next_sell, day_end)
        matched = [f for f in fills[symbol] if start is not None and start <= f["at"] < cycle_end]
        accounts = sorted({f["account"] for f in matched})
        cohort.append({"day": str(local.date()), "t": time, "sym": symbol, "observed": row["observed"],
                       "bar": at, "last_sell_bar": start, "cycle_end": cycle_end, "accounts": accounts,
                       "before_close_accounts": sorted({f["account"] for f in matched if f["at"] < at + timedelta(minutes=1)}),
                       "within_30s_close_accounts": sorted({f["account"] for f in matched if f["at"] < at + timedelta(seconds=90)}),
                       "filled_at": [f["filled_at"] for f in matched], "path": row["path"], "line": row["line"]})
    # Comparison happens only after rebuilding the cohort and its fill attribution.
    reviewed = {(r["day"], r["t"], r["sym"]): r for r in json.loads(reviewer_path.read_text()) if r["sess"] == "RTH"}
    own = {(r["day"], r["t"], r["sym"]): r for r in cohort}
    outcomes = Counter(tuple(r["accounts"]) for r in cohort)
    differences = []
    for key in own.keys() & reviewed.keys():
        expected = sorted(account for field, account in (("schwab", "live:schwab_1m_v2"), ("webull", "live:orb"))
                          if str(reviewed[key][field]).startswith("filled"))
        if own[key]["accounts"] != expected:
            differences.append({"key": key, "ours": own[key]["accounts"], "reviewer": expected,
                                "reviewer_labels": {f: reviewed[key][f] for f in ("schwab", "webull")}})
    return {"cutoff": end, "fill_rows": fill_data["count"], "counts": counts,
            "rth_reviewed": len(reviewed), "rth_intersection": len(own.keys() & reviewed.keys()),
            "own_only": sorted(own.keys() - reviewed.keys()), "reviewer_only": sorted(reviewed.keys() - own.keys()),
            "outcomes": {"+".join(k) or "no_fill": v for k, v in outcomes.items()},
            "fill_attribution_differences": sorted(differences, key=lambda x: x["key"]), "cohort": cohort}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", type=Path)
    parser.add_argument("fills", type=Path)
    parser.add_argument("reviewer", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.logs, args.fills, args.reviewer), default=str, indent=2))
