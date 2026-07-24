"""Backtest REPLAY engine — Phase 1 (ENTRY side).

Replays a historical trading day through the **REAL live entry code**
(`strategy_core.schwab_1m_v2.SchwabV2Strategy`) rather than a re-implementation, then
runs each emitted draft through the **shared emit-gate**
(`strategy_core.entry_gate`) — the exact functions the live bot calls. This is the
durable fix for the chronic "backtest ≠ live" drift (docs/backtest-replay-engine-design.md):
the entry signal, the config, and the emit-gate are SHARED, so they cannot drift by
construction. The only re-implemented surface here is the honest ENTRY fill model and the
tape feed (both small, both bounded by the 07-23 parity reconciliation).

Scope (P1): ENTRY only. The fill stops at the entry; exits are Phase 2.

Data: Schwab 1-min bars (`strategy_bar_history`, the LIVE decision source — NOT Polygon,
per the bar-source-defect rule) + Schwab LEVELONE quotes (`market_quote_ticks` provider
'schwab'), both via `backtest.data`. Feed-coverage honesty: a too-sparse feed is a
SKIP-with-reason, never a silent absence.

The ONE strategy infra-dependency that a standalone replay must neutralize is the
strategy's **wall-clock reads** in the resting-entry path (`_now_ms`, `_resting_in_window`,
`_resting_session_is_eh`). `ReplayStrategy` overrides exactly those to read the injected
HISTORICAL clock — no entry logic is re-implemented; only "now" is substituted for the
replayed instant (the no-look-ahead requirement). Everything else runs in the real class.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import Quote as TapeQuote
from project_mai_tai.backtest.data import SchwabBar
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.market_data.schwab_v2_rest_client import Quote as StratQuote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import entry_gate
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy

EASTERN = ZoneInfo("America/New_York")

# Bars are delivered to the strategy at bar CLOSE (minute start ts + 60s), so a quote at t
# only ever sees bars closed <= t (no look-ahead) and the resting live-bar gate (180s) sees a
# realistic ~60s bar age at placement — exactly the live handoff.
BAR_CLOSE_OFFSET_MS = 60_000

# Coverage honesty: fewer Schwab 1-min bars than this in the loaded window = too sparse to
# replay the ATR flip faithfully (the ATR trail needs ~period+1 bars to define, and the flip
# machinery needs a run of them). Report as a SKIP with the count, never a silent no-entry.
MIN_BARS_FOR_REPLAY = 8


# ------------------------------------------------------------------- outputs
@dataclass(frozen=True)
class ReplayEntry:
    symbol: str
    mode: str          # "resting" | "reactive"
    order_type: str    # "STOP_LIMIT" | "market" | "limit"
    signal_ts: datetime  # when the order was placed (resting) / the break fired (reactive)
    fill_ts: datetime    # when it filled on the tape
    level: float         # the ATR line / trigger the entry keyed off
    fill_price: float


@dataclass(frozen=True)
class ReplaySkip:
    symbol: str
    reason: str
    detail: str = ""


@dataclass
class ReplayResult:
    symbol: str
    session_day_et: str
    n_bars: int
    n_quotes: int
    entries: list[ReplayEntry] = field(default_factory=list)
    skips: list[ReplaySkip] = field(default_factory=list)
    # Resting orders that were placed and worked but never filled on the tape (honest MISS).
    misses: list[ReplaySkip] = field(default_factory=list)


# ------------------------------------------------------------------- config
# The live-LOCKED config values (docs/schwab-1m-v2-live-spec.md §8) that DIFFER from the code
# defaults. Encoded here so an off-VPS / CI replay is faithful without an env file. On the VPS,
# `get_settings()` (env-merged) is the authority — `build_replay_settings` starts from a base
# Settings and overlays these so either source yields the live regime.
LIVE_LOCKED = dict(
    strategy_schwab_1m_v2_enabled=True,
    strategy_schwab_1m_v2_confirmed_window_enabled=True,
    strategy_schwab_1m_v2_cw_v2_enabled=True,
    strategy_schwab_1m_v2_atr_only_mode=True,
    strategy_schwab_1m_v2_atr_flip_enabled=True,
    strategy_schwab_1m_v2_atr_flip_quantity=2,
    strategy_schwab_1m_v2_atr_flip_vol_floor=10000,
    strategy_schwab_1m_v2_atr_flip_period=5,
    strategy_schwab_1m_v2_atr_flip_factor=3.5,
    strategy_schwab_1m_v2_cw_v2_reactive_entry_enabled=True,
    strategy_schwab_1m_v2_cw_v2_reclaim_enabled=False,
    strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
    strategy_schwab_1m_v2_cw_v2_resting_entry_band_pct=0.5,
    strategy_schwab_1m_v2_cw_v2_resting_entry_reprice_pct=0.5,
    strategy_schwab_1m_v2_cw_v2_resting_entry_min_short_bars=3,
    strategy_schwab_1m_v2_cw_v2_resting_entry_max_bar_age_secs=180.0,
    strategy_schwab_1m_v2_cw_v2_resting_entry_flip_grace_secs=30.0,
    strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=False,
    # Boot-hold safety is ON live but the bot RELEASES it after its one-time verify; the replay
    # models steady-state (released) via `_entries_held = False` below, so this is left default-off.
    strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=False,
    strategy_schwab_1m_v2_entry_window_start_hour_et=7,
    strategy_schwab_1m_v2_entry_window_start_minute_et=0,
    strategy_schwab_1m_v2_entry_window_end_hour_et=16,
    strategy_schwab_1m_v2_entry_window_end_minute_et=0,
)


def build_replay_settings(base: Settings | None = None, **overrides) -> Settings:
    """Faithful live-regime Settings for the replay. Starts from `base` (or the env-merged
    `Settings()` on the VPS), overlays the live-LOCKED spec §8 values, then any test overrides."""
    merged = dict(base.model_dump()) if base is not None else {}
    merged.update(LIVE_LOCKED)
    merged.update(overrides)
    return Settings(**merged)


# ------------------------------------------------------------------- clock-injecting strategy
class ReplayStrategy(SchwabV2Strategy):
    """The REAL entry strategy with the wall-clock reads substituted for the injected historical
    clock. Overrides ONLY the three time sources the resting path reads from `datetime.now(UTC)`;
    all entry logic (ATR flip, wait-3 reactive break, resting place/reprice/cancel, gates) runs
    unchanged in the base class."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._replay_now_ms = 0
        # Steady-state: the live bot releases boot-hold after its verify; the replay starts released.
        self._entries_held = False

    def set_clock_ms(self, now_ms: int) -> None:
        self._replay_now_ms = int(now_ms)

    def _now_ms(self) -> int:  # override wall-clock (used by resting live-bar gate + flip grace)
        return self._replay_now_ms

    def _replay_now(self) -> datetime:
        return datetime.fromtimestamp(self._replay_now_ms / 1000.0, UTC)

    def _resting_in_window(self, now: datetime | None = None) -> bool:
        return super()._resting_in_window(now if now is not None else self._replay_now())

    def _resting_session_is_eh(self, now: datetime | None = None) -> bool:
        return super()._resting_session_is_eh(now if now is not None else self._replay_now())


