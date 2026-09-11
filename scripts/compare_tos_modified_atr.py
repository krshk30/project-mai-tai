#!/usr/bin/env python3
"""Locate the first divergence between live v2 ATR and Thinkorswim Modified ATR shapes.

The calculation consumes the complete persisted preceding series before displaying the comparison
window. It evaluates four paths over the same persisted live bars:

* the real ``SchwabV2Strategy._update_atr_state`` implementation;
* an independent model of the live 04:00 ET reset plus its 90-second gap guard;
* Modified ATR with a 04:00 ET reset and no live-only gap guard; and
* Modified ATR over continuous history.

The two Thinkorswim shapes are both reported because chart session settings are part of the input.
A single matching candle cannot establish recursive ATR parity. The command refuses to query the
database during regular market hours.

Example, after 16:00 ET:

    python scripts/compare_tos_modified_atr.py --symbol DBGI \
      --compare-start 2026-09-10T11:30:00-04:00 \
      --end 2026-09-10T12:10:00-04:00
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
import math
import os
from typing import Iterable
from zoneinfo import ZoneInfo

import psycopg

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    SchwabV2Strategy,
)


ET = ZoneInfo("America/New_York")
ATR_PERIOD = 5
ATR_FACTOR = 3.5
LIVE_GAP_BOUND_MS = 90_000
# Primary specification:
# https://toslc.thinkorswim.com/center/reference/Tech-Indicators/studies-library/A-B/ATRTrailingStop.html
# https://toslc.thinkorswim.com/center/reference/thinkScript/Functions/Tech-Analysis/WildersAverage
# https://toslc.thinkorswim.com/center/reference/thinkScript/tutorials/Advanced/Chapter-12---Past-Offset-and-Prefetch
# Thinkorswim documents WildersAverage as using seven lengths of input prefetch. Modified true
# range itself needs the preceding ``period - 1`` high/low bars before its first valid input, so a
# comparison needs both populations before the first displayed bar.
TOS_WILDERS_PREFETCH_INPUTS = 7 * ATR_PERIOD
MODIFIED_TR_PREFIX_BARS = ATR_PERIOD - 1
TOS_RAW_PREFETCH_BARS = TOS_WILDERS_PREFETCH_INPUTS + MODIFIED_TR_PREFIX_BARS

BAR_SQL = """
SELECT
    extract(epoch FROM bar_time) * 1000,
    open_price,
    high_price,
    low_price,
    close_price,
    volume
FROM strategy_bar_history
WHERE strategy_code = 'schwab_1m_v2'
  AND interval_secs = 60
  AND source = 'live'
  AND symbol = %(symbol)s
  AND bar_time < %(end)s
