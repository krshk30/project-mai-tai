"""Build/holdout study of small early-state combinations on the locked 97 ATR entries.

This is measurement-only. Entry timestamps, fills, ATR-segment exits, and reached-5 labels come
from the reviewed trend-exit artifact. Feature rules are selected on 2026-08-24 through
2026-08-28, then applied unchanged to the untouched 2026-08-31 and 2026-09-01 holdout.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import itertools
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import Quote, SchwabBar
from project_mai_tai.backtest.dot_entry import build_rows
from project_mai_tai.backtest.replay import BAR_CLOSE_OFFSET_MS, ReplayStrategy, _to_chartbar

CHECKPOINTS = (0, 3, 5, 10)
EASTERN = ZoneInfo("America/New_York")
BUILD_END = date(2026, 8, 28)
HOLDOUT_START = date(2026, 8, 31)
TARGETS = (2.0, 3.0, 4.0)
MIN_COVERAGE = 0.85


@dataclass(frozen=True)
class LockedEntry:
    session_day_et: str
    symbol: str
    buy_signal_ts: datetime
    entry_ts: datetime
    entry_px: float
    reached_5: bool
    natural_exit_ts: datetime
    natural_return_pct: float
    natural_max_up_pct: float
    natural_max_down_pct: float

    @property
    def split(self) -> str:
        return "build" if date.fromisoformat(self.session_day_et) <= BUILD_END else "holdout"

    @property
    def key(self) -> tuple[str, str]:
        return self.symbol, self.buy_signal_ts.isoformat()


@dataclass(frozen=True)
class FeatureSnapshot:
    session_day_et: str
    split: str
    symbol: str
    buy_signal_ts: datetime
    entry_ts: datetime
    entry_px: float
    reached_5: bool
    checkpoint_minutes: int
    checkpoint_ts: datetime
    quote_ts: datetime | None
    bar_close_ts: datetime | None
    missing_state: str
    price_vs_entry_pct: float | None
    max_up_so_far_pct: float | None
    max_down_so_far_pct: float | None
    touched_plus_2: bool | None
    touched_minus_3: bool | None
    volume_ratio_20: float | None
    macd_histogram: float | None
    macd_histogram_pct: float | None
    macd_histogram_direction: str | None
    stochastic: float | None
    rsi: float | None
    dot_consensus: int | None
    atr_trailing_stop: float | None
    atr_direction: str | None
    vwap: float | None
    price_vs_vwap_pct: float | None
    above_vwap: bool | None
    minutes_since_flip: float
    last_bar_direction: str | None
    latest_minute_new_low: bool | None

    @property
    def key(self) -> tuple[str, str]:
        return self.symbol, self.buy_signal_ts.isoformat()


@dataclass(frozen=True)
class LoserPath:
    session_day_et: str
    split: str
    symbol: str
    buy_signal_ts: datetime
    entry_ts: datetime
    entry_px: float
    max_up_pct: float
    max_down_pct: float
    upside_bucket: str
    touched_plus_1: bool
    minutes_to_plus_1: float | None
    touched_plus_2: bool
    minutes_to_plus_2: float | None
    touched_plus_3: bool
    minutes_to_plus_3: float | None
    touched_plus_4: bool
    minutes_to_plus_4: float | None
    peak_ts: datetime
    post_peak_low_ts: datetime
    post_peak_drawdown_pct: float
    post_peak_low_vs_entry_pct: float


@dataclass(frozen=True)
class Atom:
    checkpoint: int
    feature: str
    text: str
    evaluate: Callable[[FeatureSnapshot], bool | None]


def _optional_float(value: str) -> float | None:
    return float(value) if value else None


def load_locked_entries(path: Path) -> list[LockedEntry]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    entries = [
        LockedEntry(
            session_day_et=row["session_day_et"],
            symbol=row["symbol"],
            buy_signal_ts=datetime.fromisoformat(row["buy_signal_ts"]),
            entry_ts=datetime.fromisoformat(row["entry_ts"]),
            entry_px=float(row["entry_px"]),
            reached_5=row["reached_5"].lower() == "true",
            natural_exit_ts=datetime.fromisoformat(row["atr_sell_exit_ts"]),
            natural_return_pct=float(row["atr_sell_return_pct"]),
            natural_max_up_pct=float(row["atr_segment_max_up_pct"]),
            natural_max_down_pct=float(row["atr_segment_max_down_pct"]),
        )
        for row in rows
    ]
    keys = {entry.key for entry in entries}
    if len(entries) != 97 or len(keys) != 97:
        raise RuntimeError(f"locked population must contain 97 unique entries, got {len(entries)}")
    days = {date.fromisoformat(entry.session_day_et) for entry in entries}
    expected = {
        date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26),
        date(2026, 8, 27), date(2026, 8, 28), date(2026, 8, 31), date(2026, 9, 1),
    }
    if days != expected:
        raise RuntimeError(f"unexpected session set: {sorted(days)}")
    return entries


def _bar_close(bar: SchwabBar) -> datetime:
    return datetime.fromtimestamp((bar.ts + BAR_CLOSE_OFFSET_MS) / 1000.0, UTC)


def _finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def _ema(values: Sequence[float], length: int) -> list[float]:
    factor = 2.0 / (length + 1.0)
    output: list[float] = []
    previous = 0.0
    for index, value in enumerate(values):
        previous = value if index == 0 else value * factor + previous * (1.0 - factor)
        output.append(previous)
    return output


def _macd_histogram(closes: Sequence[float]) -> list[float]:
    fast = _ema(closes, 12)
    slow = _ema(closes, 26)
    macd = [fast[index] - slow[index] for index in range(len(closes))]
    signal = _ema(macd, 9)
    return [macd[index] - signal[index] for index in range(len(closes))]


def _indicator_context(symbol: str, bars: list[SchwabBar], settings) -> dict[datetime, dict]:
    if not bars:
        return {}
    highs = [float(bar.high) for bar in bars]
    lows = [float(bar.low) for bar in bars]
    closes = [float(bar.close) for bar in bars]
    volumes = [float(bar.volume) for bar in bars]
    dots = build_rows(highs, lows, closes)
    hist = _macd_histogram(closes)

    strategy = ReplayStrategy(settings)
    state = strategy.watchlist_state(symbol)
    atr_rows: list[dict | None] = []
    for bar in bars:
        strategy._clock_ms = int(bar.ts) + BAR_CLOSE_OFFSET_MS
        atr_rows.append(
            strategy._update_atr_state(
                state, _to_chartbar(symbol, bar), observation_phase="replay"
            )
        )

    context: dict[datetime, dict] = {}
    vwap_pv = 0.0
    vwap_volume = 0.0
    for index, bar in enumerate(bars):
        typical = (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0
        vwap_pv += typical * float(bar.volume)
        vwap_volume += float(bar.volume)
        vwap = vwap_pv / vwap_volume if vwap_volume > 0 else float(bar.close)
        avg_volume = (
            statistics.fmean(volumes[index - 19 : index + 1]) if index >= 19 else None
        )
        histogram = _finite(hist[index])
        prior_histogram = _finite(hist[index - 1]) if index else None
        if histogram is None or prior_histogram is None:
            histogram_direction = None
        elif histogram > prior_histogram:
            histogram_direction = "rising"
        elif histogram < prior_histogram:
            histogram_direction = "falling"
        else:
            histogram_direction = "flat"
        atr = atr_rows[index]
        context[_bar_close(bar)] = {
            "bar": bar,
            "volume_ratio_20": (
                float(bar.volume) / avg_volume if avg_volume is not None and avg_volume > 0 else None
            ),
            "macd_histogram": histogram,
            "macd_histogram_pct": (
                histogram / float(bar.close) * 100.0
                if histogram is not None and float(bar.close) > 0
                else None
            ),
            "macd_histogram_direction": histogram_direction,
            "stochastic": _finite(dots.stoch[index]),
            "rsi": _finite(dots.rsi[index]),
            "dot_consensus": dots.consensus(index),
            "atr_trailing_stop": float(atr["trail"]) if atr is not None else None,
            "atr_direction": str(atr["state"]) if atr is not None else None,
            "vwap": vwap,
        }
    return context


def _quote_window(
    quotes: list[Quote], entry_ts: datetime, checkpoint_ts: datetime
) -> list[Quote]:
    times = [quote.ts for quote in quotes]
    lo = bisect.bisect_left(times, entry_ts)
    hi = bisect.bisect_right(times, checkpoint_ts)
    return quotes[lo:hi]


def _latest_minute_new_low(quotes: Sequence[Quote], entry_ts: datetime, minutes: int) -> bool | None:
    if minutes <= 0:
        return None
    minute_lows: list[float] = []
    for minute in range(1, minutes + 1):
        lo = entry_ts + timedelta(minutes=minute - 1)
        hi = entry_ts + timedelta(minutes=minute)
        values = [
            float(quote.bid)
            for quote in quotes
            if (quote.ts >= lo if minute == 1 else quote.ts > lo) and quote.ts <= hi
        ]
        if not values:
            return None
        minute_lows.append(min(values))
    return minute_lows[-1] < min(minute_lows[:-1]) if len(minute_lows) > 1 else False


def build_snapshot(
    entry: LockedEntry,
    checkpoint: int,
    quotes: list[Quote],
    context: dict[datetime, dict],
) -> FeatureSnapshot:
    checkpoint_ts = entry.entry_ts + timedelta(minutes=checkpoint)
    expected_close = entry.buy_signal_ts + timedelta(minutes=checkpoint)
    indicator = context.get(expected_close)
    observed = _quote_window(quotes, entry.entry_ts, checkpoint_ts)
    current_quote = observed[-1] if observed else None
    if current_quote is not None and checkpoint > 0:
        if current_quote.ts < checkpoint_ts - timedelta(minutes=1):
            current_quote = None
    bids = [float(quote.bid) for quote in observed]
    bar = indicator["bar"] if indicator is not None else None
    missing: list[str] = []
    if indicator is None:
        missing.append("strategy_bar")
    if current_quote is None:
        missing.append("executable_quote")
    vwap = float(indicator["vwap"]) if indicator is not None else None
    close = float(bar.close) if bar is not None else None
    if bar is None:
        direction = None
    elif bar.close > bar.open:
        direction = "up"
    elif bar.close < bar.open:
        direction = "down"
    else:
        direction = "flat"
    return FeatureSnapshot(
        session_day_et=entry.session_day_et,
        split=entry.split,
        symbol=entry.symbol,
        buy_signal_ts=entry.buy_signal_ts,
        entry_ts=entry.entry_ts,
        entry_px=entry.entry_px,
        reached_5=entry.reached_5,
        checkpoint_minutes=checkpoint,
        checkpoint_ts=checkpoint_ts,
        quote_ts=current_quote.ts if current_quote is not None else None,
        bar_close_ts=expected_close if indicator is not None else None,
        missing_state=",".join(missing),
        price_vs_entry_pct=(
            (float(current_quote.bid) / entry.entry_px - 1.0) * 100.0
            if current_quote is not None else None
        ),
        max_up_so_far_pct=(max(bids) / entry.entry_px - 1.0) * 100.0 if bids else None,
        max_down_so_far_pct=(min(bids) / entry.entry_px - 1.0) * 100.0 if bids else None,
        touched_plus_2=(max(bids) / entry.entry_px - 1.0) * 100.0 >= 2.0 if bids else None,
        touched_minus_3=(min(bids) / entry.entry_px - 1.0) * 100.0 <= -3.0 if bids else None,
        volume_ratio_20=indicator["volume_ratio_20"] if indicator is not None else None,
        macd_histogram=indicator["macd_histogram"] if indicator is not None else None,
        macd_histogram_pct=indicator["macd_histogram_pct"] if indicator is not None else None,
        macd_histogram_direction=(
            indicator["macd_histogram_direction"] if indicator is not None else None
        ),
        stochastic=indicator["stochastic"] if indicator is not None else None,
        rsi=indicator["rsi"] if indicator is not None else None,
        dot_consensus=indicator["dot_consensus"] if indicator is not None else None,
        atr_trailing_stop=indicator["atr_trailing_stop"] if indicator is not None else None,
        atr_direction=indicator["atr_direction"] if indicator is not None else None,
        vwap=vwap,
        price_vs_vwap_pct=(close / vwap - 1.0) * 100.0 if close is not None and vwap else None,
        above_vwap=close > vwap if close is not None and vwap is not None else None,
        minutes_since_flip=(checkpoint_ts - entry.buy_signal_ts).total_seconds() / 60.0,
        last_bar_direction=direction,
        latest_minute_new_low=_latest_minute_new_low(observed, entry.entry_ts, checkpoint),
    )


def run_snapshots(source, settings, entries: Sequence[LockedEntry]) -> list[FeatureSnapshot]:
    grouped: dict[tuple[str, str], list[LockedEntry]] = defaultdict(list)
    for entry in entries:
        grouped[(entry.session_day_et, entry.symbol)].append(entry)
    snapshots: list[FeatureSnapshot] = []
    for (day_text, symbol), group in sorted(grouped.items()):
        session_day = date.fromisoformat(day_text)
        observation_start = datetime.combine(session_day, time(4), EASTERN)
        session_end = datetime.combine(session_day, time(16), EASTERN).astimezone(UTC)
        bars = source.schwab_bars(symbol, observation_start, session_end)
        quotes = source.quotes(symbol, datetime.combine(session_day, time(7), EASTERN), session_end)
        context = _indicator_context(symbol, bars, settings)
        for entry in sorted(group, key=lambda item: item.entry_ts):
            snapshots.extend(
                build_snapshot(entry, checkpoint, quotes, context) for checkpoint in CHECKPOINTS
            )
        print(f"features {day_text} {symbol}: entries={len(group)} bars={len(bars)}", flush=True)
    if len(snapshots) != len(entries) * len(CHECKPOINTS):
        raise RuntimeError(f"expected {len(entries) * len(CHECKPOINTS)} snapshots, got {len(snapshots)}")
    return snapshots


def _numeric_atom(checkpoint: int, feature: str, op: str, threshold: float) -> Atom:
    def evaluate(row: FeatureSnapshot) -> bool | None:
        value = getattr(row, feature)
        if value is None:
            return None
        return float(value) <= threshold if op == "<=" else float(value) >= threshold

    return Atom(checkpoint, feature, f"{feature} {op} {threshold:+g}", evaluate)


def _category_atom(checkpoint: int, feature: str, value: object) -> Atom:
    def evaluate(row: FeatureSnapshot) -> bool | None:
        observed = getattr(row, feature)
        return None if observed is None else observed == value

    return Atom(checkpoint, feature, f"{feature} is {value}", evaluate)


def atoms_for_checkpoint(checkpoint: int) -> list[Atom]:
    thresholds = {
        "price_vs_entry_pct": (-3, -2, -1, 0, 1, 2, 3),
        "max_up_so_far_pct": (0, 1, 2, 3, 4),
        "max_down_so_far_pct": (-5, -3, -2, -1, 0),
        "volume_ratio_20": (0.5, 0.75, 1, 1.5, 2),
        "macd_histogram_pct": (0,),
        "stochastic": (30, 50, 70),
        "rsi": (30, 50, 70),
        "dot_consensus": (0, 1, 2, 3),
        "price_vs_vwap_pct": (-2, -1, 0, 1, 2),
    }
    atoms: list[Atom] = []
    for feature, values in thresholds.items():
        for threshold in values:
            atoms.append(_numeric_atom(checkpoint, feature, "<=", float(threshold)))
            atoms.append(_numeric_atom(checkpoint, feature, ">=", float(threshold)))
    for feature, values in {
        "macd_histogram_direction": ("rising", "falling"),
        "atr_direction": ("long", "short"),
        "above_vwap": (True, False),
        "last_bar_direction": ("up", "down"),
        "touched_plus_2": (True, False),
        "touched_minus_3": (True, False),
        "latest_minute_new_low": (True, False),
    }.items():
        for value in values:
            atoms.append(_category_atom(checkpoint, feature, value))
    return atoms


def _rule_result(
    atoms: Sequence[Atom], rows: Sequence[FeatureSnapshot], split: str
) -> dict[str, object]:
    selected = [row for row in rows if row.split == split]
    winner_total = sum(row.reached_5 for row in selected)
    loser_total = len(selected) - winner_total
    assessed: list[tuple[FeatureSnapshot, bool]] = []
    for row in selected:
        values = [atom.evaluate(row) for atom in atoms]
        if any(value is None for value in values):
            continue
        assessed.append((row, all(bool(value) for value in values)))
    winners = [item for item in assessed if item[0].reached_5]
    losers = [item for item in assessed if not item[0].reached_5]
    winner_removed = sum(flag for _, flag in winners)
    loser_removed = sum(flag for _, flag in losers)
    return {
        f"{split}_winner_total": winner_total,
        f"{split}_loser_total": loser_total,
        f"{split}_winner_assessed": len(winners),
        f"{split}_loser_assessed": len(losers),
        f"{split}_winner_removed": winner_removed,
        f"{split}_loser_removed": loser_removed,
        # Missing inputs cannot satisfy an AND-rule, so they remain on the kept side. They are
        # still called out separately; this is execution semantics, not value imputation.
        f"{split}_winner_kept": winner_total - winner_removed,
        f"{split}_loser_kept": loser_total - loser_removed,
        f"{split}_winner_keep_rate": (winner_total - winner_removed) / winner_total if winner_total else 0,
        f"{split}_loser_remove_rate": loser_removed / loser_total if loser_total else 0,
        f"{split}_unassessed": len(selected) - len(assessed),
    }


def search_combinations(snapshots: Sequence[FeatureSnapshot]) -> tuple[list[dict], list[dict]]:
    rows_by_checkpoint = {
        checkpoint: [row for row in snapshots if row.checkpoint_minutes == checkpoint]
        for checkpoint in CHECKPOINTS
    }
    census: list[dict] = []
    selected: list[dict] = []
    for checkpoint, rows in rows_by_checkpoint.items():
        build_total = [row for row in rows if row.split == "build"]
        build_winners = sum(row.reached_5 for row in build_total)
        build_losers = len(build_total) - build_winners
        atoms = atoms_for_checkpoint(checkpoint)
        candidates: list[tuple[tuple, tuple[Atom, ...], dict]] = []
        seen_masks: set[tuple[bool | None, ...]] = set()
        for size in (2, 3):
            for rule in itertools.combinations(atoms, size):
                if len({atom.feature for atom in rule}) != size:
                    continue
                mask = tuple(
                    None
                    if any(atom.evaluate(row) is None for atom in rule)
                    else all(bool(atom.evaluate(row)) for atom in rule)
                    for row in build_total
                )
                if mask in seen_masks:
                    continue
                seen_masks.add(mask)
                build = _rule_result(rule, rows, "build")
                winner_assessed = int(build["build_winner_assessed"])
                loser_assessed = int(build["build_loser_assessed"])
                if (
                    winner_assessed < math.ceil(build_winners * MIN_COVERAGE)
                    or loser_assessed < math.ceil(build_losers * MIN_COVERAGE)
                ):
                    continue
                min_winners_kept = math.ceil(build_winners * 30 / 42)
                min_losers_removed = math.ceil(build_losers * 25 / 55)
                if (
                    int(build["build_winner_kept"]) < min_winners_kept
                    or int(build["build_loser_removed"]) < min_losers_removed
                ):
                    continue
                holdout = _rule_result(rule, rows, "holdout")
                result = {
                    "checkpoint_minutes": checkpoint,
                    "conditions": " AND ".join(atom.text for atom in rule),
                    "condition_count": size,
                    **build,
                    **holdout,
                }
                build_margin = (
                    float(build["build_loser_remove_rate"])
                    - (1.0 - float(build["build_winner_keep_rate"]))
                )
                score = (
                    build_margin,
                    int(build["build_loser_removed"]),
                    int(build["build_winner_kept"]),
                    -size,
                )
                candidates.append((score, rule, result))
                census.append(result)
        candidates.sort(key=lambda item: item[0], reverse=True)
        for rank, (_, rule, result) in enumerate(candidates[:5], 1):
            pooled_winner_kept = int(result["build_winner_kept"]) + int(result["holdout_winner_kept"])
            pooled_loser_removed = int(result["build_loser_removed"]) + int(result["holdout_loser_removed"])
            build_margin = float(result["build_loser_remove_rate"]) - (
                1.0 - float(result["build_winner_keep_rate"])
            )
            holdout_margin = float(result["holdout_loser_remove_rate"]) - (
                1.0 - float(result["holdout_winner_keep_rate"])
            )
            selected.append(
                {
                    "build_rank_at_checkpoint": rank,
                    **result,
                    "pooled_winner_kept": pooled_winner_kept,
                    "pooled_loser_removed": pooled_loser_removed,
                    "pooled_meets_30_25": pooled_winner_kept >= 30 and pooled_loser_removed >= 25,
                    "build_separation_margin": round(build_margin, 6),
                    "holdout_separation_margin": round(holdout_margin, 6),
                    "holdout_result": "survived" if holdout_margin > 0 else "reversed/failed",
                    "_atoms": rule,
                }
            )
    return census, selected


def loser_path(entry: LockedEntry, quotes: Sequence[Quote]) -> LoserPath:
    tape = [quote for quote in quotes if entry.entry_ts <= quote.ts <= entry.natural_exit_ts]
    if not tape:
        raise RuntimeError(f"no natural-path quotes for {entry.key}")
    returns = [(quote.ts, (float(quote.bid) / entry.entry_px - 1.0) * 100.0) for quote in tape]
    peak_index = max(range(len(returns)), key=lambda index: returns[index][1])
    peak_ts, peak_return = returns[peak_index]
    post_peak_index = min(
        range(peak_index, len(returns)), key=lambda index: float(tape[index].bid)
    )
    low_quote = tape[post_peak_index]
    peak_bid = float(tape[peak_index].bid)

    def first_touch(level: float) -> tuple[bool, float | None]:
        match = next((ts for ts, value in returns if value >= level), None)
        return match is not None, (match - entry.entry_ts).total_seconds() / 60.0 if match else None

    touches = {level: first_touch(level) for level in (1.0, 2.0, 3.0, 4.0)}
    if peak_return < 1:
        bucket = "below +1"
    elif peak_return < 2:
        bucket = "+1 to +2"
    elif peak_return < 3:
        bucket = "+2 to +3"
    elif peak_return < 4:
        bucket = "+3 to +4"
    else:
        bucket = "+4 to +5"
    return LoserPath(
        session_day_et=entry.session_day_et,
        split=entry.split,
        symbol=entry.symbol,
        buy_signal_ts=entry.buy_signal_ts,
        entry_ts=entry.entry_ts,
        entry_px=entry.entry_px,
        max_up_pct=peak_return,
        max_down_pct=min(value for _, value in returns),
        upside_bucket=bucket,
        touched_plus_1=touches[1.0][0],
        minutes_to_plus_1=touches[1.0][1],
        touched_plus_2=touches[2.0][0],
        minutes_to_plus_2=touches[2.0][1],
        touched_plus_3=touches[3.0][0],
        minutes_to_plus_3=touches[3.0][1],
        touched_plus_4=touches[4.0][0],
        minutes_to_plus_4=touches[4.0][1],
        peak_ts=peak_ts,
        post_peak_low_ts=low_quote.ts,
        post_peak_drawdown_pct=(float(low_quote.bid) / peak_bid - 1.0) * 100.0,
        post_peak_low_vs_entry_pct=(float(low_quote.bid) / entry.entry_px - 1.0) * 100.0,
    )


def target_outcome(entry: LockedEntry, quotes: Sequence[Quote], target: float) -> dict:
    tape = [quote for quote in quotes if entry.entry_ts < quote.ts <= entry.natural_exit_ts]
    pending_stop = False
    for quote in tape:
        gain = (float(quote.bid) / entry.entry_px - 1.0) * 100.0
        if pending_stop:
            return {"return_pct": gain, "reason": "hard_stop", "exit_ts": quote.ts}
        if gain >= target:
            return {"return_pct": target, "reason": "target", "exit_ts": quote.ts}
        if gain <= -8.0:
            pending_stop = True
    return {
        "return_pct": entry.natural_return_pct,
        "reason": "atr_sell_or_close",
        "exit_ts": entry.natural_exit_ts,
    }


def path_measurements(source, entries: Sequence[LockedEntry]):
    grouped: dict[tuple[str, str], list[LockedEntry]] = defaultdict(list)
    for entry in entries:
        grouped[(entry.session_day_et, entry.symbol)].append(entry)
    losers: list[LoserPath] = []
    outcomes: list[dict] = []
    for (day_text, symbol), group in sorted(grouped.items()):
        session_day = date.fromisoformat(day_text)
        quotes = source.quotes(
            symbol,
            datetime.combine(session_day, time(7), EASTERN),
            datetime.combine(session_day, time(16), EASTERN),
        )
        for entry in group:
            if not entry.reached_5:
                losers.append(loser_path(entry, quotes))
            for target in TARGETS:
                outcome = target_outcome(entry, quotes, target)
                outcomes.append(
                    {
                        "session_day_et": entry.session_day_et,
                        "split": entry.split,
                        "symbol": entry.symbol,
                        "buy_signal_ts": entry.buy_signal_ts,
                        "entry_ts": entry.entry_ts,
                        "reached_5": entry.reached_5,
                        "target_pct": target,
                        "exit_reason": outcome["reason"],
                        "exit_ts": outcome["exit_ts"],
                        "return_pct": outcome["return_pct"],
                        "natural_max_up_pct": entry.natural_max_up_pct,
                        "ran_past_target": entry.natural_max_up_pct > target,
                        "available_upside_capped_pct": max(0.0, entry.natural_max_up_pct - target),
                    }
                )
    return losers, outcomes


def target_summary(outcomes: Sequence[dict]) -> list[dict]:
    result: list[dict] = []
    days = sorted({str(row["session_day_et"]) for row in outcomes})
    for target in TARGETS:
        for split in ("build", "holdout", "all", *days):
            rows = [
                row for row in outcomes
                if row["target_pct"] == target
                and (
                    split == "all"
                    or row["split"] == split
                    or row["session_day_et"] == split
                )
            ]
            costs = [float(row["available_upside_capped_pct"]) for row in rows if row["ran_past_target"]]
            result.append(
                {
                    "target_pct": target,
                    "split": split,
                    "trades": len(rows),
                    "targets_reached": sum(row["exit_reason"] == "target" for row in rows),
                    "hard_stops": sum(row["exit_reason"] == "hard_stop" for row in rows),
                    "total_return_pct": round(sum(float(row["return_pct"]) for row in rows), 4),
                    "mean_return_pct": round(statistics.fmean(float(row["return_pct"]) for row in rows), 4),
                    "trades_that_ran_past_target": len(costs),
                    "total_available_upside_capped_pct": round(sum(costs), 4),
                    "median_available_upside_capped_pct": round(statistics.median(costs), 4) if costs else None,
                    "max_available_upside_capped_pct": round(max(costs), 4) if costs else None,
                }
            )
    return result


def _fmt(value: float | None, suffix: str = "%") -> str:
    return "NA" if value is None else f"{value:+.2f}{suffix}"


def trade_explanations(
    entries: Sequence[LockedEntry], snapshots: Sequence[FeatureSnapshot], selected: Sequence[dict]
) -> list[dict]:
    by_key: dict[tuple[str, str], list[FeatureSnapshot]] = defaultdict(list)
    for row in snapshots:
        by_key[row.key].append(row)
    survivors = [
        row
        for row in selected
        if row["pooled_meets_30_25"] and row["holdout_result"] == "survived"
    ]
    top_rules = []
    for checkpoint in sorted({int(row["checkpoint_minutes"]) for row in survivors}):
        candidates = [
            row for row in survivors if int(row["checkpoint_minutes"]) == checkpoint
        ]
        top_rules.append(
            min(
                candidates,
                key=lambda row: (
                    int(row["condition_count"]), int(row["build_rank_at_checkpoint"])
                ),
            )
        )
    result: list[dict] = []
    for entry in entries:
        rows = sorted(by_key[entry.key], key=lambda item: item.checkpoint_minutes)
        states = []
        for row in rows:
            states.append(
                f"{row.checkpoint_minutes}m price {_fmt(row.price_vs_entry_pct)}, "
                f"range {_fmt(row.max_down_so_far_pct)} to {_fmt(row.max_up_so_far_pct)}, "
                f"vol {_fmt(row.volume_ratio_20, 'x')}, hist {row.macd_histogram_direction or 'NA'}, "
                f"dot {row.dot_consensus if row.dot_consensus is not None else 'NA'}, "
                f"ATR {row.atr_direction or 'NA'}, VWAP "
                f"{('above' if row.above_vwap else 'below') if row.above_vwap is not None else 'NA'}"
            )
        flags: list[str] = []
        for rule in top_rules:
            row = next(
                item for item in rows if item.checkpoint_minutes == int(rule["checkpoint_minutes"])
            )
            atoms = rule["_atoms"]
            values = [atom.evaluate(row) for atom in atoms]
            if values and all(value is True for value in values):
                flags.append(f"{row.checkpoint_minutes}m: {rule['conditions']}")
        if entry.reached_5:
            observed = "Reached +5%; " + "; ".join(states)
            early = "; ".join(flags) if flags else "no selected early failure combination present"
        else:
            observed = "Did not reach +5%; " + "; ".join(states)
            early = "; ".join(flags) if flags else "nothing visible"
        result.append(
            {
                "session_day_et": entry.session_day_et,
                "split": entry.split,
                "symbol": entry.symbol,
                "buy_signal_ts": entry.buy_signal_ts,
                "entry_ts": entry.entry_ts,
                "reached_5": entry.reached_5,
                "what_went_right_or_wrong": observed,
                "what_would_have_flagged_it_early": early,
            }
        )
    return result


def _plain_rule(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "_atoms"}


def _write_csv(path: Path, rows: Sequence[dict]) -> None:
    if not rows:
        return
    plain = [_plain_rule(dict(row)) for row in rows]
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


def write_report(
    path: Path,
    entries: Sequence[LockedEntry],
    snapshots: Sequence[FeatureSnapshot],
    selected: Sequence[dict],
    losers: Sequence[LoserPath],
    targets: Sequence[dict],
) -> None:
    build_w = sum(entry.reached_5 and entry.split == "build" for entry in entries)
    build_l = sum(not entry.reached_5 and entry.split == "build" for entry in entries)
    hold_w = sum(entry.reached_5 and entry.split == "holdout" for entry in entries)
    hold_l = sum(not entry.reached_5 and entry.split == "holdout" for entry in entries)
    complete = {
        checkpoint: sum(
            not row.missing_state for row in snapshots if row.checkpoint_minutes == checkpoint
        )
        for checkpoint in CHECKPOINTS
    }
    lines = [
        "# ATR Early-State Combination Study: 2026-08-24 to 2026-09-01",
        "",
        f"Locked population: **97 entries**. Build (Aug 24-28): {build_w} reached +5, "
        f"{build_l} did not. Untouched holdout (Aug 31 and Sep 1): {hold_w} reached +5, "
        f"{hold_l} did not.",
        "",
        "Each checkpoint uses the executable bid path from the ask fill and the exact Schwab "
        "strategy bar closing at that signal-relative minute. Indicators use the stored series: "
        "12/26/9 MACD histogram, TOS FastStochastic(10), Wilder RSI(14), exact three-row dot "
        "consensus, live replay ATR state, 04:00-anchored typical-price VWAP, and current volume "
        "divided by the live 20-bar average (including the current bar). No missing state is "
        "imputed.",
        "",
        "## Coverage",
        "",
        "| Checkpoint | Complete rows | Rows retained |",
        "|---:|---:|---:|",
    ]
    for checkpoint in CHECKPOINTS:
        lines.append(f"| {checkpoint} min | {complete[checkpoint]}/97 | 97/97 |")
    lines.extend([
        "",
        "## Build-Selected Pairs and Triples",
        "",
        "The table shows the five highest-ranked build-day rules at each checkpoint. The fail "
        "side is the AND of its conditions. Rules needed at least 85% coverage in each build "
        "population and the build-day proportional equivalent of keeping 30/42 winners while "
        "removing 25/55 losers. Ranking did not inspect holdout results.",
        "",
        "| Min | Conditions (fail side) | Build: W kept / L removed | Holdout: W kept / L removed | "
        "Unavailable B/H | Pooled 30/25 | Holdout |",
        "|---:|---|---:|---:|---:|:---:|---|",
    ])
    if not selected:
        lines.append("| - | No pair or triple met the build criterion | - | - | - | No | - |")
    for row in selected:
        lines.append(
            f"| {row['checkpoint_minutes']} | {row['conditions']} | "
            f"{row['build_winner_kept']}/{row['build_winner_total']} / "
            f"{row['build_loser_removed']}/{row['build_loser_total']} | "
            f"{row['holdout_winner_kept']}/{row['holdout_winner_total']} / "
            f"{row['holdout_loser_removed']}/{row['holdout_loser_total']} | "
            f"{row['build_unassessed']} / {row['holdout_unassessed']} | "
            f"{'Yes' if row['pooled_meets_30_25'] else 'No'} | {row['holdout_result']} |"
        )
    survivors = [row for row in selected if row["pooled_meets_30_25"] and row["holdout_result"] == "survived"]
    lines.extend([
        "",
        "A combination is called a survivor only when the unchanged pooled result keeps at least "
        "30 winners, removes at least 25 losers, and its holdout separation remains positive. "
        f"**Survivors among the preselected rules: {len(survivors)}.**",
        "",
        "## The 55 Trades Below +5%",
        "",
        "`After-peak fall` is the executable-bid drawdown from the trade's highest bid to the "
        "lowest later bid before ATR SELL/session close.",
        "",
        "| Maximum-up bucket | Build | Holdout | All |",
        "|---|---:|---:|---:|",
    ])
    for bucket in ("below +1", "+1 to +2", "+2 to +3", "+3 to +4", "+4 to +5"):
        b = sum(row.upside_bucket == bucket and row.split == "build" for row in losers)
        h = sum(row.upside_bucket == bucket and row.split == "holdout" for row in losers)
        lines.append(f"| {bucket} | {b} | {h} | {b + h} |")
    lines.extend([
        "",
        "| Threshold touched by the 55 | Build | Holdout | All |",
        "|---|---:|---:|---:|",
    ])
    for level in (1, 2, 3, 4):
        field = f"touched_plus_{level}"
        b = sum(bool(getattr(row, field)) and row.split == "build" for row in losers)
        h = sum(bool(getattr(row, field)) and row.split == "holdout" for row in losers)
        lines.append(f"| +{level}% | {b}/{build_l} | {h}/{hold_l} | {b+h}/55 |")
    lines.extend([
        "",
        "## Full Exit at +2%, +3%, or +4% With -8% Stop",
        "",
        "A target fills at the target price on first executable-bid touch. A -8% stop triggers "
        "on a bid and fills at the next captured bid, matching the prior study's live-style stop "
        "model. If neither occurs, the locked ATR SELL/session-close exit remains.",
        "",
        "| Target | Split | Reached | Stops | Total return | Mean | Ran farther | Available upside capped |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ])
    for row in targets:
        if row["split"] not in ("build", "holdout", "all"):
            continue
        lines.append(
            f"| +{row['target_pct']:g}% | {row['split']} | {row['targets_reached']}/{row['trades']} | "
            f"{row['hard_stops']} | {row['total_return_pct']:+.2f} pts | "
            f"{row['mean_return_pct']:+.2f}% | {row['trades_that_ran_past_target']} | "
            f"{row['total_available_upside_capped_pct']:.2f} pts |"
        )
    largest_runner = max(entries, key=lambda entry: entry.natural_max_up_pct)
    capped = ", ".join(
        f"+{target:g}% target: {largest_runner.natural_max_up_pct - target:.2f} points"
        for target in TARGETS
    )
    lines.extend([
        "",
        f"Largest runner: {largest_runner.symbol} on {largest_runner.session_day_et} had "
        f"{largest_runner.natural_max_up_pct:+.2f}% available before its ATR-segment exit. "
        f"Available upside above each fixed target was capped by {capped}.",
        "",
        "### Per-day target totals",
        "",
        "| Date | +2% reached / total return | +3% reached / total return | +4% reached / total return |",
        "|---|---:|---:|---:|",
    ])
    for day_text in sorted({entry.session_day_et for entry in entries}):
        by_target = {
            float(row["target_pct"]): row
            for row in targets if row["split"] == day_text
        }
        cells = []
        for target in TARGETS:
            row = by_target[target]
            cells.append(
                f"{row['targets_reached']}/{row['trades']} / {row['total_return_pct']:+.2f} pts"
            )
        lines.append(f"| {day_text} | " + " | ".join(cells) + " |")
    lines.extend([
        "",
        "The upside-cost column is `natural ATR-segment max-up minus target`, summed only for "
        "trades that ran beyond the target. It measures available upside capped, not a claim that "
        "the full maximum was executable as an exit.",
        "",
        "Exact 388 checkpoint rows, all 97 plain-language trade records, all 55 loser paths and "
        "touch times, the complete qualifying combination census, and every target outcome are "
        "in the companion CSV files.",
    ])
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--population-csv", type=Path,
        default=Path("analysis/reports/atr-trend-exit-2026-08-24-to-2026-09-01-trades.csv"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("analysis/reports"))
    args = parser.parse_args()

    from project_mai_tai.backtest.data import DbMarketDataSource
    from project_mai_tai.backtest.replay import build_replay_settings
    from project_mai_tai.db.session import build_session_factory
    from project_mai_tai.settings import get_settings

    entries = load_locked_entries(args.population_csv)
    base = get_settings()
    source = DbMarketDataSource(build_session_factory(base))
    settings = build_replay_settings(base=base)
    snapshots = run_snapshots(source, settings, entries)
    census, selected = search_combinations(snapshots)
    losers, outcomes = path_measurements(source, entries)
    targets = target_summary(outcomes)
    explanations = trade_explanations(entries, snapshots, selected)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = "atr-combination-study-2026-08-24-to-2026-09-01"
    _write_csv(args.output_dir / f"{stem}-snapshots.csv", [asdict(row) for row in snapshots])
    _write_csv(args.output_dir / f"{stem}-selected-rules.csv", selected)
    _write_csv(args.output_dir / f"{stem}-combination-census.csv", census)
    _write_csv(args.output_dir / f"{stem}-trade-explanations.csv", explanations)
    _write_csv(args.output_dir / f"{stem}-loser-paths.csv", [asdict(row) for row in losers])
    _write_csv(args.output_dir / f"{stem}-target-outcomes.csv", outcomes)
    _write_csv(args.output_dir / f"{stem}-target-summary.csv", targets)
    write_report(args.output_dir / f"{stem}.md", entries, snapshots, selected, losers, targets)
    (args.output_dir / f"{stem}.json").write_text(
        json.dumps(
            {
                "entries": [asdict(row) for row in entries],
                "snapshots": [asdict(row) for row in snapshots],
                "selected_rules": [_plain_rule(row) for row in selected],
                "loser_paths": [asdict(row) for row in losers],
                "target_summary": targets,
            },
            default=lambda value: value.isoformat() if isinstance(value, (date, datetime)) else str(value),
            indent=2,
        ) + "\n"
    )
    print(
        f"entries={len(entries)} snapshots={len(snapshots)} selected={len(selected)} "
        f"losers={len(losers)} outcomes={len(outcomes)} output={args.output_dir / stem}*"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
