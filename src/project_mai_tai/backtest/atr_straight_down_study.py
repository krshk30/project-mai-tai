"""Compare the 24 ATR entries that never reached +1% with the other 73 entries.

The target label and entry population are locked from the prior reviewed artifacts. Feature-rule
selection uses only 2026-08-24 through 2026-08-28; 2026-08-31 and 2026-09-01 are holdout.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Sequence

from project_mai_tai.backtest.atr_combination_study import (
    EASTERN,
    LockedEntry,
    _indicator_context,
    load_locked_entries,
)
from project_mai_tai.backtest.data import SchwabBar, Trade

BAR_NUMBERS = (1, 2, 3, 4, 5)
MIN_COVERAGE = 0.85


@dataclass(frozen=True)
class BarState:
    session_day_et: str
    split: str
    symbol: str
    buy_signal_ts: datetime
    entry_ts: datetime
    entry_px: float
    never_touched_plus_1: bool
    bar_number: int
    bar_start_ts: datetime
    bar_close_ts: datetime
    strategy_bar_available: bool
    post_fill_trade_prints: int
    missing_state: str
    close_vs_entry_pct: float | None
    close_above_entry: bool | None
    traded_above_entry: bool | None
    bar_direction: str | None
    running_low_pct: float | None
    volume_ratio_20: float | None
    macd_histogram: float | None
    macd_histogram_pct: float | None
    macd_histogram_direction: str | None
    stochastic: float | None
    rsi: float | None
    dot_consensus: int | None
    vwap: float | None
    price_vs_vwap_pct: float | None
    above_vwap: bool | None
    atr_trailing_stop: float | None
    atr_stop_vs_price_pct: float | None
    atr_stop_position: str | None

    @property
    def key(self) -> tuple[str, str]:
        return self.symbol, self.buy_signal_ts.isoformat()


@dataclass(frozen=True)
class Atom:
    bar_number: int
    feature: str
    text: str
    evaluate: Callable[[BarState], bool | None]


def load_target_keys(path: Path) -> set[tuple[str, str]]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    keys = {
        (row["symbol"], row["buy_signal_ts"])
        for row in rows
        if float(row["max_up_pct"]) < 1.0
    }
    if len(keys) != 24:
        raise RuntimeError(f"straight-down population must contain 24 entries, got {len(keys)}")
    return keys


def _trades_in_bar(
    trades: Sequence[Trade], entry_ts: datetime, start: datetime, end: datetime
) -> list[Trade]:
    lower = max(entry_ts, start)
    return [trade for trade in trades if lower <= trade.ts < end]


def build_bar_states(
    entry: LockedEntry,
    target_keys: set[tuple[str, str]],
    bars_by_close: dict[datetime, dict],
    trades: Sequence[Trade],
) -> list[BarState]:
    states: list[BarState] = []
    observed_prices: list[float] = []
    for bar_number in BAR_NUMBERS:
        start = entry.buy_signal_ts + timedelta(minutes=bar_number - 1)
        close_ts = entry.buy_signal_ts + timedelta(minutes=bar_number)
        context = bars_by_close.get(close_ts)
        prints = _trades_in_bar(trades, entry.entry_ts, start, close_ts)
        observed_prices.extend(float(trade.price) for trade in prints)
        missing: list[str] = []
        if context is None:
            missing.append("strategy_bar")
        if not prints:
            missing.append("trade_prints")
        bar: SchwabBar | None = context["bar"] if context is not None else None
        close = float(bar.close) if bar is not None else None
        vwap = float(context["vwap"]) if context is not None else None
        trail = float(context["atr_trailing_stop"]) if context is not None else None
        if bar is None:
            bar_direction = None
        elif bar.close > bar.open:
            bar_direction = "up"
        elif bar.close < bar.open:
            bar_direction = "down"
        else:
            bar_direction = "flat"
        if close is None or trail is None:
            stop_position = None
        elif trail > close:
            stop_position = "above"
        elif trail < close:
            stop_position = "below"
        else:
            stop_position = "equal"
        states.append(
            BarState(
                session_day_et=entry.session_day_et,
                split=entry.split,
                symbol=entry.symbol,
                buy_signal_ts=entry.buy_signal_ts,
                entry_ts=entry.entry_ts,
                entry_px=entry.entry_px,
                never_touched_plus_1=entry.key in target_keys,
                bar_number=bar_number,
                bar_start_ts=start,
                bar_close_ts=close_ts,
                strategy_bar_available=context is not None,
                post_fill_trade_prints=len(prints),
                missing_state=",".join(missing),
                close_vs_entry_pct=(close / entry.entry_px - 1.0) * 100.0 if close else None,
                close_above_entry=close > entry.entry_px if close is not None else None,
                traded_above_entry=(
                    any(float(trade.price) > entry.entry_px for trade in prints)
                    if prints else None
                ),
                bar_direction=bar_direction,
                running_low_pct=(
                    (min(observed_prices) / entry.entry_px - 1.0) * 100.0
                    if observed_prices else None
                ),
                volume_ratio_20=context["volume_ratio_20"] if context is not None else None,
                macd_histogram=context["macd_histogram"] if context is not None else None,
                macd_histogram_pct=(
                    context["macd_histogram_pct"] if context is not None else None
                ),
                macd_histogram_direction=(
                    context["macd_histogram_direction"] if context is not None else None
                ),
                stochastic=context["stochastic"] if context is not None else None,
                rsi=context["rsi"] if context is not None else None,
                dot_consensus=context["dot_consensus"] if context is not None else None,
                vwap=vwap,
                price_vs_vwap_pct=(close / vwap - 1.0) * 100.0 if close and vwap else None,
                above_vwap=close > vwap if close is not None and vwap is not None else None,
                atr_trailing_stop=trail,
                atr_stop_vs_price_pct=(trail / close - 1.0) * 100.0 if trail and close else None,
                atr_stop_position=stop_position,
            )
        )
    return states


def run_measurement(source, settings, entries, target_keys) -> list[BarState]:
    grouped: dict[tuple[str, str], list[LockedEntry]] = defaultdict(list)
    for entry in entries:
        grouped[(entry.session_day_et, entry.symbol)].append(entry)
    states: list[BarState] = []
    for (day_text, symbol), group in sorted(grouped.items()):
        session_day = date.fromisoformat(day_text)
        observation_start = datetime.combine(session_day, time(4), EASTERN)
        session_start = datetime.combine(session_day, time(7), EASTERN)
        session_end = datetime.combine(session_day, time(16), EASTERN)
        bars = source.schwab_bars(symbol, observation_start, session_end)
        trades = source.trades(symbol, session_start, session_end)
        context = _indicator_context(symbol, bars, settings)
        for entry in sorted(group, key=lambda item: item.entry_ts):
            states.extend(build_bar_states(entry, target_keys, context, trades))
        print(
            f"bar states {day_text} {symbol}: entries={len(group)} bars={len(bars)} "
            f"trades={len(trades)}",
            flush=True,
        )
    if len(states) != 97 * 5:
        raise RuntimeError(f"expected 485 bar states, got {len(states)}")
    return states


def hypothesis_results(states: Sequence[BarState]) -> list[dict[str, object]]:
    by_key: dict[tuple[str, str], list[BarState]] = defaultdict(list)
    for state in states:
        by_key[state.key].append(state)
    rows: list[dict[str, object]] = []
    for horizon in (2, 3, 4, 5):
        for split in ("build", "holdout", "all"):
            groups = [
                sorted(group, key=lambda state: state.bar_number)
                for group in by_key.values()
                if split == "all" or group[0].split == split
            ]
            target_total = sum(group[0].never_touched_plus_1 for group in groups)
            comparator_total = len(groups) - target_total
            assessed: list[tuple[bool, bool]] = []
            for group in groups:
                values = [
                    state.traded_above_entry
                    for state in group
                    if 2 <= state.bar_number <= horizon
                ]
                if len(values) != horizon - 1 or any(value is None for value in values):
                    continue
                assessed.append((group[0].never_touched_plus_1, not any(values)))
            rows.append(
                {
                    "horizon_bar": horizon,
                    "condition": f"no post-fill trade print above entry in bars 2-{horizon}",
                    "split": split,
                    "target_total": target_total,
                    "comparator_total": comparator_total,
                    "target_assessed": sum(target for target, _ in assessed),
                    "comparator_assessed": sum(not target for target, _ in assessed),
                    "target_caught": sum(target and matched for target, matched in assessed),
                    "comparator_touched": sum(
                        not target and matched for target, matched in assessed
                    ),
                    "unavailable": len(groups) - len(assessed),
                }
            )
    return rows


def _numeric_atom(bar_number: int, feature: str, op: str, threshold: float) -> Atom:
    def evaluate(state: BarState) -> bool | None:
        value = getattr(state, feature)
        if value is None:
            return None
        return float(value) <= threshold if op == "<=" else float(value) >= threshold

    return Atom(bar_number, feature, f"b{bar_number}.{feature} {op} {threshold:+g}", evaluate)


def _category_atom(bar_number: int, feature: str, expected: object) -> Atom:
    def evaluate(state: BarState) -> bool | None:
        value = getattr(state, feature)
        return None if value is None else value == expected

    return Atom(bar_number, feature, f"b{bar_number}.{feature} is {expected}", evaluate)


def atoms_through(horizon: int) -> list[Atom]:
    thresholds = {
        "close_vs_entry_pct": (-3, -2, -1, 0, 1),
        "running_low_pct": (-5, -3, -2, -1, 0),
        "volume_ratio_20": (0.5, 0.75, 1, 1.5, 2),
        "macd_histogram_pct": (0,),
        "stochastic": (30, 50, 70),
        "rsi": (30, 50, 70),
        "dot_consensus": (1, 2),
        "price_vs_vwap_pct": (-2, -1, 0, 1, 2),
    }
    categories = {
        "close_above_entry": (True, False),
        "traded_above_entry": (True, False),
        "bar_direction": ("up", "down"),
        "macd_histogram_direction": ("rising", "falling"),
        "above_vwap": (True, False),
        "atr_stop_position": ("above", "below"),
    }
    atoms: list[Atom] = []
    for bar_number in range(1, horizon + 1):
        for feature, values in thresholds.items():
            for threshold in values:
                atoms.append(_numeric_atom(bar_number, feature, "<=", float(threshold)))
                atoms.append(_numeric_atom(bar_number, feature, ">=", float(threshold)))
        for feature, values in categories.items():
            for value in values:
                atoms.append(_category_atom(bar_number, feature, value))
    return atoms


def _evaluate_rule(
    rule: Sequence[Atom], states_by_key: dict[tuple[str, str], dict[int, BarState]], split: str
) -> dict[str, object]:
    entries = [
        bars for bars in states_by_key.values()
        if split == "all" or next(iter(bars.values())).split == split
    ]
    target_total = sum(next(iter(bars.values())).never_touched_plus_1 for bars in entries)
    comparator_total = len(entries) - target_total
    assessed: list[tuple[bool, bool]] = []
    for bars in entries:
        values = [atom.evaluate(bars[atom.bar_number]) for atom in rule]
        if any(value is None for value in values):
            continue
        assessed.append((next(iter(bars.values())).never_touched_plus_1, all(values)))
    caught = sum(target and matched for target, matched in assessed)
    false_positive = sum(not target and matched for target, matched in assessed)
    return {
        f"{split}_target_total": target_total,
        f"{split}_comparator_total": comparator_total,
        f"{split}_target_assessed": sum(target for target, _ in assessed),
        f"{split}_comparator_assessed": sum(not target for target, _ in assessed),
        f"{split}_target_caught": caught,
        f"{split}_comparator_touched": false_positive,
        f"{split}_target_not_caught": target_total - caught,
        f"{split}_comparator_not_touched": comparator_total - false_positive,
        f"{split}_unavailable": len(entries) - len(assessed),
    }


def search_combinations(states: Sequence[BarState]) -> tuple[list[dict], list[dict]]:
    by_key: dict[tuple[str, str], dict[int, BarState]] = defaultdict(dict)
    for state in states:
        by_key[state.key][state.bar_number] = state
    build_keys = sorted(
        key for key, bars in by_key.items() if next(iter(bars.values())).split == "build"
    )
    target_mask = 0
    for index, key in enumerate(build_keys):
        if by_key[key][1].never_touched_plus_1:
            target_mask |= 1 << index
    all_mask = (1 << len(build_keys)) - 1
    comparator_mask = all_mask ^ target_mask
    target_total = target_mask.bit_count()
    comparator_total = comparator_mask.bit_count()
    min_target_caught = math.ceil(target_total * 15 / 24)
    max_comparator_touched = math.floor(comparator_total * 9 / 73)

    census: list[dict] = []
    selected: list[dict] = []
    for horizon in BAR_NUMBERS:
        encoded: list[tuple[Atom, int, int]] = []
        seen_atoms: set[tuple[int, int]] = set()
        for atom in atoms_through(horizon):
            true_mask = 0
            available_mask = 0
            for index, key in enumerate(build_keys):
                value = atom.evaluate(by_key[key][atom.bar_number])
                if value is not None:
                    available_mask |= 1 << index
                    if value:
                        true_mask |= 1 << index
            signature = true_mask, available_mask
            if signature in seen_atoms:
                continue
            seen_atoms.add(signature)
            encoded.append((atom, true_mask, available_mask))

        candidates: list[tuple[tuple, tuple[Atom, ...], dict]] = []
        seen_rules: set[tuple[int, int]] = set()
        for size in (2, 3):
            for pieces in itertools.combinations(encoded, size):
                rule = tuple(piece[0] for piece in pieces)
                if max(atom.bar_number for atom in rule) != horizon:
                    continue
                if len({(atom.bar_number, atom.feature) for atom in rule}) != size:
                    continue
                matched = all_mask
                available = all_mask
                for _, true_mask, available_mask in pieces:
                    matched &= true_mask
                    available &= available_mask
                signature = matched, available
                if signature in seen_rules:
                    continue
                seen_rules.add(signature)
                target_assessed = (available & target_mask).bit_count()
                comparator_assessed = (available & comparator_mask).bit_count()
                if (
                    target_assessed < math.ceil(target_total * MIN_COVERAGE)
                    or comparator_assessed < math.ceil(comparator_total * MIN_COVERAGE)
                ):
                    continue
                target_caught = (matched & target_mask).bit_count()
                comparator_touched = (matched & comparator_mask).bit_count()
                if (
                    target_caught < min_target_caught
                    or comparator_touched > max_comparator_touched
                ):
                    continue
                build = _evaluate_rule(rule, by_key, "build")
                holdout = _evaluate_rule(rule, by_key, "holdout")
                result = {
                    "horizon_bar": horizon,
                    "condition_count": size,
                    "conditions": " AND ".join(atom.text for atom in rule),
                    **build,
                    **holdout,
                }
                census.append(result)
                candidates.append(
                    ((target_caught, -comparator_touched, -size), rule, result)
                )
        candidates.sort(key=lambda item: item[0], reverse=True)
        for rank, (_, rule, result) in enumerate(candidates[:5], 1):
            pooled_target = int(result["build_target_caught"]) + int(
                result["holdout_target_caught"]
            )
            pooled_comparator = int(result["build_comparator_touched"]) + int(
                result["holdout_comparator_touched"]
            )
            selected.append(
                {
                    "build_rank_at_horizon": rank,
                    **result,
                    "pooled_target_caught": pooled_target,
                    "pooled_comparator_touched": pooled_comparator,
                    "pooled_meets_15_under_10": (
                        pooled_target >= 15 and pooled_comparator < 10
                    ),
                    "holdout_separates": (
                        int(result["holdout_target_caught"])
                        * int(result["holdout_comparator_total"])
                        > int(result["holdout_comparator_touched"])
                        * int(result["holdout_target_total"])
                    ),
                    "_atoms": rule,
                }
            )
    return census, selected


def _plain(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "_atoms"}


def _write_csv(path: Path, rows: Sequence[dict]) -> None:
    if not rows:
        return
    plain = [_plain(dict(row)) for row in rows]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(plain[0]))
        writer.writeheader()
        for row in plain:
            writer.writerow(
                {
                    key: value.isoformat() if isinstance(value, (date, datetime)) else value
                    for key, value in row.items()
                }
            )


def write_report(path: Path, states, hypotheses, selected) -> None:
    target_build = sum(
        state.never_touched_plus_1 and state.bar_number == 1 and state.split == "build"
        for state in states
    )
    target_holdout = sum(
        state.never_touched_plus_1 and state.bar_number == 1 and state.split == "holdout"
        for state in states
    )
    lines = [
        "# ATR Straight-Down First-Five-Bar Study",
        "",
        f"Locked target: **24/97 entries that never touched +1%** ({target_build} build, "
        f"{target_holdout} holdout). Comparator: the other 73 entries. The 31 sub-+5 trades "
        "that did touch +1% are not mixed into the target; they remain part of the explicitly "
        "requested 73-entry comparator.",
        "",
        "Bars are the first five Schwab strategy bars after the ATR BUY fill. `Traded above` "
        "uses captured trade prints strictly after the executable fill, avoiding pre-fill seconds "
        "inside bar 1. Closes, bar direction, volume, indicators, VWAP and ATR trail use the exact "
        "Schwab strategy series. Volume ratio is current volume divided by the live 20-bar average "
        "including the current bar. Missing bars or prints are unavailable, never inferred.",
        "",
        "## Coverage",
        "",
        "| Bar | Strategy bar | Post-fill prints | Complete state |",
        "|---:|---:|---:|---:|",
    ]
    for bar_number in BAR_NUMBERS:
        rows = [state for state in states if state.bar_number == bar_number]
        strategy = sum(state.strategy_bar_available for state in rows)
        prints = sum(state.post_fill_trade_prints > 0 for state in rows)
        complete = sum(not state.missing_state for state in rows)
        lines.append(f"| {bar_number} | {strategy}/97 | {prints}/97 | {complete}/97 |")
    lines.extend([
        "",
        "## Operator Hypothesis",
        "",
        "| Through bar | Build: target caught / 19 | Build: comparator touched / 48 | "
        "Holdout: target caught / 5 | Holdout: comparator touched / 25 | "
        "All: target / comparator | Unavailable |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for horizon in (2, 3, 4, 5):
        by_split = {
            row["split"]: row for row in hypotheses if row["horizon_bar"] == horizon
        }
        build = by_split["build"]
        holdout = by_split["holdout"]
        all_rows = by_split["all"]
        lines.append(
            f"| {horizon} | {build['target_caught']}/19 | "
            f"{build['comparator_touched']}/48 | {holdout['target_caught']}/5 | "
            f"{holdout['comparator_touched']}/25 | {all_rows['target_caught']}/24 / "
            f"{all_rows['comparator_touched']}/73 | {all_rows['unavailable']} |"
        )
    lines.extend([
        "",
        "The hypothesis condition is: from bar 2 through the stated horizon, no captured "
        "post-fill trade print exceeded the ask fill.",
        "",
        "## Build-Selected Pairs and Triples",
        "",
        "Only build-day rules that caught the proportional equivalent of 15/24 targets while "
        "touching fewer than the proportional equivalent of 10/73 comparators were eligible. "
        "Holdout results were read only after ranking. Missing inputs remain on the not-caught side.",
        "",
        "| By bar | Conditions | Build target / comparator | Holdout target / comparator | "
        "Pooled target / comparator | Meets 15/<10 | Holdout separates |",
        "|---:|---|---:|---:|---:|:---:|:---:|",
    ])
    if not selected:
        lines.append("| - | No pair or triple met the build threshold | - | - | - | No | No |")
    for row in selected:
        lines.append(
            f"| {row['horizon_bar']} | {row['conditions']} | "
            f"{row['build_target_caught']}/{row['build_target_total']} / "
            f"{row['build_comparator_touched']}/{row['build_comparator_total']} | "
            f"{row['holdout_target_caught']}/{row['holdout_target_total']} / "
            f"{row['holdout_comparator_touched']}/{row['holdout_comparator_total']} | "
            f"{row['pooled_target_caught']}/24 / {row['pooled_comparator_touched']}/73 | "
            f"{'Yes' if row['pooled_meets_15_under_10'] else 'No'} | "
            f"{'Yes' if row['holdout_separates'] else 'No'} |"
        )
    early = [
        row for row in selected
        if int(row["horizon_bar"]) <= 3
        and row["pooled_meets_15_under_10"]
        and row["holdout_separates"]
    ]
    lines.extend([
        "",
        f"Conditions visible by bar 3 meeting the stated pooled count and preserving holdout "
        f"separation: **{len(early)}**.",
        "",
        "Exact 485 bar states, hypothesis counts, all build-qualified combinations, and the "
        "build-ranked rules with unchanged holdout results are in the companion CSV files. "
        "This is measurement only; no entry or exit rule was changed.",
    ])
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--population-csv", type=Path,
        default=Path("analysis/reports/atr-trend-exit-2026-08-24-to-2026-09-01-trades.csv"),
    )
    parser.add_argument(
        "--loser-paths-csv", type=Path,
        default=Path(
            "analysis/reports/atr-combination-study-2026-08-24-to-2026-09-01-loser-paths.csv"
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("analysis/reports"))
    args = parser.parse_args()

    from project_mai_tai.backtest.data import DbMarketDataSource
    from project_mai_tai.backtest.replay import build_replay_settings
    from project_mai_tai.db.session import build_session_factory
    from project_mai_tai.settings import get_settings

    entries = load_locked_entries(args.population_csv)
    target_keys = load_target_keys(args.loser_paths_csv)
    if not target_keys <= {entry.key for entry in entries}:
        raise RuntimeError("straight-down target contains an entry outside the locked 97")
    base = get_settings()
    source = DbMarketDataSource(build_session_factory(base))
    settings = build_replay_settings(base=base)
    states = run_measurement(source, settings, entries, target_keys)
    hypotheses = hypothesis_results(states)
    census, selected = search_combinations(states)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = "atr-straight-down-study-2026-08-24-to-2026-09-01"
    _write_csv(args.output_dir / f"{stem}-bar-states.csv", [asdict(row) for row in states])
    _write_csv(args.output_dir / f"{stem}-hypothesis.csv", hypotheses)
    _write_csv(args.output_dir / f"{stem}-combination-census.csv", census)
    _write_csv(args.output_dir / f"{stem}-selected-rules.csv", selected)
    write_report(args.output_dir / f"{stem}.md", states, hypotheses, selected)
    (args.output_dir / f"{stem}.json").write_text(
        json.dumps(
            {
                "bar_states": [asdict(row) for row in states],
                "hypothesis": hypotheses,
                "selected_rules": [_plain(row) for row in selected],
            },
            default=lambda value: value.isoformat() if isinstance(value, (date, datetime)) else str(value),
            indent=2,
        ) + "\n"
    )
    print(
        f"entries=97 target=24 states={len(states)} qualifying={len(census)} "
        f"selected={len(selected)} output={args.output_dir / stem}*"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
