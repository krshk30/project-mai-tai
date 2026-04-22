from __future__ import annotations

import argparse
import html
import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from project_mai_tai.db.models import StrategyBarHistory, Strategy, TradeIntent

EASTERN_TZ = ZoneInfo("America/New_York")


@dataclass
class BarRow:
    bar_time: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: int
    decision_status: str
    decision_reason: str
    decision_path: str
    decision_score: str
    indicators: dict[str, object]


@dataclass
class IntentRow:
    created_at: datetime
    side: str
    intent_type: str
    quantity: float
    reason: str
    status: str


@dataclass
class ReviewMarker:
    bar_time: datetime
    price: float
    category: str
    label: str
    note: str
    reason: str


@dataclass
class ReviewSetup:
    start_time: datetime
    end_time: datetime
    category: str
    reason: str
    bars: int
    first_price: float
    last_price: float
    note: str


@dataclass
class ActualOutcome:
    event_time: datetime
    bar_time: datetime
    category: str
    reason: str
    price: float
    note: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a Mai Tai live day review chart.")
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--strategy", default="macd_1m")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--date", required=True, help="ET date in YYYY-MM-DD")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--lookahead-bars", type=int, default=10)
    parser.add_argument("--target-up-pct", type=float, default=2.0)
    parser.add_argument("--stop-down-pct", type=float, default=1.0)
    return parser.parse_args()