# ------------------------------------------------------------------- adapters
def _to_chartbar(symbol: str, b: SchwabBar) -> ChartBar:
    return ChartBar(symbol=symbol, open=b.open, high=b.high, low=b.low,
                    close=b.close, volume=int(b.volume), timestamp_ms=int(b.ts))


def _to_stratquote(symbol: str, q: TapeQuote) -> StratQuote:
    return StratQuote(
        symbol=symbol,
        bid_price=float(q.bid),
        ask_price=float(q.ask),
        last_price=float(q.last) if q.last is not None else 0.0,
        quote_time_ms=int(q.ts.timestamp() * 1000),
    )


# ------------------------------------------------------------------- the replay
@dataclass
class _RestingOrder:
    stop: float
    limit: float
    place_ts: datetime


def replay_symbol_day(
    source,
    symbol: str,
    session_day_et: str,
    settings: Settings,
    *,
    window_start_hour_et: int = 4,
    window_end_hour_et: int = 20,
) -> ReplayResult:
    """Replay one symbol for one ET session day through the real entry code + shared emit-gate.

    Loads Schwab bars/quotes for [start, end) ET, feeds them to `ReplayStrategy` in strict time
    order (bars at close = ts+60s, quotes at ts; no look-ahead), gates reactive drafts through
    `entry_gate.gate_open_intent`, emits resting place/cancel drafts directly (they bypass the
    chokepoint live too), and applies the honest ENTRY fill model:
      * resting STOP_LIMIT: fills at the first quote whose ask lands in the band [stop, limit]
        (limit = stop*(1+band)), price = that ask; a break that gaps the whole band, or that never
        reaches the stop, => MISS.
      * reactive: marketable => fills at the break price (RTH) / the ask-limit (EH routing).
    Returns the replayed entries (+ skips/misses with reasons).
    """
    day = datetime.strptime(session_day_et, "%Y-%m-%d").replace(tzinfo=EASTERN)
    start = day.replace(hour=window_start_hour_et, minute=0, second=0, microsecond=0)
    end = day.replace(hour=window_end_hour_et, minute=0, second=0, microsecond=0)

    bars = source.schwab_bars(symbol, start, end)
    quotes = source.schwab_quotes(symbol, start, end)
    result = ReplayResult(symbol=symbol, session_day_et=session_day_et,
                          n_bars=len(bars), n_quotes=len(quotes))

    if len(bars) < MIN_BARS_FOR_REPLAY:
        result.skips.append(ReplaySkip(
            symbol, "sparse_schwab_feed",
            f"only {len(bars)} Schwab 1-min bars in {window_start_hour_et:02d}:00-"
            f"{window_end_hour_et:02d}:00 ET (< {MIN_BARS_FOR_REPLAY}); too sparse to replay the ATR flip",
        ))
        return result

    strat = ReplayStrategy(settings)
    qty = strat._atr_qty

    # Merge into a single time-ordered event stream. eff_ts is the instant the event reaches the
    # strategy: bars at close (ts+60s), quotes at their own ts. On a tie, the bar (minute boundary)
    # is delivered before the quote so a quote at t sees the bar closed AT t.
    events: list[tuple[int, int, object]] = []
    for b in bars:
        events.append((int(b.ts) + BAR_CLOSE_OFFSET_MS, 0, b))
    for q in quotes:
        events.append((int(q.ts.timestamp() * 1000), 1, q))
    events.sort(key=lambda e: (e[0], e[1]))

    resting: _RestingOrder | None = None
    filled = False  # P1 = entry only: one entry per symbol (no exit model to re-flatten)
    latest_stratquote: dict[str, StratQuote] = {}

    def _gate_and_maybe_fill(draft, eff_dt: datetime) -> None:
        """Run a strategy-returned (reactive) draft through the SHARED emit-gate; on emit,
        apply the marketable reactive fill and mark the symbol in-position."""
        nonlocal filled
        decision = entry_gate.gate_open_intent(draft, eff_dt, settings, latest_stratquote.get)
        if not decision.emit:
            return
        md = decision.draft.metadata
        level = float(md.get("cw_trigger") or md.get("reference_price") or md.get("entry_price") or 0.0)
        order_type = str(md.get("order_type", "market")).lower()
        # EH routing stamps a limit_price (the ask); otherwise fill at the marketable break price.
        if order_type == "limit" and md.get("limit_price"):
            fill_price = float(md["limit_price"])
        else:
            fill_price = float(md.get("entry_price") or md.get("reference_price") or 0.0)
        result.entries.append(ReplayEntry(
            symbol=symbol, mode="reactive", order_type=order_type,
            signal_ts=eff_dt, fill_ts=eff_dt, level=level, fill_price=fill_price,
        ))
        strat.update_position(symbol, qty)
        filled = True

    for eff_ts, kind, payload in events:
        eff_dt = datetime.fromtimestamp(eff_ts / 1000.0, UTC)
        strat.set_clock_ms(eff_ts)

        if kind == 0:  # bar (delivered at close)
            if filled:
                continue
            bar = _to_chartbar(symbol, payload)  # type: ignore[arg-type]
            draft = strat.on_bar(symbol, bar)
            # Drain the resting place/cancel drafts the manager queued this bar (bypass the gate,
            # exactly like the bot's direct emit).
            for d in strat.drain_pending_intents():
                it = getattr(d, "intent_type", "")
                if it == "cancel":
                    resting = None
                elif it == "open" and str(d.metadata.get("order_type", "")).upper() == "STOP_LIMIT":
                    resting = _RestingOrder(
                        stop=float(d.metadata["stop_price"]),
                        limit=float(d.metadata["limit_price"]),
                        place_ts=eff_dt,
                    )
            # A bar-close reactive draft (rare) also goes through the gate.
            if draft is not None:
                _gate_and_maybe_fill(draft, eff_dt)
            continue

        # quote
        q: TapeQuote = payload  # type: ignore[assignment]
        sq = _to_stratquote(symbol, q)
        latest_stratquote[symbol.upper()] = sq
        if not filled:
            draft = strat.on_quote(symbol, sq)
            if draft is not None:
                _gate_and_maybe_fill(draft, eff_dt)
        # Resting buy-STOP-LIMIT fill: the stop (S) triggers at ask >= S, then it is a LIMIT buy at
        # L = S*(1+band); it fills only if the ask is at/under the limit — i.e. the ask lands in the
        # band [S, L]. Fill price = the ask (== min(ask, L) inside the band). A break that GAPS the
        # whole band (every crossing ask > L) does NOT fill — the honest resting-entry miss (the band
        # is exactly the fill/miss threshold). Fill @ ask ∈ [S, L].
        if resting is not None and not filled and resting.stop <= float(q.ask) <= resting.limit:
            result.entries.append(ReplayEntry(
                symbol=symbol, mode="resting", order_type="STOP_LIMIT",
                signal_ts=resting.place_ts, fill_ts=eff_dt,
                level=resting.stop, fill_price=float(q.ask),
            ))
            strat.update_position(symbol, qty)
            filled = True
            resting = None

    # Any resting order still working at EOD that never crossed = honest MISS.
    if resting is not None and not filled:
        result.misses.append(ReplaySkip(
            symbol, "resting_never_filled",
            f"resting buy-stop-limit [{resting.stop:.4f}, {resting.limit:.4f}] placed "
            f"{resting.place_ts.astimezone(EASTERN):%H:%M:%S} ET never saw an ask in the band "
            f"on the tape (never reached the stop, or gapped through the limit)",
        ))
    return result


