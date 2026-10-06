#!/usr/bin/env python3
"""Counterfactual spanning/clamp inflation on the fixed reviewer's held population.

Inputs are retained candles and the unchanged pre-R6 acceptance JSON. This is
not market-silence evidence, a fill replay, or permission to admit a traded hole.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from line_restore_acceptance import et, load_bars, oracle_rows
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy, SymbolState


def gap_pairs(rows):
    return {(a["ts"], b["ts"]) for a, b in zip(rows, rows[1:]) if b["ts"] - a["ts"] > 90_000}


def available_rows(rows, event, through):
    return [r for r in rows if r["ts"] <= through and (
        (r["created_ms"] <= event and r["ts"] + 60_000 <= event)
        or ((r["ts"] + 61_000 if r["source"] == "live" else
             max(r["created_ms"], r["ts"] + 61_000)) <= through + 61_000)
    )]


def resume_bar(rows, event):
    # Count consecutive live closes, never ten arrivals or historical REST rows.
    live = [r for r in rows if r["source"] == "live" and not (
        r["created_ms"] <= event and r["ts"] + 60_000 <= event)]
    streak, previous = 0, 0
    for row in live:
        streak = streak + 1 if row["ts"] - previous == 60_000 else 1
        previous = row["ts"]
        if streak == 10:
            return row["ts"]
    return None


def lines(rows):
    engines = [SchwabV2Strategy(Settings(strategy_schwab_1m_v2_atr_flip_probe_symbols=""))
               for _ in range(2)]
    states = [SymbolState(rows[0]["symbol"]) for _ in engines]
    out, previous = {}, 0
    for row in rows:
        bar = OHLCVBar(row["ts"], *(float(row[key]) for key in (
            "open_price", "high_price", "low_price", "close_price")), int(row["volume"]))
        for index, (engine, state) in enumerate(zip(engines, states)):
            engine._update_atr_state(state, bar, observation_phase="replay", state_only=True,
                                     span_gap=index == 0 and row["ts"] - previous > 90_000)
        out[row["ts"]] = {
            "span_state": states[0].atr_state, "span_trail": states[0].atr_trail,
            "clamp_state": states[1].atr_state, "clamp_trail": states[1].atr_trail,
        }
        previous = row["ts"]
    # Independent oracle checks every prefix, not only the reported ten bars.
    for row, oracle in zip(rows, oracle_rows(rows)):
        actual = out[row["ts"]]
        if actual["span_state"] != oracle["state"] or (
            round(actual["span_trail"] or 0, 4) != round(oracle["trail"] or 0, 4)
        ):
            raise AssertionError(f"spanning oracle disagreement {row['symbol']} {et(row['ts'])}")
    return out


def measure(bars, baseline):
    reports = []
    for case in baseline:
        end = case["results"]["plus10"]
        if case["kind"] != "readd" or not end or not case["verdict"][1].startswith("HELD"):
            continue
        rows = bars[(case["symbol"], case["day"])]
        # The last closed live bar scored by the baseline is a fixed cutoff.
        live = [r for r in rows if r["source"] == "live" and not (
            r["created_ms"] <= case["t"] and r["ts"] + 60_000 <= case["t"])]
        if len(live) < 11:
            raise AssertionError("baseline +10 has fewer than eleven live arrivals")
        cutoff = live[10]["ts"]
        final_missing = {minute for left, right in gap_pairs(rows)
                         for minute in range(left + 60_000, right, 60_000)}
        permanent = sorted((left, right) for left, right in gap_pairs(
            available_rows(rows, case["t"], cutoff))
            if any(minute in final_missing for minute in range(left + 60_000, right, 60_000)))
        if not permanent:
            continue
        resumed = resume_bar(rows, case["t"])
        after = [r for r in rows if resumed is not None and r["ts"] > resumed][:10]
        math = lines(rows)
        samples = []
        for row in after:
            value = dict(math[row["ts"]], bar=et(row["ts"]))
            clamp, span = value["clamp_trail"], value["span_trail"]
            value["absolute_difference_pct"] = (
                abs(span - clamp) / clamp * 100 if clamp and span else None)
            value["counterfactual_rest_span"] = span * 1.005 if span else None
            value["counterfactual_rest_clamp"] = clamp * 1.005 if clamp else None
            samples.append(value)
        measured = [s["absolute_difference_pct"] for s in samples
                    if s["absolute_difference_pct"] is not None]
        maximum = max(measured) if measured else None
        reports.append({
            "symbol": case["symbol"], "event": case["et"], "event_ms": case["t"],
            "permanent_pairs": permanent, "resume": et(resumed) if resumed else None,
            "bars_measured": len(measured), "maximum_difference_pct": maximum,
            "exceeds_0_5_buffer": maximum is not None and maximum > .5,
            "colour_differs": any(s["span_state"] != s["clamp_state"] for s in samples),
            "coverage": "MEASURED" if len(measured) == 10 else "UNMEASURED: fewer than ten bars after resume",
            "samples": samples,
        })
    return reports


def availability_diagnostic(bars, results):
    """Explain, never waive, mismatches from the unchanged reviewer runner."""
    receipts = []
    for case in results:
        rows = bars[(case["symbol"], case["day"])]
        event = case["t"]
        known = [r for r in rows if r["created_ms"] <= event and r["ts"] + 60_000 <= event]
        present = {r["ts"] for r in known}
        post = sorted((r for r in rows if r["ts"] not in present), key=lambda r:
                      r["ts"] + 61_000 if r["source"] == "live" else
                      max(r["created_ms"], r["ts"] + 61_000))
        live_count = 0
        for row in post:
            known.append(row)
            if row["source"] != "live":
                continue
            live_count += 1
            if live_count not in (1, 11):
                continue
            index = 0 if live_count == 1 else 1
            name = "first" if index == 0 else "plus10"
            if not case["verdict"][index].startswith("MISMATCH"):
                continue
            admitted = case["results"][name]
            supplied = sorted({r["ts"]: r for r in known}.values(), key=lambda r: r["ts"])
            oracle = oracle_rows(supplied)[-1]
            missing = [r for r in rows if r["ts"] <= row["ts"]
                       and r["ts"] not in {r["ts"] for r in supplied}]
            receipts.append({
                "symbol": case["symbol"], "event": case["et"], "reading": name,
                "bar": et(row["ts"]), "code_state": admitted["state"],
                "code_trail": admitted["trail"], "supplied_prefix_state": oracle["state"],
                "supplied_prefix_trail": oracle["trail"],
                "code_matches_supplied_prefix": admitted["state"] == oracle["state"]
                and admitted["trail"] == round(oracle["trail"] or 0, 4),
                "not_yet_supplied_bar_count": len(missing),
                "missing_bar_times": [et(r["ts"]) for r in missing],
            })
    return receipts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--acceptance", default="")
    args = parser.parse_args()
    logging.disable(logging.WARNING)
    population = load_bars(args.bars)
    audited_bars = 0
    for rows in population.values():
        lines(rows)
        audited_bars += len(rows)
    results = measure(population, json.loads(Path(args.baseline).read_text()))
    output = Path(args.output)
    output.write_text(json.dumps(results, indent=2) + "\n")
    output.with_suffix(".audit.json").write_text(json.dumps({
        "symbol_days": len(population), "stored_bars": audited_bars,
        "mathematical_mismatches_on_identical_inputs": 0,
        "scope": "counterfactual mathematics only; NOT service population acceptance",
    }, indent=2) + "\n")
    if args.acceptance:
        diagnostic = availability_diagnostic(population, json.loads(Path(args.acceptance).read_text()))
        output.with_suffix(".availability.json").write_text(json.dumps(diagnostic, indent=2) + "\n")
        print(json.dumps({"mismatches_in_reviewer_runner": len(diagnostic),
                          "code_matches_exact_supplied_prefix": sum(
                              r["code_matches_supplied_prefix"] for r in diagnostic),
                          "with_unarrived_bars_in_runner_oracle": sum(
                              r["not_yet_supplied_bar_count"] > 0 for r in diagnostic)}))
    markdown = [
        "# R6 Spanning-versus-Clamp Measurement", "",
        "Generated from the fixed pre-R6 held re-add population, not selected on inflation.",
        "Permanent means at least one interior minute remains missing in the final retained series.",
        "No tape absence or hypothetical broker fill is asserted. All-series spanning is a counterfactual;",
        "positive traded-hole evidence still blocks production admission. First resume requires ten",
        "consecutive live closes. Samples are the next ten stored bars AFTER that resume, not ten arrivals.",
        "Rest prices are counterfactual trail x 1.005, including long states where no rest is issued.",
        "The percentage uses clamped trail as denominator; the same percentage applies before wire rounding.",
        "", f"Population independently mapped: {len(results)} permanent re-add cases, versus the reviewer's stated 28.",
        "The remaining case is UNMEASURED until its identity is reconciled; this is not a completed 28-case receipt.", "",
        "| Symbol / event ET | Resume bar ET | Bars after resume | Max difference | >0.5% | Colour differs | Coverage |",
        "|---|---|---:|---:|---|---|---|",
    ]
    for row in results:
        delta = row["maximum_difference_pct"]
        markdown.append(f"| {row['symbol']} {row['event']} | {row['resume'] or 'never'} | "
                        f"{row['bars_measured']} | {delta:.4f}% | "
                        f"{row['exceeds_0_5_buffer']} | {row['colour_differs']} | {row['coverage']} |"
                        if delta is not None else
                        f"| {row['symbol']} {row['event']} | {row['resume'] or 'never'} | 0 | "
                        f"n/a | unmeasured | unmeasured | {row['coverage']} |")
    markdown.extend(["", "Per-bar spanning/clamp states, trails and implied resting prices are in the companion JSON.",
                     "Any >0.5% case requires the reviewer's individual disposition; it is NOT waived by this report."])
    output.with_suffix(".md").write_text("\n".join(markdown) + "\n")
    print(json.dumps({"population": len(results), "measured_ten": sum(r["bars_measured"] == 10 for r in results),
                      "above_buffer": sum(r["exceeds_0_5_buffer"] for r in results),
                      "output": str(output)}, indent=2))


if __name__ == "__main__":
    main()