def _et_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0), tzinfo=EASTERN_TZ)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _load_bars(
    session: Session,
    *,
    strategy_code: str,
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[BarRow]:
    rows = session.execute(
        select(
            StrategyBarHistory.bar_time,
            StrategyBarHistory.open_price,
            StrategyBarHistory.high_price,
            StrategyBarHistory.low_price,
            StrategyBarHistory.close_price,
            StrategyBarHistory.volume,
            StrategyBarHistory.decision_status,
            StrategyBarHistory.decision_reason,
            StrategyBarHistory.decision_path,
            StrategyBarHistory.decision_score,
            StrategyBarHistory.indicators_json,
        ).where(
            StrategyBarHistory.strategy_code == strategy_code,
            StrategyBarHistory.symbol == symbol,
            StrategyBarHistory.bar_time >= start_utc,
            StrategyBarHistory.bar_time < end_utc,
        ).order_by(StrategyBarHistory.bar_time.asc())
    ).all()
    return [
        BarRow(
            bar_time=row.bar_time,
            open_price=float(row.open_price),
            high_price=float(row.high_price),
            low_price=float(row.low_price),
            close_price=float(row.close_price),
            volume=int(row.volume),
            decision_status=row.decision_status or "",
            decision_reason=row.decision_reason or "",
            decision_path=row.decision_path or "",
            decision_score=row.decision_score or "",
            indicators=dict(row.indicators_json or {}),
        )
        for row in rows
    ]


def _load_intents(
    session: Session,
    *,
    strategy_code: str,
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[IntentRow]:
    rows = session.execute(
        select(
            TradeIntent.created_at,
            TradeIntent.side,
            TradeIntent.intent_type,
            TradeIntent.quantity,
            TradeIntent.reason,
            TradeIntent.status,
        )
        .join(Strategy, Strategy.id == TradeIntent.strategy_id)
        .where(
            Strategy.code == strategy_code,
            TradeIntent.symbol == symbol,
            TradeIntent.created_at >= start_utc,
            TradeIntent.created_at < end_utc,
        )
        .order_by(TradeIntent.created_at.asc())
    ).all()
    return [
        IntentRow(
            created_at=row.created_at,
            side=row.side,
            intent_type=row.intent_type,
            quantity=float(row.quantity),
            reason=row.reason or "",
            status=row.status or "",
        )
        for row in rows
    ]


def _future_review(
    bars: list[BarRow],
    *,
    lookahead_bars: int,
    target_up_pct: float,
    stop_down_pct: float,
) -> list[ReviewMarker]:
    markers: list[ReviewMarker] = []
    up_mult = 1.0 + (target_up_pct / 100.0)
    down_mult = 1.0 - (stop_down_pct / 100.0)
    skip_reasons = {
        "",
        "no entry path matched",
        "position open",
        "awaiting open fill",
    }
    for index, bar in enumerate(bars):
        reason = (bar.decision_reason or "").strip()
        if bar.decision_status != "blocked":
            continue
        if not bar.decision_path and reason in skip_reasons:
            continue
        if index + 1 >= len(bars):
            continue
        future = bars[index + 1 : index + 1 + lookahead_bars]
        if not future:
            continue
        entry = bar.close_price
        target_price = entry * up_mult
        stop_price = entry * down_mult
        target_index: int | None = None
        stop_index: int | None = None
        for offset, future_bar in enumerate(future, start=1):
            if target_index is None and future_bar.high_price >= target_price:
                target_index = offset
            if stop_index is None and future_bar.low_price <= stop_price:
                stop_index = offset
        if target_index is None and stop_index is None:
            continue
        if target_index is not None and (stop_index is None or target_index < stop_index):
            markers.append(
                ReviewMarker(
                    bar_time=bar.bar_time,
                    price=bar.close_price,
                    category="should_enter",
                    label="H",
                    note=(
                        f"Blocked/had no signal but reached +{target_up_pct:.1f}% "
                        f"within {target_index} bars before -{stop_down_pct:.1f}%."
                    ),
                    reason=reason or "(unspecified)",
                )
            )
        elif stop_index is not None and (target_index is None or stop_index <= target_index):
            markers.append(
                ReviewMarker(
                    bar_time=bar.bar_time,
                    price=bar.close_price,
                    category="should_not_enter",
                    label="N",
                    note=(
                        f"Blocked/had no signal and hit -{stop_down_pct:.1f}% "
                        f"within {stop_index} bars before +{target_up_pct:.1f}%."
                    ),
                    reason=reason or "(unspecified)",
                )
            )
    return markers


def _group_review_markers(markers: list[ReviewMarker]) -> list[ReviewSetup]:
    if not markers:
        return []
    ordered = sorted(markers, key=lambda item: item.bar_time)
    setups: list[ReviewSetup] = []
    current: ReviewSetup | None = None
    for marker in ordered:
        if current is None:
            current = ReviewSetup(
                start_time=marker.bar_time,
                end_time=marker.bar_time,
                category=marker.category,
                reason=marker.reason,
                bars=1,
                first_price=marker.price,
                last_price=marker.price,
                note=marker.note,
            )
            continue
        gap = marker.bar_time - current.end_time
        same_setup = (
            marker.category == current.category
            and marker.reason == current.reason
            and gap <= timedelta(minutes=3)
        )
        if same_setup:
            current.end_time = marker.bar_time
            current.bars += 1
            current.last_price = marker.price
            continue
        setups.append(current)
        current = ReviewSetup(
            start_time=marker.bar_time,
            end_time=marker.bar_time,
            category=marker.category,
            reason=marker.reason,
            bars=1,
            first_price=marker.price,
            last_price=marker.price,
            note=marker.note,
        )
    if current is not None:
        setups.append(current)
    return setups


def _fmt_et(ts: datetime) -> str:
    return ts.astimezone(EASTERN_TZ).strftime("%I:%M:%S %p ET")


def _find_bar_index(
    bars: list[BarRow],
    ts: datetime,
    *,
    prefer_completed_bar: bool = False,
) -> int | None:
    ts_utc = ts.astimezone(UTC)
    # Strategy intents may be persisted either on the scored bar itself or on
    # the next bar start, depending on the runtime path. Prefer an exact
    # timestamp match first so replay analysis stays aligned for live-bar paths.
    for index, bar in enumerate(bars):
        if bar.bar_time == ts_utc:
            return index

    last_match: int | None = None
    for index, bar in enumerate(bars):
        if prefer_completed_bar:
            if bar.bar_time < ts_utc:
                last_match = index
                continue
            break
        if bar.bar_time <= ts_utc:
            last_match = index
            continue
        break
    return last_match


def _classify_forward_outcome(
    *,
    entry_price: float,
    future: list[BarRow],
    target_up_pct: float,
    stop_down_pct: float,
) -> tuple[str, str]:
    up_mult = 1.0 + (target_up_pct / 100.0)
    down_mult = 1.0 - (stop_down_pct / 100.0)
    target_price = entry_price * up_mult
    stop_price = entry_price * down_mult
    target_index: int | None = None
    stop_index: int | None = None
    for offset, future_bar in enumerate(future, start=1):
        if target_index is None and future_bar.high_price >= target_price:
            target_index = offset
        if stop_index is None and future_bar.low_price <= stop_price:
            stop_index = offset
    if target_index is None and stop_index is None:
        return ("open", f"Did not hit +{target_up_pct:.1f}% or -{stop_down_pct:.1f}% within lookahead.")
    if target_index is not None and (stop_index is None or target_index < stop_index):
        return ("good", f"Reached +{target_up_pct:.1f}% within {target_index} bars before -{stop_down_pct:.1f}%.")
    if stop_index is not None and (target_index is None or stop_index <= target_index):
        return ("bad", f"Hit -{stop_down_pct:.1f}% within {stop_index} bars before +{target_up_pct:.1f}%.")
    return ("open", "Outcome unresolved in lookahead window.")


def _classify_actual_outcomes(
    bars: list[BarRow],
    intents: list[IntentRow],
    *,
    lookahead_bars: int,
    target_up_pct: float,
    stop_down_pct: float,
) -> list[ActualOutcome]:
    outcomes: list[ActualOutcome] = []
    for intent in intents:
        if intent.status != "filled" or intent.side != "buy" or intent.intent_type != "open":
            continue
        # Strategy intents are created when the next bar begins processing, so
        # the scored decision usually belongs to the most recently completed bar.
        bar_index = _find_bar_index(bars, intent.created_at, prefer_completed_bar=True)
        if bar_index is None:
            continue
        bar = bars[bar_index]
        future = bars[bar_index + 1 : bar_index + 1 + lookahead_bars]
        outcome, note = _classify_forward_outcome(
            entry_price=bar.close_price,
            future=future,
            target_up_pct=target_up_pct,
            stop_down_pct=stop_down_pct,
        )
        outcomes.append(
            ActualOutcome(
                event_time=intent.created_at,
                bar_time=bar.bar_time,
                category={
                    "good": "taken_good",
                    "bad": "taken_bad",
                    "open": "taken_open",
                }[outcome],
                reason=intent.reason or "(actual entry)",
                price=bar.close_price,
                note=note,
            )
        )
    return outcomes


def _price_to_y(price: float, min_price: float, max_price: float, top: float, height: float) -> float:
    if math.isclose(max_price, min_price):
        return top + height / 2
    return top + (max_price - price) / (max_price - min_price) * height


def _x_for(index: int, total: int, left: float, width: float) -> float:
    if total <= 1:
        return left + width / 2
    return left + (index / (total - 1)) * width


def _polyline(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def _render_chart(
    *,
    strategy_code: str,
    symbol: str,
    bars: list[BarRow],
    intents: list[IntentRow],
    review_markers: list[ReviewMarker],
    actual_outcomes: list[ActualOutcome],
) -> str:
    width = 1500
    height = 980
    margin_left = 70
    margin_right = 30
    chart_width = width - margin_left - margin_right
    price_top = 90
    price_height = 480
    macd_top = 650
    macd_height = 200

    min_price = min(bar.low_price for bar in bars)
    max_price = max(bar.high_price for bar in bars)
    pad = (max_price - min_price) * 0.08 if max_price > min_price else 0.05
    min_price -= pad
    max_price += pad

    macd_values = [float(bar.indicators.get("macd", 0) or 0) for bar in bars]
    signal_values = [float(bar.indicators.get("signal", 0) or 0) for bar in bars]
    hist_values = [float(bar.indicators.get("histogram", 0) or 0) for bar in bars]
    min_macd = min(min(macd_values), min(signal_values), min(hist_values), 0.0)
    max_macd = max(max(macd_values), max(signal_values), max(hist_values), 0.0)
    macd_pad = (max_macd - min_macd) * 0.1 if max_macd > min_macd else 0.01
    min_macd -= macd_pad
    max_macd += macd_pad

    close_points: list[tuple[float, float]] = []
    ema9_points: list[tuple[float, float]] = []
    ema20_points: list[tuple[float, float]] = []
    vwap_points: list[tuple[float, float]] = []
    macd_points: list[tuple[float, float]] = []
    signal_points: list[tuple[float, float]] = []
    bar_lookup: dict[str, tuple[int, BarRow]] = {}

    for index, bar in enumerate(bars):
        x = _x_for(index, len(bars), margin_left, chart_width)
        close_points.append((x, _price_to_y(bar.close_price, min_price, max_price, price_top, price_height)))
        ema9 = float(bar.indicators.get("ema9", 0) or 0)
        ema20 = float(bar.indicators.get("ema20", 0) or 0)
        vwap = float(bar.indicators.get("vwap", 0) or 0)
        ema9_points.append((x, _price_to_y(ema9, min_price, max_price, price_top, price_height)))
        ema20_points.append((x, _price_to_y(ema20, min_price, max_price, price_top, price_height)))
        vwap_points.append((x, _price_to_y(vwap, min_price, max_price, price_top, price_height)))

        macd_y = _price_to_y(float(bar.indicators.get("macd", 0) or 0), min_macd, max_macd, macd_top, macd_height)
        signal_y = _price_to_y(float(bar.indicators.get("signal", 0) or 0), min_macd, max_macd, macd_top, macd_height)
        macd_points.append((x, macd_y))
        signal_points.append((x, signal_y))
        bar_lookup[_fmt_et(bar.bar_time)] = (index, bar)

    price_zero = _price_to_y(0, min_price, max_price, price_top, price_height)
    macd_zero = _price_to_y(0, min_macd, max_macd, macd_top, macd_height)

    marker_colors = {
        "actual_entry": "#22c55e",
        "actual_exit": "#ef4444",
        "should_enter": "#f59e0b",
        "should_not_enter": "#94a3b8",
        "taken_good": "#10b981",
        "taken_bad": "#f43f5e",
        "taken_open": "#38bdf8",
    }

    actual_markers: list[ReviewMarker] = []
    for intent in intents:
        if intent.status != "filled":
            continue
        label = "B" if intent.side == "buy" and intent.intent_type == "open" else "S"
        category = "actual_entry" if label == "B" else "actual_exit"
        note = f"{intent.reason} | {intent.side} {intent.intent_type} {intent.quantity:.0f} | {intent.status}"
        actual_markers.append(
            ReviewMarker(
                bar_time=intent.created_at,
                price=0.0,
                category=category,
                label=label,
                note=note,
                reason=intent.reason or "",
            )
        )

    review_setups = _group_review_markers(review_markers)
    classified_actual_markers = [
        ReviewMarker(
            bar_time=item.bar_time,
            price=item.price,
            category=item.category,
            label="G" if item.category == "taken_good" else ("B" if item.category == "taken_bad" else "O"),
            note=item.note,
            reason=item.reason,
        )
        for item in actual_outcomes
    ]

    marker_svg: list[str] = []
    for marker in classified_actual_markers + review_markers:
        index = _find_bar_index(bars, marker.bar_time)
        if index is None:
            continue
        bar = bars[index]
        x = _x_for(index, len(bars), margin_left, chart_width)
        price = marker.price if marker.price > 0 else bar.close_price
        y = _price_to_y(price, min_price, max_price, price_top, price_height)
        color = marker_colors[marker.category]
        tooltip = html.escape(f"{_fmt_et(marker.bar_time)} | {marker.label} | {marker.reason} | {marker.note}")
        marker_svg.append(
            (
                f'<circle cx="{x:.2f}" cy="{y:.2f}" r="8" fill="{color}" stroke="#0f172a" stroke-width="2">'
                f"<title>{tooltip}</title></circle>"
                f'<text x="{x:.2f}" y="{y + 4:.2f}" text-anchor="middle" font-size="10" fill="#0f172a" '
                f'font-weight="700">{html.escape(marker.label)}</text>'
            )
        )

    hist_svg: list[str] = []
    bar_width = max(chart_width / max(len(bars), 1) * 0.7, 1.5)
    for index, bar in enumerate(bars):
        x = _x_for(index, len(bars), margin_left, chart_width)
        hist = float(bar.indicators.get("histogram", 0) or 0)
        y = _price_to_y(hist, min_macd, max_macd, macd_top, macd_height)
        height_px = abs(y - macd_zero)
        top_y = min(y, macd_zero)
        color = "#22c55e" if hist >= 0 else "#ef4444"
        hist_svg.append(
            f'<rect x="{x - bar_width / 2:.2f}" y="{top_y:.2f}" width="{bar_width:.2f}" height="{max(height_px, 1):.2f}" fill="{color}" opacity="0.45" />'
        )

    summary = {
        "bars": len(bars),
        "actual_entries": len(actual_outcomes),
        "taken_good": sum(1 for item in actual_outcomes if item.category == "taken_good"),
        "taken_bad": sum(1 for item in actual_outcomes if item.category == "taken_bad"),
        "taken_open": sum(1 for item in actual_outcomes if item.category == "taken_open"),
        "missed_good": sum(1 for setup in review_setups if setup.category == "should_enter"),
        "missed_bad": sum(1 for setup in review_setups if setup.category == "should_not_enter"),
    }

    reason_summary: dict[str, dict[str, int]] = {}
    for setup in review_setups:
        bucket = reason_summary.setdefault(
            setup.reason,
            {
                "should_enter_setups": 0,
                "should_not_enter_setups": 0,
                "should_enter_bars": 0,
                "should_not_enter_bars": 0,
            },
        )
        if setup.category == "should_enter":
            bucket["should_enter_setups"] += 1
            bucket["should_enter_bars"] += setup.bars
        else:
            bucket["should_not_enter_setups"] += 1
            bucket["should_not_enter_bars"] += setup.bars
    reason_rows = []
    for reason, counts in sorted(
        reason_summary.items(),
        key=lambda item: (item[1]["should_enter_setups"] + item[1]["should_not_enter_setups"]),
        reverse=True,
    ):
        total_setups = counts["should_enter_setups"] + counts["should_not_enter_setups"]
        reason_rows.append(
            "<tr>"
            f"<td>{html.escape(reason)}</td>"
            f"<td>{total_setups}</td>"
            f"<td>{counts['should_enter_setups']}</td>"
            f"<td>{counts['should_not_enter_setups']}</td>"
            f"<td>{counts['should_enter_bars']}</td>"
            f"<td>{counts['should_not_enter_bars']}</td>"
            "</tr>"
        )
    if not reason_rows:
        reason_rows.append('<tr><td colspan="6" class="muted">No blocked candidate setups found.</td></tr>')

    actual_rows = []
    for item in actual_outcomes:
        actual_rows.append(
            "<tr>"
            f"<td>{html.escape(_fmt_et(item.event_time))}</td>"
            f"<td>{html.escape(item.reason)}</td>"
            f"<td>{html.escape(item.category.replace('_', ' '))}</td>"
            f"<td>{item.price:.4f}</td>"
            f"<td>{html.escape(item.note)}</td>"
            "</tr>"
        )
    if not actual_rows:
        actual_rows.append('<tr><td colspan="5" class="muted">No actual filled entries found.</td></tr>')

    setup_rows = []
    for setup in review_setups:
        setup_rows.append(
            "<tr>"
            f"<td>{html.escape(_fmt_et(setup.start_time))}</td>"
            f"<td>{html.escape(_fmt_et(setup.end_time))}</td>"
            f"<td>{html.escape(setup.reason)}</td>"
            f"<td>{html.escape(setup.category.replace('_', ' '))}</td>"
            f"<td>{setup.bars}</td>"
            f"<td>{setup.first_price:.4f}</td>"
            f"<td>{setup.last_price:.4f}</td>"
            "</tr>"
        )
    if not setup_rows:
        setup_rows.append('<tr><td colspan="7" class="muted">No setup-level review markers for this window.</td></tr>')

    review_rows = []
    for marker in sorted(review_markers, key=lambda item: item.bar_time):
        review_rows.append(
            "<tr>"
            f"<td>{html.escape(_fmt_et(marker.bar_time))}</td>"
            f"<td>{html.escape(marker.reason)}</td>"
            f"<td>{html.escape(marker.category.replace('_', ' '))}</td>"
            f"<td>{marker.price:.4f}</td>"
            f"<td>{html.escape(marker.note)}</td>"
            "</tr>"
        )
    if not review_rows:
        review_rows.append('<tr><td colspan="5" class="muted">No review markers for this window.</td></tr>')

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Mai Tai Review | {html.escape(strategy_code)} | {html.escape(symbol)}</title>
  <style>
    :root {{
      --bg: #0f172a;
      --panel: #111b33;
      --panel2: #162340;
      --ink: #e5eefc;
      --muted: #8ea4c8;
      --grid: #294164;
      --accent: #5bd0ff;
      --ema9: #f59e0b;
      --ema20: #a855f7;
      --vwap: #facc15;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Consolas, "Courier New", monospace;
    }}
    .wrap {{
      max-width: 1540px;
      margin: 0 auto;
      padding: 18px;
    }}
    .panel {{
      background: linear-gradient(180deg, var(--panel), var(--panel2));
      border: 1px solid #27466d;
      border-radius: 18px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
      overflow: hidden;
      margin-bottom: 18px;
    }}
    .head {{
      padding: 16px 18px 10px;
      border-bottom: 1px solid #27466d;
    }}
    .title {{
      font-size: 28px;
      font-weight: 700;
      margin: 0 0 6px;
    }}
    .sub {{
      color: var(--muted);
      font-size: 13px;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
      padding: 16px 18px;
    }}
    .stat {{
      border: 1px solid #325685;
      border-radius: 14px;
      padding: 12px;
      background: rgba(255,255,255,0.02);
    }}
    .stat .k {{
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 6px;
    }}
    .stat .v {{
      font-size: 28px;
      font-weight: 700;
    }}
    svg {{
      display: block;
      width: 100%;
      height: auto;
      background: #0d152b;
    }}
    .table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }}
    .table th, .table td {{
      border-top: 1px solid #27466d;
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
    }}
    .table th {{
      color: #cce0ff;
      background: #22385a;
      position: sticky;
      top: 0;
    }}
    .scroll {{
      max-height: 280px;
      overflow: auto;
    }}
    .muted {{
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="panel">
      <div class="head">
        <div class="title">Mai Tai Live Review | {html.escape(strategy_code)} | {html.escape(symbol)}</div>
        <div class="sub">Internal day chart from stored live bars. B = actual entry, S = actual exit, H = blocked but would have won, N = blocked and likely right to skip.</div>
      </div>
      <div class="stats">
        <div class="stat"><div class="k">Bars</div><div class="v">{summary["bars"]}</div></div>
        <div class="stat"><div class="k">Taken Good</div><div class="v">{summary["taken_good"]}</div></div>
        <div class="stat"><div class="k">Taken Bad</div><div class="v">{summary["taken_bad"]}</div></div>
        <div class="stat"><div class="k">Missed Good</div><div class="v">{summary["missed_good"]}</div></div>
        <div class="stat"><div class="k">Missed Bad</div><div class="v">{summary["missed_bad"]}</div></div>
      </div>
      <svg viewBox="0 0 {width} {height}" aria-label="Day review chart">
        <rect x="0" y="0" width="{width}" height="{height}" fill="#0d152b" />
        <line x1="{margin_left}" y1="{price_zero:.2f}" x2="{margin_left + chart_width}" y2="{price_zero:.2f}" stroke="#1e3355" stroke-width="1" opacity="0.35" />
        <line x1="{margin_left}" y1="{macd_zero:.2f}" x2="{margin_left + chart_width}" y2="{macd_zero:.2f}" stroke="#1e3355" stroke-width="1" opacity="0.45" />
        <text x="{margin_left}" y="54" fill="#e5eefc" font-size="18" font-weight="700">Price / EMA / VWAP</text>
        <text x="{margin_left}" y="{macd_top - 18}" fill="#e5eefc" font-size="18" font-weight="700">MACD / Signal / Histogram</text>
        <polyline fill="none" stroke="var(--vwap)" stroke-width="2" points="{_polyline(vwap_points)}" opacity="0.9" />
        <polyline fill="none" stroke="var(--ema9)" stroke-width="2" points="{_polyline(ema9_points)}" opacity="0.9" />
        <polyline fill="none" stroke="var(--ema20)" stroke-width="2" points="{_polyline(ema20_points)}" opacity="0.85" />
        <polyline fill="none" stroke="var(--accent)" stroke-width="2.5" points="{_polyline(close_points)}" />
        {''.join(hist_svg)}
        <polyline fill="none" stroke="#5bd0ff" stroke-width="2" points="{_polyline(macd_points)}" />
        <polyline fill="none" stroke="#f59e0b" stroke-width="2" points="{_polyline(signal_points)}" />
        {''.join(marker_svg)}
      </svg>
    </div>
    <div class="panel">
      <div class="head">
        <div class="title" style="font-size:20px;">Taken Tape</div>
        <div class="sub">Actual filled entries judged as good, bad, or unresolved by the same review rule.</div>
      </div>
      <div class="scroll">
        <table class="table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Reason</th>
              <th>Type</th>
              <th>Price</th>
              <th>Note</th>
            </tr>
          </thead>
          <tbody>
            {''.join(actual_rows)}
          </tbody>
        </table>
      </div>
    </div>
    <div class="panel">
      <div class="head">
        <div class="title" style="font-size:20px;">Blocked Candidates By Reason</div>
        <div class="sub">Only real blocked candidate bars, not generic idle/no-path bars.</div>
      </div>
      <div class="scroll">
        <table class="table">
          <thead>
            <tr>
              <th>Reason</th>
              <th>Total Setups</th>
              <th>Missed Setups</th>
              <th>Good Skips</th>
              <th>Missed Bars</th>
              <th>Good-Skip Bars</th>
            </tr>
          </thead>
          <tbody>
            {''.join(reason_rows)}
          </tbody>
        </table>
      </div>
    </div>
    <div class="panel">
      <div class="head">
        <div class="title" style="font-size:20px;">Setup Tape</div>
        <div class="sub">Grouped missed opportunities so repeated blocked bars in the same move count as one setup.</div>
      </div>
      <div class="scroll">
        <table class="table">
          <thead>
            <tr>
              <th>Start</th>
              <th>End</th>
              <th>Reason</th>
              <th>Type</th>
              <th>Bars</th>
              <th>Start Px</th>
              <th>End Px</th>
            </tr>
          </thead>
          <tbody>
            {''.join(setup_rows)}
          </tbody>
        </table>
      </div>
    </div>
    <div class="panel">
      <div class="head">
        <div class="title" style="font-size:20px;">Review Tape</div>
        <div class="sub">Forward-look classification on blocked/idle bars using the configured lookahead window.</div>
      </div>
      <div class="scroll">
        <table class="table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Reason</th>
              <th>Type</th>
              <th>Price</th>
              <th>Note</th>
            </tr>
          </thead>
          <tbody>
            {''.join(review_rows)}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</body>
</html>"""


def main() -> None:
    args = _parse_args()
    target_day = date.fromisoformat(args.date)
    start_utc, end_utc = _et_window(target_day)
    engine = create_engine(args.db_url)

    with Session(engine) as session:
        bars = _load_bars(
            session,
            strategy_code=args.strategy,
            symbol=args.symbol.upper(),
            start_utc=start_utc,
            end_utc=end_utc,
        )
        intents = _load_intents(
            session,
            strategy_code=args.strategy,
            symbol=args.symbol.upper(),
            start_utc=start_utc,
            end_utc=end_utc,
        )

    if not bars:
        raise SystemExit(f"No bars found for {args.strategy} {args.symbol} on {args.date}")

    review_markers = _future_review(
        bars,
        lookahead_bars=args.lookahead_bars,
        target_up_pct=args.target_up_pct,
        stop_down_pct=args.stop_down_pct,
    )
    actual_outcomes = _classify_actual_outcomes(
        bars,
        intents,
        lookahead_bars=args.lookahead_bars,
        target_up_pct=args.target_up_pct,
        stop_down_pct=args.stop_down_pct,
    )
    html_text = _render_chart(
        strategy_code=args.strategy,
        symbol=args.symbol.upper(),
        bars=bars,
        intents=intents,
        review_markers=review_markers,
        actual_outcomes=actual_outcomes,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html_text, encoding="utf-8")
    print(json.dumps({"output": str(args.output), "bars": len(bars), "intents": len(intents), "review_markers": len(review_markers)}))


if __name__ == "__main__":
    main()