ORDER BY bar_time, id
"""


@dataclass(frozen=True)
class AtrStage:
    bar: OHLCVBar
    session_anchor_ms: int
    reset: bool
    gap_ms: int
    high_low: float
    modified_tr: float | None
    wilders: float | None
    loss: float | None
    trail: float | None
    state: str | None
    flip: str | None


def parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def refuse_regular_market_hours(now: datetime | None = None) -> None:
    current = (now or datetime.now(UTC)).astimezone(ET)
    if current.weekday() < 5 and time(9, 30) <= current.time() < time(16, 0):
        raise SystemExit("refusing historical ATR query during 09:30-16:00 ET; run after the close")


def session_anchor_ms(timestamp_ms: int) -> int:
    current = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).astimezone(ET)
    anchor_date = (
        current.date() if current.time() >= time(4, 0) else current.date() - timedelta(days=1)
    )
    anchor = datetime.combine(anchor_date, time(4, 0), tzinfo=ET)
    return int(anchor.timestamp() * 1000)


def _modified_true_range(
    *,
    current: OHLCVBar,
    previous: OHLCVBar,
    average_high_low: float,
) -> float:
    high_low = current.high - current.low
    capped_high_low = min(high_low, 1.5 * average_high_low)
    high_reference = (
        current.high - previous.close
        if current.low <= previous.high
        else (current.high - previous.close) - 0.5 * (current.low - previous.high)
    )
    low_reference = (
        previous.close - current.low
        if current.high >= previous.low
        else (previous.close - current.low) - 0.5 * (previous.low - current.high)
    )
    return max(capped_high_low, high_reference, low_reference)


def calculate_modified_atr(
    bars: Iterable[OHLCVBar],
    *,
    session_sliced: bool,
    guard_bar_gaps: bool,
    period: int = ATR_PERIOD,
    factor: float = ATR_FACTOR,
) -> list[AtrStage]:
    """Independent Modified-ATR implementation with explicit init/session/gap choices."""

    stages: list[AtrStage] = []
    high_lows: deque[float] = deque(maxlen=period)
    tr_seed: list[float] = []
    previous: OHLCVBar | None = None
    wilders: float | None = None
    state: str | None = None
    trail: float | None = None
    active_anchor = 0

    for bar in bars:
        anchor = session_anchor_ms(bar.timestamp_ms)
        boundary_changed = bool(session_sliced and anchor != active_anchor)
        reset = bool(active_anchor and boundary_changed)
        if boundary_changed:
            high_lows = deque(maxlen=period)
            tr_seed = []
            previous = None
            wilders = None
            state = None
            trail = None
        active_anchor = anchor

        high_low = bar.high - bar.low
        high_lows.append(high_low)
        gap_ms = bar.timestamp_ms - previous.timestamp_ms if previous is not None else 0
        modified_tr: float | None = None
        if len(high_lows) == period and previous is not None:
            average_high_low = sum(high_lows) / period
            modified_tr = _modified_true_range(
                current=bar,
                previous=previous,
                average_high_low=average_high_low,
            )
            if guard_bar_gaps and gap_ms > LIVE_GAP_BOUND_MS:
                modified_tr = min(high_low, 1.5 * average_high_low)

        if modified_tr is not None:
            if wilders is None:
                tr_seed.append(modified_tr)
                if len(tr_seed) == period:
                    wilders = sum(tr_seed) / period
            else:
                wilders = wilders + (modified_tr - wilders) / period

        loss = factor * wilders if wilders is not None else None
        flip: str | None = None
        if loss is not None:
            if state is None:
                state = "long"
                trail = bar.close - loss
            elif state == "long":
                if bar.close > float(trail):
                    trail = max(float(trail), bar.close - loss)
                else:
                    state = "short"
                    trail = bar.close + loss
                    flip = "SELL"
            elif bar.close < float(trail):
                trail = min(float(trail), bar.close + loss)
            else:
                state = "long"
                trail = bar.close - loss
                flip = "BUY"

        stages.append(
            AtrStage(
                bar=bar,
                session_anchor_ms=anchor,
                reset=reset,
                gap_ms=gap_ms,
                high_low=high_low,
                modified_tr=modified_tr,
                wilders=wilders,
                loss=loss,
                trail=trail,
                state=state,
                flip=flip,
            )
        )
        previous = bar
    return stages


def calculate_live_strategy(bars: Iterable[OHLCVBar]) -> list[AtrStage]:
    """Run the production strategy method and expose its intermediate Wilder/TR stages."""

    settings = Settings(
        _env_file=None,
        strategy_schwab_1m_v2_atr_flip_enabled=True,
        strategy_schwab_1m_v2_atr_flip_period=ATR_PERIOD,
        strategy_schwab_1m_v2_atr_flip_factor=ATR_FACTOR,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=False,
    )
    strategy = SchwabV2Strategy(settings)
    state = strategy.watchlist_state("ATR-COMPARE")
    stages: list[AtrStage] = []

    for bar in bars:
        old_anchor = state.atr_session_anchor_ms
        old_wilders = state.atr_wilders
        old_seed_len = len(state.atr_tr_seed)
        old_prev = state.atr_prev_bar
        state.bars.append(bar)
        signal = strategy._update_atr_state(state, bar, observation_phase="replay")
        reset = bool(old_anchor and old_anchor != state.atr_session_anchor_ms)
        gap_ms = bar.timestamp_ms - old_prev.timestamp_ms if old_prev is not None else 0

        modified_tr: float | None = None
        if len(state.atr_tr_seed) > old_seed_len:
            modified_tr = state.atr_tr_seed[-1]
        elif old_wilders is not None and state.atr_wilders is not None:
            modified_tr = state.atr_wilders * ATR_PERIOD - old_wilders * (ATR_PERIOD - 1)

        stages.append(
            AtrStage(
                bar=bar,
                session_anchor_ms=state.atr_session_anchor_ms,
                reset=reset,
                gap_ms=gap_ms,
                high_low=bar.high - bar.low,
                modified_tr=modified_tr,
                wilders=state.atr_wilders,
                loss=None if signal is None else float(signal["loss"]),
                trail=state.atr_trail,
                state=state.atr_state,
                flip=None if signal is None else signal["flip"],
            )
        )
    return stages


def _different(left: float | None, right: float | None, tolerance: float) -> bool:
    if left is None or right is None:
        return left is not right
    return not math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)


def first_divergence(
    left: list[AtrStage],
    right: list[AtrStage],
    *,
    tolerance: float = 1e-8,
) -> tuple[int, str] | None:
    if len(left) != len(right):
        return min(len(left), len(right)), "input_length"
    for index, (lhs, rhs) in enumerate(zip(left, right, strict=True)):
        if lhs.bar != rhs.bar:
            return index, "input_ohlcv"
        if lhs.reset != rhs.reset or lhs.session_anchor_ms != rhs.session_anchor_ms:
            return index, "session_boundary"
        if _different(lhs.modified_tr, rhs.modified_tr, tolerance):
            return index, "modified_true_range"
        if _different(lhs.wilders, rhs.wilders, tolerance):
            return index, "wilders_seed_or_update"
        if _different(lhs.loss, rhs.loss, tolerance):
            return index, "atr_loss"
        if (
            _different(lhs.trail, rhs.trail, tolerance)
            or lhs.state != rhs.state
            or lhs.flip != rhs.flip
        ):
            return index, "trail_state_or_flip"
    return None


def rows_from_query(records: Iterable[tuple[object, ...]]) -> list[OHLCVBar]:
    return [
        OHLCVBar(
            timestamp_ms=int(record[0]),
            open=float(record[1]),
            high=float(record[2]),
            low=float(record[3]),
            close=float(record[4]),
            volume=int(record[5] or 0),
        )
        for record in records
    ]


def _dsn(value: str | None) -> str:
    raw = value or os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not raw:
        raise SystemExit("no DSN: pass --dsn or set MAI_TAI_DATABASE_URL")
    return raw.replace("postgresql+psycopg://", "postgresql://")


def _format_value(value: float | None) -> str:
    return "-" if value is None else f"{value:.6f}"


def _print_divergence(
    label: str,
    actual: list[AtrStage],
    comparison: list[AtrStage],
) -> None:
    divergence = first_divergence(actual, comparison)
    if divergence is None:
        print(f"{label}: MATCH bars={len(actual)}/{len(comparison)}")
        return
    index, stage = divergence
    row = actual[index] if index < len(actual) else comparison[index]
    at = datetime.fromtimestamp(row.bar.timestamp_ms / 1000, tz=UTC).astimezone(ET)
    print(
        f"{label}: DIVERGED stage={stage} index={index} at={at.isoformat()} "
        f"bars={len(actual)}/{len(comparison)}"
    )
    if index < len(actual) and index < len(comparison):
        lhs = actual[index]
        rhs = comparison[index]
        print(
            "  actual="
            f"reset:{int(lhs.reset)},tr:{_format_value(lhs.modified_tr)},"
            f"wilders:{_format_value(lhs.wilders)},loss:{_format_value(lhs.loss)},"
            f"trail:{_format_value(lhs.trail)},state:{lhs.state or '-'},flip:{lhs.flip or '-'}"
        )
        print(
            "  comparison="
            f"reset:{int(rhs.reset)},tr:{_format_value(rhs.modified_tr)},"
            f"wilders:{_format_value(rhs.wilders)},loss:{_format_value(rhs.loss)},"
            f"trail:{_format_value(rhs.trail)},state:{rhs.state or '-'},flip:{rhs.flip or '-'}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--compare-start", required=True, type=parse_instant)
    parser.add_argument("--end", required=True, type=parse_instant)
    parser.add_argument("--dsn")
    args = parser.parse_args()
    if args.compare_start >= args.end:
        parser.error("require compare-start < end")
    refuse_regular_market_hours()

    with psycopg.connect(_dsn(args.dsn)) as connection, connection.cursor() as cursor:
        cursor.execute(
            BAR_SQL,
            {
                "symbol": args.symbol.upper(),
                "end": args.end,
            },
        )
        bars = rows_from_query(cursor.fetchall())
    if not bars:
        print("STATUS=UNMEASURED reason=no_live_strategy_bars denominator=0")
        return 2

    actual = calculate_live_strategy(bars)
    independent_live = calculate_modified_atr(bars, session_sliced=True, guard_bar_gaps=True)
    tos_session = calculate_modified_atr(bars, session_sliced=True, guard_bar_gaps=False)
    tos_continuous = calculate_modified_atr(bars, session_sliced=False, guard_bar_gaps=False)
    display_indexes = [
        index
        for index, bar in enumerate(bars)
        if bar.timestamp_ms >= int(args.compare_start.timestamp() * 1000)
    ]
    gaps = [stage for stage in actual if stage.gap_ms > LIVE_GAP_BOUND_MS]
    anchors = {stage.session_anchor_ms for stage in actual}
    first_display_index = display_indexes[0] if display_indexes else len(bars)
    preceding_bars = first_display_index
    first_bar_at = datetime.fromtimestamp(bars[0].timestamp_ms / 1000, tz=UTC).astimezone(ET)
    print(
        f"ATR COMPARISON symbol={args.symbol.upper()} "
        f"persisted_history={first_bar_at.isoformat()}..{args.end.astimezone(ET).isoformat()} "
        f"display_from={args.compare_start.astimezone(ET).isoformat()}"
    )
    print(
        f"DENOMINATORS total_input_bars={len(bars)} preceding_bars={preceding_bars} "
        f"tos_wilders_prefetch_inputs={TOS_WILDERS_PREFETCH_INPUTS} "
        f"modified_tr_prefix_bars={MODIFIED_TR_PREFIX_BARS} "
        f"raw_prefetch_required={TOS_RAW_PREFETCH_BARS} "
        f"displayed_bars={len(display_indexes)} sessions={len(anchors)} "
        f"nonadjacent_pairs={len(gaps)}"
    )
    if not display_indexes:
        print("STATUS=UNMEASURED reason=no_bars_in_display_window denominator=0")
        return 2
    if preceding_bars < TOS_RAW_PREFETCH_BARS:
        print(
            "STATUS=UNMEASURED reason=insufficient_tos_wilders_prefetch "
            f"preceding_bars={preceding_bars}/{TOS_RAW_PREFETCH_BARS}"
        )
        return 2
    _print_divergence("actual_live_vs_independent_live", actual, independent_live)
    _print_divergence("actual_live_vs_tos_modified_session_sliced", actual, tos_session)
    _print_divergence("actual_live_vs_tos_modified_continuous", actual, tos_continuous)

    print("\nDISPLAY WINDOW (ET)")
    print("time                     close live_tr live_wilder live_trail live_state live_flip")
    for index in display_indexes:
        stage = actual[index]
        at = datetime.fromtimestamp(stage.bar.timestamp_ms / 1000, tz=UTC).astimezone(ET)
        print(
            f"{at.isoformat():25} {stage.bar.close:7.4f} "
            f"{_format_value(stage.modified_tr):>9} {_format_value(stage.wilders):>11} "
            f"{_format_value(stage.trail):>10} {str(stage.state or '-'):10} "
            f"{str(stage.flip or '-')}"
        )

    integrity = first_divergence(actual, independent_live)
    if integrity is not None:
        print(f"STATUS=INSTRUMENT_ERROR first_stage={integrity[1]}")
        return 2
    print("STATUS=COMPLETE live_reference_parity=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
