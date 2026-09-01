"""Scanner-window ATR bracket study using the live v2 entry state machine.

This module changes no trading behavior.  It replays historical Schwab bars and quotes through
``ReplayStrategy`` (the clock-injected subclass of the live ``SchwabV2Strategy``), then substitutes
one research-only exit bracket.  A modelled close is fed back through ``update_position`` so the
live reclaim transition runs and the tape can continue past the first round trip.

The study is deliberately signal-level: one filled strategy entry is one trade.  Webull fan-out is
disabled by the caller because duplicating a signal across venues is execution, not a second ATR
opportunity, and the historical counterfactual cannot know a broker's future fill outcome.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import Quote as TapeQuote
from project_mai_tai.backtest.replay import (
    BAR_CLOSE_OFFSET_MS,
    MIN_BARS_FOR_REPLAY,
    ReplayStrategy,
    _eh_entry_reprice,
    _to_chartbar,
    _to_stratquote,
)
from project_mai_tai.backtest.watch_start import WatchWindow
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import entry_gate
from project_mai_tai.strategy_core.schwab_1m_v2 import session_start_ts_ms

EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class BracketPolicy:
    """Static bracket percentages anchored to the executable entry fill."""

    target_pct: float
    floor_pct: float

    def __post_init__(self) -> None:
        if self.target_pct <= 0:
            raise ValueError("target_pct must be positive")
        if self.floor_pct > 0:
            raise ValueError("floor_pct must be zero or negative")

    @property
    def name(self) -> str:
        return f"target+{self.target_pct:g}_floor{self.floor_pct:+g}"


@dataclass(frozen=True)
class StudySkip:
    symbol: str
    reason: str
    detail: str


@dataclass(frozen=True)
class StudyMiss:
    symbol: str
    reason: str
    at: datetime | None
    detail: str


@dataclass(frozen=True)
class StudyTrade:
    session_day_et: str
    symbol: str
    policy: str
    entry_slot: Literal["first", "reclaim"]
    entry_mode: Literal["resting", "reactive"]
    order_type: str
    scanner_window_start: datetime
    scanner_window_end: datetime | None
    arm_bar_ts_ms: int
    signal_ts: datetime
    entry_ts: datetime
    entry_px: float
    target_px: float
    floor_px: float
    exit_trigger_ts: datetime
    exit_ts: datetime
    exit_px: float
    exit_reason: Literal["target", "floor", "close", "unmodellable"]
    ret_pct: float
    duration_seconds: float
    mfe_pct: float
    mae_pct: float
    first_plus_1_ts: datetime | None
    first_plus_2_ts: datetime | None
    first_minus_1_ts: datetime | None
    first_minus_2_ts: datetime | None
    quotes_observed: int


@dataclass
class BracketStudyResult:
    symbol: str
    session_day_et: str
    policy: BracketPolicy
    n_bars: int
    n_quotes: int
    n_scanner_windows: int
    trades: list[StudyTrade] = field(default_factory=list)
    skips: list[StudySkip] = field(default_factory=list)
    misses: list[StudyMiss] = field(default_factory=list)
    n_watch_start_capped: int = 0
    n_entry_drafts_outside_scanner: int = 0


@dataclass
class _WorkingRest:
    stop: float
    limit: float
    place_ts: datetime
    entry_ref: float
    slot: Literal["first", "reclaim"]
    arm_bar_ts_ms: int


@dataclass
class _OpenTrade:
    slot: Literal["first", "reclaim"]
    mode: Literal["resting", "reactive"]
    order_type: str
    signal_ts: datetime
    entry_ts: datetime
    entry_px: float
    arm_bar_ts_ms: int
    scanner_window: WatchWindow
    target_px: float
    floor_px: float
    max_bid: float
    min_bid: float
    quotes_observed: int = 0
    plus_1_ts: datetime | None = None
    plus_2_ts: datetime | None = None
    minus_1_ts: datetime | None = None
    minus_2_ts: datetime | None = None
    floor_trigger_ts: datetime | None = None

    def observe(self, ts: datetime, bid: float) -> None:
        self.quotes_observed += 1
        self.max_bid = max(self.max_bid, bid)
        self.min_bid = min(self.min_bid, bid)
        pct = (bid - self.entry_px) / self.entry_px * 100.0
        if pct >= 1.0 and self.plus_1_ts is None:
            self.plus_1_ts = ts
        if pct >= 2.0 and self.plus_2_ts is None:
            self.plus_2_ts = ts
        if pct <= -1.0 and self.minus_1_ts is None:
            self.minus_1_ts = ts
        if pct <= -2.0 and self.minus_2_ts is None:
            self.minus_2_ts = ts


def _window_at(windows: list[WatchWindow], ts_ms: int) -> WatchWindow | None:
    return next((window for window in windows if window.contains(ts_ms)), None)


def _entry_slot(value: object, *, mode: str) -> Literal["first", "reclaim"]:
    raw = str(value or "").lower()
    if raw in ("first", "reclaim"):
        return raw  # type: ignore[return-value]
    return "reclaim" if mode == "reactive" else "first"


def _consume_stale_arm(
    strategy: ReplayStrategy,
    symbol: str,
    *,
    watch_start_ms: int,
    session_anchor_ms: int,
) -> bool:
    """Mirror the live service's post-#859 composition cap.

    The strategy class owns the entry state, but the live cap is service-owned.  The replay's older
    helper consumed only the legacy counter; the live paths read the two slot claims.  A historical
    study must consume all three or it can trade a segment production would refuse.
    """
    state = strategy.watchlist_state(symbol)
    max_entries = strategy._cw_v2_max_entries_per_flip
    boundary = max(int(watch_start_ms), int(session_anchor_ms))
    dangerous = bool(
        state.cw_armed
        and 0 < state.cw_arm_bar_ts <= boundary
        and (
            state.cw_entries_this_flip < max_entries
            or not state.cw_resting_taken
            or not state.cw_reclaim_taken
        )
    )
    if not dangerous:
        return False
    state.cw_entries_this_flip = max_entries
    state.cw_resting_taken = True
    state.cw_reclaim_taken = True
    if state.resting_active:
        strategy._queue_resting_cancel(state, reason="scanner-watch-start-cap")
    return True


def run_symbol_policy(
    source,
    symbol: str,
    session_day_et: str,
    settings: Settings,
    policy: BracketPolicy,
    *,
    observation_start_hour_et: int = 4,
    entry_start_hour_et: int = 7,
    session_end_hour_et: int = 16,
) -> BracketStudyResult:
    """Replay one symbol/session for one target/floor policy.

    Entries are admitted only while a real-time scanner window is open and only during
    ``[entry_start, session_end)``.  Scanner removal prevents future entries but never liquidates an
    already-open position.  A floor touch triggers a market-style exit filled at the *next* observed
    bid; the target is a resting limit filled at its stated price on first executable touch.
    """
    symbol = symbol.upper()
    session_day = datetime.strptime(session_day_et, "%Y-%m-%d").replace(tzinfo=EASTERN)
    observation_start = session_day.replace(
        hour=observation_start_hour_et, minute=0, second=0, microsecond=0
    )
    entry_start = session_day.replace(
        hour=entry_start_hour_et, minute=0, second=0, microsecond=0
    )
    session_end = session_day.replace(
        hour=session_end_hour_et, minute=0, second=0, microsecond=0
    )

    bars = source.schwab_bars(symbol, observation_start, session_end)
    quotes = source.schwab_quotes(symbol, observation_start, session_end)
    try:
        windows = source.watch_windows(
            symbol,
            session_day.date(),
            realtime_confirms_only=True,
        )
    except TypeError:
        windows = source.watch_windows(symbol, session_day.date())

    result = BracketStudyResult(
        symbol=symbol,
        session_day_et=session_day_et,
        policy=policy,
        n_bars=len(bars),
        n_quotes=len(quotes),
        n_scanner_windows=len(windows),
    )
    if not windows:
        result.skips.append(StudySkip(symbol, "no_scanner_window", "no real-time CONFIRM window"))
        return result
    if len(bars) < MIN_BARS_FOR_REPLAY:
        result.skips.append(
            StudySkip(
                symbol,
                "sparse_schwab_feed",
                f"only {len(bars)} Schwab one-minute bars (<{MIN_BARS_FOR_REPLAY})",
            )
        )
        return result

    strategy = ReplayStrategy(settings, watch_windows=windows)
    strategy._boot_ms = int(observation_start.timestamp() * 1000)
    quantity = strategy._atr_qty
    session_anchor_ms = session_start_ts_ms(int(entry_start.timestamp() * 1000))

    # Boundary events make cancellation happen at the scanner timestamp, not at whichever market
    # event happens to arrive next.  Priority: leave, join, bar-close, quote.
    events: list[tuple[int, int, str, object]] = []
    for window in windows:
        events.append((window.start_ms, 1, "join", window))
        if window.end_ms is not None:
            events.append((window.end_ms, 0, "leave", window))
    for bar in bars:
        events.append((int(bar.ts) + BAR_CLOSE_OFFSET_MS, 2, "bar", bar))
    for quote in quotes:
        events.append((int(quote.ts.timestamp() * 1000), 3, "quote", quote))
    events.sort(key=lambda event: (event[0], event[1]))

    working_rest: _WorkingRest | None = None
    open_trade: _OpenTrade | None = None
    latest_quote = {}
    last_bid: tuple[datetime, float] | None = None

    def drain_primary_intents(*, eligible: bool) -> None:
        nonlocal working_rest
        for draft in strategy.drain_pending_intents():
            intent_type = str(getattr(draft, "intent_type", ""))
            if intent_type == "cancel":
                working_rest = None
                continue
            metadata = getattr(draft, "metadata", {})
            if (
                intent_type != "open"
                or str(metadata.get("order_type", "")).upper() != "STOP_LIMIT"
            ):
                continue
            if not eligible or open_trade is not None:
                result.n_entry_drafts_outside_scanner += int(not eligible)
                continue
            mode = "resting"
            working_rest = _WorkingRest(
                stop=float(metadata["stop_price"]),
                limit=float(metadata["limit_price"]),
                place_ts=datetime.fromtimestamp(strategy._now_ms() / 1000.0, UTC),
                entry_ref=float(
                    metadata.get("entry_price")
                    or metadata.get("reference_price")
                    or metadata["stop_price"]
                ),
                slot=_entry_slot(metadata.get("cw_entry_slot"), mode=mode),
                arm_bar_ts_ms=int(metadata.get("cw_arm_bar_ts") or 0),
            )
        # Fan-out is explicitly disabled for this signal-level study, but drain defensively so a
        # misconfigured caller cannot let an execution-side queue grow without bound.
        strategy.drain_webull_direct_intents()
        strategy.drain_webull_fanout_intents()

    def begin_trade(
        *,
        slot: Literal["first", "reclaim"],
        mode: Literal["resting", "reactive"],
        order_type: str,
        signal_ts: datetime,
        fill_ts: datetime,
        fill_px: float,
        arm_bar_ts_ms: int,
        scanner_window: WatchWindow,
    ) -> None:
        nonlocal open_trade, working_rest
        open_trade = _OpenTrade(
            slot=slot,
            mode=mode,
            order_type=order_type,
            signal_ts=signal_ts,
            entry_ts=fill_ts,
            entry_px=fill_px,
            arm_bar_ts_ms=arm_bar_ts_ms,
            scanner_window=scanner_window,
            target_px=fill_px * (1.0 + policy.target_pct / 100.0),
            floor_px=fill_px * (1.0 + policy.floor_pct / 100.0),
            max_bid=fill_px,
            min_bid=fill_px,
        )
        working_rest = None
        strategy.update_position(symbol, quantity, held_qty=quantity)
        strategy.drain_webull_direct_intents()
        strategy.drain_webull_fanout_intents()

    def gate_and_fill(draft, now: datetime, quote: TapeQuote, window: WatchWindow) -> None:
        metadata = getattr(draft, "metadata", {})
        decision = entry_gate.gate_open_intent(draft, now, settings, latest_quote.get)
        if not decision.emit:
            return
        metadata = decision.draft.metadata
        is_resting = (
            str(metadata.get("eh_resting", "")).lower() == "true"
            or str(metadata.get("resting_entry", "")).lower() == "true"
        )
        mode: Literal["resting", "reactive"] = "resting" if is_resting else "reactive"
        slot = _entry_slot(metadata.get("cw_entry_slot"), mode=mode)
        order_type = str(metadata.get("order_type", "market")).lower()
        fill_px = float(quote.ask)
        if str(metadata.get("session", "")).upper() in ("AM", "PM"):
            eh_fill = _eh_entry_reprice(metadata, fill_px, settings, is_resting=is_resting)
            if eh_fill.reason_code:
                result.misses.append(
                    StudyMiss(
                        symbol,
                        "eh_entry_abandoned",
                        now,
                        f"{mode}/{slot} {eh_fill.reason_code} ask={fill_px:.4f}",
                    )
                )
                return
            fill_px = eh_fill.fill_price
            order_type = "limit"
        if fill_px <= 0:
            result.misses.append(StudyMiss(symbol, "no_executable_ask", now, mode))
            return
        begin_trade(
            slot=slot,
            mode=mode,
            order_type=order_type,
            signal_ts=now,
            fill_ts=quote.ts,
            fill_px=fill_px,
            arm_bar_ts_ms=int(metadata.get("cw_arm_bar_ts") or 0),
            scanner_window=window,
        )

    def finish_trade(
        *,
        trigger_ts: datetime,
        exit_ts: datetime,
        exit_px: float,
        reason: Literal["target", "floor", "close", "unmodellable"],
    ) -> None:
        nonlocal open_trade
        trade = open_trade
        assert trade is not None
        ret_pct = (exit_px - trade.entry_px) / trade.entry_px * 100.0
        result.trades.append(
            StudyTrade(
                session_day_et=session_day_et,
                symbol=symbol,
                policy=policy.name,
                entry_slot=trade.slot,
                entry_mode=trade.mode,
                order_type=trade.order_type,
                scanner_window_start=datetime.fromtimestamp(
                    trade.scanner_window.start_ms / 1000.0, UTC
                ),
                scanner_window_end=(
                    datetime.fromtimestamp(trade.scanner_window.end_ms / 1000.0, UTC)
                    if trade.scanner_window.end_ms is not None
                    else None
                ),
                arm_bar_ts_ms=trade.arm_bar_ts_ms,
                signal_ts=trade.signal_ts,
                entry_ts=trade.entry_ts,
                entry_px=trade.entry_px,
                target_px=trade.target_px,
                floor_px=trade.floor_px,
                exit_trigger_ts=trigger_ts,
                exit_ts=exit_ts,
                exit_px=exit_px,
                exit_reason=reason,
                ret_pct=ret_pct,
                duration_seconds=(exit_ts - trade.entry_ts).total_seconds(),
                mfe_pct=(trade.max_bid - trade.entry_px) / trade.entry_px * 100.0,
                mae_pct=(trade.min_bid - trade.entry_px) / trade.entry_px * 100.0,
                first_plus_1_ts=trade.plus_1_ts,
                first_plus_2_ts=trade.plus_2_ts,
                first_minus_1_ts=trade.minus_1_ts,
                first_minus_2_ts=trade.minus_2_ts,
                quotes_observed=trade.quotes_observed,
            )
        )
        strategy.update_position(symbol, 0, held_qty=0)
        strategy.drain_webull_direct_intents()
        strategy.drain_webull_fanout_intents()
        open_trade = None

    for event_ms, _, event_type, payload in events:
        event_dt = datetime.fromtimestamp(event_ms / 1000.0, UTC)
        if event_dt < observation_start.astimezone(UTC) or event_dt >= session_end.astimezone(UTC):
            continue
        strategy.set_clock_ms(event_ms)
        window = _window_at(windows, event_ms)
        entry_time_ok = entry_start.astimezone(UTC) <= event_dt < session_end.astimezone(UTC)
        eligible = window is not None and entry_time_ok

        if event_type == "leave":
            working_rest = None
            strategy._release_post_close_entry_state(
                strategy.watchlist_state(symbol), reason="scanner-window-ended"
            )
            drain_primary_intents(eligible=False)
            continue
        if event_type == "join":
            joined: WatchWindow = payload  # type: ignore[assignment]
            if _consume_stale_arm(
                strategy,
                symbol,
                watch_start_ms=joined.start_ms,
                session_anchor_ms=session_anchor_ms,
            ):
                result.n_watch_start_capped += 1
            drain_primary_intents(eligible=entry_time_ok)
            continue

        if event_type == "bar":
            draft = strategy.on_bar(symbol, _to_chartbar(symbol, payload))
            if eligible and window is not None:
                if _consume_stale_arm(
                    strategy,
                    symbol,
                    watch_start_ms=window.start_ms,
                    session_anchor_ms=session_anchor_ms,
                ):
                    result.n_watch_start_capped += 1
            drain_primary_intents(eligible=eligible)
            # CW-v2 opens are quote-driven.  Keep a returned close draft out of the counterfactual
            # bracket: it still mutated the live ATR segment state, but exits only by this policy.
            if draft is not None and getattr(draft, "intent_type", "") == "open" and not eligible:
                result.n_entry_drafts_outside_scanner += 1
            continue

        quote: TapeQuote = payload  # type: ignore[assignment]
        if quote.bid > 0:
            last_bid = (quote.ts, float(quote.bid))
        strategy_quote = _to_stratquote(symbol, quote)
        latest_quote[symbol] = strategy_quote

        if open_trade is not None:
            # The fill quote existed before the bracket children could become active.  Start exit
            # observation on the first strictly later snapshot; this still makes a 0% floor expose
            # the spread immediately without time-travelling within one quote.
            if quote.ts <= open_trade.entry_ts or quote.bid <= 0:
                continue
            bid = float(quote.bid)
            open_trade.observe(quote.ts, bid)
            if open_trade.floor_trigger_ts is not None:
                finish_trade(
                    trigger_ts=open_trade.floor_trigger_ts,
                    exit_ts=quote.ts,
                    exit_px=bid,
                    reason="floor",
                )
                continue
            if bid >= open_trade.target_px:
                finish_trade(
                    trigger_ts=quote.ts,
                    exit_ts=quote.ts,
                    exit_px=open_trade.target_px,
                    reason="target",
                )
                continue
            if bid <= open_trade.floor_px:
                open_trade.floor_trigger_ts = quote.ts
            continue

        if not eligible or window is None:
            continue

        draft = strategy.on_quote(symbol, strategy_quote)
        if draft is not None:
            gate_and_fill(draft, event_dt, quote, window)
        if (
            open_trade is None
            and working_rest is not None
            and working_rest.stop <= float(quote.ask) <= working_rest.limit
        ):
            begin_trade(
                slot=working_rest.slot,
                mode="resting",
                order_type="STOP_LIMIT",
                signal_ts=working_rest.place_ts,
                fill_ts=quote.ts,
                fill_px=float(quote.ask),
                arm_bar_ts_ms=working_rest.arm_bar_ts_ms,
                scanner_window=window,
            )

    if open_trade is not None:
        if last_bid is None or last_bid[0] <= open_trade.entry_ts:
            result.misses.append(
                StudyMiss(
                    symbol,
                    "exit_unmodellable",
                    open_trade.entry_ts,
                    "no executable bid after entry through 16:00 ET",
                )
            )
        else:
            trigger_ts = open_trade.floor_trigger_ts or last_bid[0]
            finish_trade(
                trigger_ts=trigger_ts,
                exit_ts=last_bid[0],
                exit_px=last_bid[1],
                reason="floor" if open_trade.floor_trigger_ts else "close",
            )
    if working_rest is not None:
        result.misses.append(
            StudyMiss(
                symbol,
                "resting_never_filled",
                working_rest.place_ts,
                f"{working_rest.slot} band [{working_rest.stop:.4f},{working_rest.limit:.4f}]",
            )
        )
    return result


def trading_sessions(start: date, end: date) -> list[date]:
    """Weekday sessions for a small bounded study; exchange holidays are caller-owned."""
    out: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            out.append(current)
        current = date.fromordinal(current.toordinal() + 1)
    return out


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"cannot encode {type(value).__name__}")


def _summary_rows(results: list[BracketStudyResult]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[StudyTrade]] = defaultdict(list)
    for result in results:
        for trade in result.trades:
            groups[(trade.policy, "all")].append(trade)
            groups[(trade.policy, trade.entry_slot)].append(trade)

    rows: list[dict[str, object]] = []
    policy_names = sorted({result.policy.name for result in results})
    for policy_name in policy_names:
        for population in ("all", "first", "reclaim"):
            trades = groups.get((policy_name, population), [])
            returns = [trade.ret_pct for trade in trades]
            gross_wins = sum(value for value in returns if value > 0)
            gross_losses = abs(sum(value for value in returns if value < 0))
            rows.append(
                {
                    "policy": policy_name,
                    "population": population,
                    "trades": len(trades),
                    "targets": sum(trade.exit_reason == "target" for trade in trades),
                    "floors": sum(trade.exit_reason == "floor" for trade in trades),
                    "closes": sum(trade.exit_reason == "close" for trade in trades),
                    "wins": sum(value > 0 for value in returns),
                    "losses": sum(value < 0 for value in returns),
                    "scratches": sum(abs(value) < 1e-12 for value in returns),
                    "win_rate_pct": round(
                        sum(value > 0 for value in returns) / len(returns) * 100.0, 3
                    )
                    if returns
                    else None,
                    "total_return_pct": round(sum(returns), 4),
                    "mean_return_pct": round(statistics.fmean(returns), 4) if returns else None,
                    "median_return_pct": round(statistics.median(returns), 4) if returns else None,
                    "profit_factor": round(gross_wins / gross_losses, 4)
                    if gross_losses
                    else None,
                    "reached_plus_1": sum(trade.first_plus_1_ts is not None for trade in trades),
                    "reached_plus_2": sum(trade.first_plus_2_ts is not None for trade in trades),
                    "mean_mfe_pct": round(
                        statistics.fmean(trade.mfe_pct for trade in trades), 4
                    )
                    if trades
                    else None,
                    "mean_mae_pct": round(
                        statistics.fmean(trade.mae_pct for trade in trades), 4
                    )
                    if trades
                    else None,
                    "median_duration_seconds": round(
                        statistics.median(trade.duration_seconds for trade in trades), 1
                    )
                    if trades
                    else None,
                }
            )
    return rows


def _write_outputs(
    output_dir: Path,
    results: list[BracketStudyResult],
    *,
    start: date,
    end: date,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"atr-bracket-study-{start.isoformat()}-to-{end.isoformat()}"
    detail_path = output_dir / f"{stem}.json"
    trades_path = output_dir / f"{stem}-trades.csv"
    summary_path = output_dir / f"{stem}-summary.csv"

    detail_path.write_text(
        json.dumps([asdict(result) for result in results], indent=2, default=_json_default) + "\n"
    )
    trades = [trade for result in results for trade in result.trades]
    trade_rows = [asdict(trade) for trade in trades]
    if trade_rows:
        with trades_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(trade_rows[0]))
            writer.writeheader()
            for row in trade_rows:
                writer.writerow(
                    {
                        key: value.isoformat() if isinstance(value, datetime) else value
                        for key, value in row.items()
                    }
                )
    summary_rows = _summary_rows(results)
    with summary_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"detail={detail_path}")
    print(f"trades={trades_path}")
    print(f"summary={summary_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--output-dir", default="analysis/reports")
    args = parser.parse_args()

    from project_mai_tai.backtest.data import DbMarketDataSource
    from project_mai_tai.backtest.replay import build_replay_settings
    from project_mai_tai.db.session import build_session_factory
    from project_mai_tai.settings import get_settings

    base_settings = get_settings()
    settings = build_replay_settings(
        base=base_settings,
        # One ATR opportunity, not one row per venue.  These execution-side switches otherwise
        # require historical broker-outcome acknowledgements the counterfactual does not possess.
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=False,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=False,
    )
    source = DbMarketDataSource(build_session_factory(base_settings))
    policies = [
        BracketPolicy(target_pct=target, floor_pct=floor)
        for target in (1.0, 2.0)
        for floor in (-2.0, -1.0, 0.0)
    ]

    results: list[BracketStudyResult] = []
    sessions = trading_sessions(args.start, args.end)
    for session_date in sessions:
        symbols = source.scanner_confirmed_symbols(
            session_date, realtime_confirms_only=True
        )
        print(
            f"session={session_date.isoformat()} scanner_symbols={len(symbols)}",
            flush=True,
        )
        for symbol in symbols:
            for policy in policies:
                result = run_symbol_policy(
                    source,
                    symbol,
                    session_date.isoformat(),
                    settings,
                    policy,
                )
                results.append(result)
            print(f"  {symbol} policies={len(policies)}", flush=True)

    _write_outputs(
        Path(args.output_dir),
        results,
        start=args.start,
        end=args.end,
    )
    total_trades = sum(len(result.trades) for result in results)
    total_skips = sum(len(result.skips) for result in results)
    total_misses = sum(len(result.misses) for result in results)
    print(
        f"runs={len(results)} trades={total_trades} skips={total_skips} misses={total_misses}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
