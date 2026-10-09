"""Offline L4 measurements only; strict parity failures are not trading authorization."""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from statistics import median
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail  # noqa: E402

ET = ZoneInfo("America/New_York")
FIELDS = ("o", "h", "l", "c", "v")
METHOD = {
    "window": "07:00 <= bar-start < 08:00 America/New_York, each recorded day",
    "percent": "100 * abs(Schwab - Massive) / abs(Massive), per field/bar",
    "zero_reference": "both zero: 0%; Massive zero and Schwab nonzero: Infinity",
    "ohlc_exact": "count of paired bars with all four decimal OHLC fields equal",
    "volume_percent": "median/max of paired-bar absolute differences; direction counted separately",
    "impact": "pure pinned ATR oracle at exactly 07:09; identical Massive 04:00-06:59 prefix; "
              "replace only overlapping Schwab >=07:00 OHLC, preserving Schwab timestamps; "
              "volume forced to zero in BOTH math streams (oracle ignores volume)",
    "missing_target": "UNMEASURED, with last-available snapshots separately disclosed; no fallback PASS",
    "line_level": "exact equality of pinned oracle's 4-decimal trail; no additional tolerance",
    "scope": "pure oracle counterfactual, NOT runtime buffer/VWAP/volume isolation or entry acceptance",
    "exit_codes": "0: all exact source + seeded target comparisons PASS; "
                  "1: any mismatch/unmeasured/unseeded; 2: malformed row (all rows still reported)",
}


def number(value):
    value = Decimal(str(value))
    if not value.is_finite() or value < 0:
        raise ValueError("nonfinite or negative provider value")
    return value


def percent(schwab, massive):
    if massive == 0:
        return Decimal(0) if schwab == 0 else Decimal("Infinity")
    return abs(schwab - massive) / abs(massive) * 100


def index(bars, *, schwab=False):
    result = {}
    for bar in bars:
        raw = bar["datetime" if schwab else "t"]
        stamp = int(raw)
        if isinstance(raw, bool) or number(raw) != stamp or stamp % 60_000:
            raise ValueError("invalid/non-minute bar timestamp")
        if stamp in result:
            raise ValueError(f"duplicate timestamp {stamp}")
        names = ("open", "high", "low", "close", "volume") if schwab else FIELDS
        result[stamp] = {key: number(bar[name]) for key, name in zip(FIELDS, names)}
    return result


def snapshot(row):
    return None if row is None else {"timestamp_ms": row["ts"], "state": row["state"],
                                   "level": row["trail"], "age": row["state_age"]}


def massive_rows(payload):
    if "results" in payload:
        return payload["results"]
    if payload.get("status") == "OK" and payload.get("resultsCount") == 0:
        return []  # Recorded NXTS/HKIT explicitly report a complete empty preopen result.
    raise ValueError("Massive results absent without a proven empty response")


def line_impact(prefix, schwab, massive, boundary):
    target = boundary + 9 * 60_000
    baseline, replaced = [], []
    replaced_ids = []
    for stamp in sorted(prefix):
        if boundary - 3 * 3_600_000 <= stamp < boundary:
            bar = Bar(stamp, *(float(prefix[stamp][k]) for k in FIELDS[:4]), 0)
            baseline.append(bar)
            replaced.append(bar)
    for stamp in sorted(schwab):
        if boundary <= stamp <= target:
            original = schwab[stamp]
            substitution = massive.get(stamp, original)
            baseline.append(Bar(stamp, *(float(original[k]) for k in FIELDS[:4]), 0))
            replaced.append(Bar(stamp, *(float(substitution[k]) for k in FIELDS[:4]), 0))
            if stamp in massive:
                replaced_ids.append(stamp)
    left, right = compute_atr_trail(baseline), compute_atr_trail(replaced)
    before = snapshot(next((bar for bar in left if bar["ts"] == target), None))
    after = snapshot(next((bar for bar in right if bar["ts"] == target), None))
    changed = [key for key in ("state", "level", "age")
               if before is not None and after is not None and before[key] != after[key]]
    result = ("UNMEASURED" if before is None or after is None else
              "UNSEEDED" if before["state"] is None or after["state"] is None else
              "FAIL" if changed else "PASS")
    return {"target_ms": target, "baseline": before, "replaced": after,
            "result": result, "changed_fields": changed, "replaced_timestamps_ms": replaced_ids,
            "baseline_timestamps_ms": [b.ts for b in baseline],
            "replaced_stream_timestamps_ms": [b.ts for b in replaced],
            "last_available_baseline": snapshot(left[-1]) if left else None,
            "last_available_replaced": snapshot(right[-1]) if right else None}