# ------------------------------------------------------------------- reconciliation (Deliverable 3)
@dataclass(frozen=True)
class RealEntry:
    symbol: str
    entry_price: float
    entry_time_et: datetime
    entry_path: str


def fetch_real_v2_entries(session_factory, session_day_et: str) -> list[RealEntry]:
    """The REAL v2 entries for a day from `oms_managed_positions` (the ground truth to reconcile)."""
    from sqlalchemy import text

    with session_factory() as s:
        rows = s.execute(
            text(
                "SELECT symbol, entry_price, entry_time, entry_path FROM oms_managed_positions "
                "WHERE strategy_code='schwab_1m_v2' "
                "AND (entry_time AT TIME ZONE 'America/New_York')::date = :d "
                "ORDER BY entry_time"
            ),
            {"d": session_day_et},
        ).all()
    return [
        RealEntry(sym, float(px), et.astimezone(EASTERN), str(path or ""))
        for sym, px, et, path in rows
    ]


def reconcile_day(source, session_day_et: str, settings: Settings, real: list[RealEntry]) -> str:
    """Replay every real-entry symbol for the day and reconcile the replayed entries vs the real
    fills. Returns a human-readable report (trade-for-trade). Honest about feed coverage."""
    lines: list[str] = []
    lines.append(f"=== BACKTEST REPLAY — ENTRY PARITY — {session_day_et} ===")
    lines.append(f"real v2 entries: {len(real)}")
    for r in real:
        res = replay_symbol_day(source, r.symbol, session_day_et, settings)
        lines.append("")
        lines.append(
            f"[{r.symbol}] REAL: {r.entry_path} @ {r.entry_price:.4f} "
            f"{r.entry_time_et:%H:%M:%S} ET | Schwab feed: {res.n_bars} bars, {res.n_quotes} quotes"
        )
        for sk in res.skips:
            lines.append(f"    SKIP  {sk.reason}: {sk.detail}")
        for m in res.misses:
            lines.append(f"    MISS  {m.reason}: {m.detail}")
        if not res.entries:
            lines.append("    REPLAY: (no entry)")
        for e in res.entries:
            dp = (e.fill_price - r.entry_price) / r.entry_price * 100.0
            lines.append(
                f"    REPLAY {e.mode}/{e.order_type} @ {e.fill_price:.4f} "
                f"fill {e.fill_ts.astimezone(EASTERN):%H:%M:%S} ET (level {e.level:.4f}) "
                f"| Δ vs real fill {dp:+.2f}%"
            )
    return "\n".join(lines)


def main() -> None:  # pragma: no cover - CLI wrapper (exercised via the VPS reconciliation)
    import argparse

    from project_mai_tai.backtest.data import DbMarketDataSource
    from project_mai_tai.db.session import build_session_factory
    from project_mai_tai.settings import get_settings

    ap = argparse.ArgumentParser(description="Backtest REPLAY — P1 entry parity reconciliation")
    ap.add_argument("date", help="session day, ET, YYYY-MM-DD")
    ap.add_argument("symbols", nargs="*", help="optional symbol filter (default: all real v2 entries)")
    args = ap.parse_args()

    sf = build_session_factory(get_settings())
    settings = build_replay_settings(base=get_settings())
    source = DbMarketDataSource(sf)
    real = fetch_real_v2_entries(sf, args.date)
    if args.symbols:
        keep = {s.upper() for s in args.symbols}
        real = [r for r in real if r.symbol.upper() in keep]
    print(reconcile_day(source, args.date, settings, real))


if __name__ == "__main__":  # pragma: no cover
    main()