def analyze(row):
    boundary = int(datetime.fromisoformat(row["day"] + "T07:00:00").replace(tzinfo=ET)
                   .timestamp() * 1000)
    if row.get("error"):
        raise ValueError(str(row["error"]))
    if row.get("boundary_ms", boundary) != boundary:
        raise ValueError("recorded boundary disagrees with day 07:00 ET")
    schwab = index(row["schwab"]["candles"], schwab=True)
    # Recorded 25-row fixture calls the Massive chart payload simply `chart`.
    chart = row["chart"] if "chart" in row else row["massive"]["chart"]
    massive = index(massive_rows(chart))
    prefix = index(massive_rows(row["preopen"]))
    if prefix != {t: b for t, b in massive.items()
                  if boundary - 3 * 3_600_000 <= t < boundary}:
        raise ValueError("Massive seed/chart prefixes disagree")
    s = {t for t in schwab if boundary <= t < boundary + 3_600_000}
    m = {t for t in massive if boundary <= t < boundary + 3_600_000}
    common = sorted(s & m)
    diffs, ohlc_percent, volume_percent = [], [], []
    exact = {key: 0 for key in FIELDS}
    ohlc_exact = 0
    direction = {"massive_greater": 0, "schwab_greater": 0, "equal": 0}
    for stamp in sorted(s | m):
        if stamp not in s or stamp not in m:
            provider = "Massive" if stamp in m else "Schwab"
            values = massive[stamp] if stamp in m else schwab[stamp]
            diffs.append({"timestamp_ms": stamp, "kind": "only_" + provider,
                          "values": {k: str(v) for k, v in values.items()}})
            continue
        changes = {}
        for key in FIELDS:
            left, right = schwab[stamp][key], massive[stamp][key]
            pct = percent(left, right)
            (volume_percent if key == "v" else ohlc_percent).append(pct)
            exact[key] += left == right
            if left != right:
                changes[key] = {"Schwab": str(left), "Massive": str(right),
                                "abs_diff_pct": str(pct)}
        ohlc_exact += all(schwab[stamp][key] == massive[stamp][key] for key in FIELDS[:4])
        sign = ("massive_greater" if massive[stamp]["v"] > schwab[stamp]["v"] else
                "schwab_greater" if schwab[stamp]["v"] > massive[stamp]["v"] else "equal")
        direction[sign] += 1
        if changes:
            diffs.append({"timestamp_ms": stamp, "kind": "paired", "fields": changes,
                          "volume_direction": sign})
    impact = line_impact(prefix, schwab, massive, boundary)
    return {"day": row["day"], "symbol": row["symbol"], "both": len(common),
            "time_exact": s == m and bool(common), "only_M": sorted(m - s), "only_S": sorted(s - m),
            "ohlc_exact": ohlc_exact, "field_exact": exact,
            "ohlc_max_diff_pct": str(max(ohlc_percent)) if ohlc_percent else None,
            "volume_exact": exact["v"],
            "volume_median_diff_pct": str(median(volume_percent)) if volume_percent else None,
            "volume_max_diff_pct": str(max(volume_percent)) if volume_percent else None,
            "volume_direction": direction, "line_impact": impact, "diff_records": diffs,
            "source_result": "PASS" if common and s == m and
                             ohlc_exact == exact["v"] == len(common) else "FAIL"}


def assess(rows):
    results = []
    for row in rows:
        try:
            results.append(analyze(row))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            results.append({"day": row.get("day"), "symbol": row.get("symbol"),
                            "source_result": "ERROR", "error": str(exc)})
    code = 2 if any(r["source_result"] == "ERROR" for r in results) else int(
        not results or any(r["source_result"] != "PASS" or r["line_impact"]["result"] != "PASS"
                           for r in results))
    return {"method": METHOD, "rows": results, "exit_code": code}


def table(report):
    lines = ["day symbol both time onlyM onlyS OHLCeq OHLCmax% Veq Vmedian% Vmax% M>S/S>M "
             "07:09[S->M OHLC] state/level/age result"]
    for row in report["rows"]:
        if row["source_result"] == "ERROR":
            lines.append(f"{row['day']} {row['symbol']} ERROR {row['error']}")
            continue
        def pct(value):
            return "NA" if value is None else format(Decimal(value), ".4f")
        impact = row["line_impact"]
        def line(value):
            return "NA" if value is None else f"{value['state']}/{value['level']}/{value['age']}"
        direction = row["volume_direction"]
        lines.append(f"{row['day']} {row['symbol']} {row['both']} {row['time_exact']} "
                     f"{len(row['only_M'])} {len(row['only_S'])} {row['ohlc_exact']} "
                     f"{pct(row['ohlc_max_diff_pct'])} {row['volume_exact']} "
                     f"{pct(row['volume_median_diff_pct'])} {pct(row['volume_max_diff_pct'])} "
                     f"{direction['massive_greater']}/{direction['schwab_greater']} "
                     f"{line(impact['baseline'])}->{line(impact['replaced'])} {impact['result']}")
    lines.append(f"exit_code={report['exit_code']} (strict source AND line parity)")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", nargs="?", type=Path,
                        default=ROOT / "tests/fixtures/line_repair_preopen_20261005_09.json")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    report = assess(json.loads(args.fixture.read_text(), parse_float=Decimal)["rows"])
    print(json.dumps(report, allow_nan=False) if args.format == "json" else table(report))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
