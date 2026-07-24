"""Backtest REPLAY engine — Phase 1 (ENTRY side) + Phase 2 (EXIT side).

Replays a historical trading day through the **REAL live entry code**
(`strategy_core.schwab_1m_v2.SchwabV2Strategy`) rather than a re-implementation, then
runs each emitted draft through the **shared emit-gate**
(`strategy_core.entry_gate`) — the exact functions the live bot calls. This is the
durable fix for the chronic "backtest ≠ live" drift (docs/backtest-replay-engine-design.md):
the entry signal, the config, and the emit-gate are SHARED, so they cannot drift by
construction. The only re-implemented surface here is the honest ENTRY fill model and the
tape feed (both small, both bounded by the 07-23 parity reconciliation).

Scope (P1): ENTRY — the honest fill against the tape (resting band / marketable reactive).

Scope (P2): EXIT — continue past the entry fill into the full trade, unified on the LIVE
exit code. The geometry is chosen by the position's OPEN session (docs/schwab-1m-v2-live-spec.md §6):
  * **RTH open → STATIC native OCO** (§6a): target = ref×(1+cw_target%) [+2%], stop =
    ref×(1−cw_hard_stop%) [−5%], anchored off the CW break/**reference** price (`_apply_v2_oco_bracket_entry`
    uses `metadata["entry_price"] or reference_price`, NOT the fill). Modeled as **first-touch on
    the trade tape**: whichever leg the tape reaches first exits; if neither by the 16:00 bell, the
    DAY OCO expires and we **close at the 16:00 price** (what really happened to SKYQ 07-23).
  * **EH open → software CW floor-RIDE** (§6b): the SHARED `cw_exit_decision` is driven tick-by-tick
    over the Schwab LEVELONE bids (the exact fn the OMS `_evaluate_v2_managed_exit` calls), reading
    `oms_v2_cw_*` from Settings. On +target% it ARMS a floor and rides; exits on fallback-to-floor /
    −stop% (pre-arm) / bar-close ATR flip. **The v2 replay exit NEVER touches `ExitEngine`** — that
    divergence (the 07-23 `ExitEngine` vs `cw_exit_decision` drift) is killed here by construction.

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
from project_mai_tai.backtest.data import Trade as TapeTrade
from project_mai_tai.exit_logic.cw_exit import cw_exit_decision
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

# The regular session (ET). The RTH-vs-EH open decides the exit geometry and the native OCO is
# a regular-session construct — this mirrors `oms.service._is_regular_market_session` /
# `_extended_hours_session` (09:30 <= t < 16:00 ET). Kept local so the CI replay stays hermetic
# (no oms.service import); the parity gate pins it against the real fills.
RTH_OPEN_ET = (9, 30)
RTH_CLOSE_ET = (16, 0)


def _is_rth(dt_utc: datetime) -> bool:
    et = dt_utc.astimezone(EASTERN)
    open_et = et.replace(hour=RTH_OPEN_ET[0], minute=RTH_OPEN_ET[1], second=0, microsecond=0)
    close_et = et.replace(hour=RTH_CLOSE_ET[0], minute=RTH_CLOSE_ET[1], second=0, microsecond=0)
    return open_et <= et < close_et


def _schwab_round_price(price: float) -> float:
    """Numeric mirror of `oms.service._schwab_round`: >$1 -> 2dp, <=$1 -> 4dp. The native OCO legs
    are rounded to this tick rule live (firm-rejects otherwise), so the first-touch model rounds the
    target/stop to the SAME levels the broker would actually rest."""
    return round(price, 2) if price > 1.0 else round(price, 4)


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
    # The CW break/reference price the OCO anchors off (metadata entry_price/reference_price) — the
    # RTH static-OCO target/stop are struck off THIS, not the realized fill (per spec §6a).
    entry_ref: float = 0.0


# The full entry->exit trade — the P2 deliverable. exit_reason is the canonical enum.
@dataclass(frozen=True)
class ReplayTrade:
    symbol: str
    mode: str               # "resting" | "reactive"
    geometry: str           # "rth_static_oco" | "eh_floor_ride"
    entry_ts: datetime
    entry_px: float         # the realized entry FILL (the cost basis for ret_pct)
    entry_ref: float        # the CW break/reference anchor for the OCO legs
    exit_ts: datetime
    exit_px: float
    ret_pct: float
    exit_reason: str        # target | stop | floor | flip | close-at-bell


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
    # Full entry->exit trades (P2). One per filled entry once the exit resolves.
    trades: list[ReplayTrade] = field(default_factory=list)


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


# ------------------------------------------------------------------- exit models
@dataclass
class _RestingOrder:
    stop: float
    limit: float
    place_ts: datetime
    entry_ref: float


def _static_oco_first_touch(
    entry_ref: float,
    tape: list[tuple[datetime, float]],
    *,
    target_pct: float,
    stop_pct: float,
    close_dt: datetime,
) -> tuple[datetime, float, str]:
    """RTH-open geometry: the broker-native OCO is STATIC (spec §6a). Struck off the CW break/
    reference price (NOT the fill), the child OCO is `SELL LIMIT @ target` + `SELL STOP @ protect`,
    both rounded to the Schwab tick rule — exactly `_apply_v2_oco_bracket_entry`.

    First-touch on the trade tape (prints in [entry, 16:00), time-ordered): the SELL LIMIT fills on
    the first print that reaches the target (>= target); the SELL STOP triggers on the first print
    that reaches the protect (<= stop). Whichever the tape reaches first is the exit (a print is a
    single price, so target/stop are mutually exclusive per print — no same-print ambiguity). If
    NEITHER leg is touched by the 16:00 bell, the DAY OCO expires → **close at the 16:00 price** (the
    last print <= the close). Returns (exit_ts, exit_px, reason)."""
    target = _schwab_round_price(entry_ref * (1.0 + target_pct / 100.0))
    stop = _schwab_round_price(entry_ref * (1.0 - stop_pct / 100.0))
    last_ts: datetime | None = None
    last_px: float | None = None
    for ts, px in tape:
        if px >= target:
            return ts, target, "target"     # SELL LIMIT fills at the target
        if px <= stop:
            return ts, stop, "stop"         # SELL STOP triggers, modeled fill at the stop level
        last_ts, last_px = ts, px
    # Neither leg by the close: the DAY OCO lapses; close at the 16:00 price (last print seen).
    if last_px is None:
        return close_dt, target, "close-at-bell"  # no prints post-entry (degenerate) -> ref-level
    return last_ts or close_dt, last_px, "close-at-bell"


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

    # Post-entry trade tape for the RTH static-OCO first-touch — the native OCO fills/triggers
    # against the actual prints (spec §6a). Loaded once; sliced to [entry, 16:00) when a fill lands.
    trades: list[TapeTrade] = source.trades(symbol, start, end) if hasattr(source, "trades") else []
    rth_close_dt = day.replace(hour=RTH_CLOSE_ET[0], minute=RTH_CLOSE_ET[1], second=0, microsecond=0)

    # Live exit params (spec §6) from Settings — the SAME values the OMS passes to cw_exit_decision,
    # so the EH floor-ride is the live decision verbatim and RTH OCO legs are struck at live levels.
    cw_target_pct = float(getattr(settings, "oms_v2_cw_target_pct", 2.0))
    cw_stop_pct = float(getattr(settings, "oms_v2_cw_hard_stop_pct", 5.0))
    cw_floor_pct = float(getattr(settings, "oms_v2_cw_floor_pct", 2.0))
    cw_floor_enabled = bool(getattr(settings, "oms_v2_cw_floor_exit_enabled", False))

    resting: _RestingOrder | None = None
    filled = False           # one entry per symbol
    entry_rec: ReplayEntry | None = None
    geometry = ""            # "rth_static_oco" | "eh_floor_ride"
    exit_done = False
    eh_armed = False         # EH floor-ride: cw_exit_decision floor-armed state
    eh_flip_pending = False  # EH floor-ride: a bar-close ATR SELL-flip fired while holding
    eh_last_bid: tuple[datetime, float] | None = None
    latest_stratquote: dict[str, StratQuote] = {}

    def _open_static_oco(e: ReplayEntry) -> None:
        """RTH open -> the broker owns a STATIC OCO; resolve it by first-touch on the trade tape."""
        nonlocal exit_done
        tape = [(t.ts, float(t.price)) for t in trades if e.fill_ts <= t.ts < rth_close_dt]
        exit_ts, exit_px, reason = _static_oco_first_touch(
            e.entry_ref, tape, target_pct=cw_target_pct, stop_pct=cw_stop_pct, close_dt=rth_close_dt,
        )
        ret = (exit_px - e.fill_price) / e.fill_price * 100.0 if e.fill_price else 0.0
        result.trades.append(ReplayTrade(
            symbol=symbol, mode=e.mode, geometry="rth_static_oco",
            entry_ts=e.fill_ts, entry_px=e.fill_price, entry_ref=e.entry_ref,
            exit_ts=exit_ts, exit_px=exit_px, ret_pct=ret, exit_reason=reason,
        ))
        exit_done = True

    def _record_fill(e: ReplayEntry) -> None:
        """Record the entry, mark the symbol in-position, and select the exit geometry by the OPEN
        session. RTH resolves the static OCO immediately (broker-arbitrated); EH continues the loop
        so the SHARED cw_exit_decision rides the tape bids."""
        nonlocal filled, entry_rec, geometry
        result.entries.append(e)
        strat.update_position(symbol, qty)
        filled = True
        entry_rec = e
        if _is_rth(e.fill_ts):
            geometry = "rth_static_oco"
            _open_static_oco(e)
        else:
            geometry = "eh_floor_ride"

    def _gate_and_maybe_fill(draft, eff_dt: datetime) -> None:
        """Run a strategy-returned (reactive) draft through the SHARED emit-gate; on emit, apply the
        marketable reactive fill and record the entry (which selects/opens the exit geometry)."""
        decision = entry_gate.gate_open_intent(draft, eff_dt, settings, latest_stratquote.get)
        if not decision.emit:
            return
        md = decision.draft.metadata
        level = float(md.get("cw_trigger") or md.get("reference_price") or md.get("entry_price") or 0.0)
        order_type = str(md.get("order_type", "market")).lower()
        # The OCO anchor is the CW break/reference price (metadata entry_price/reference_price) — the
        # exact field `_apply_v2_oco_bracket_entry` reads; NOT the realized fill.
        entry_ref = float(md.get("entry_price") or md.get("reference_price") or 0.0)
        # EH routing stamps a limit_price (the ask); otherwise fill at the marketable break price.
        if order_type == "limit" and md.get("limit_price"):
            fill_price = float(md["limit_price"])
        else:
            fill_price = float(md.get("entry_price") or md.get("reference_price") or 0.0)
        _record_fill(ReplayEntry(
            symbol=symbol, mode="reactive", order_type=order_type,
            signal_ts=eff_dt, fill_ts=eff_dt, level=level, fill_price=fill_price, entry_ref=entry_ref,
        ))

    def _finish_eh_exit(exit_ts: datetime, exit_px: float, reason: str) -> None:
        nonlocal exit_done
        e = entry_rec
        assert e is not None
        ret = (exit_px - e.fill_price) / e.fill_price * 100.0 if e.fill_price else 0.0
        result.trades.append(ReplayTrade(
            symbol=symbol, mode=e.mode, geometry="eh_floor_ride",
            entry_ts=e.fill_ts, entry_px=e.fill_price, entry_ref=e.entry_ref,
            exit_ts=exit_ts, exit_px=exit_px, ret_pct=ret, exit_reason=reason,
        ))
        exit_done = True

    for eff_ts, kind, payload in events:
        if exit_done:
            break
        eff_dt = datetime.fromtimestamp(eff_ts / 1000.0, UTC)
        strat.set_clock_ms(eff_ts)

        if kind == 0:  # bar (delivered at close)
            bar = _to_chartbar(symbol, payload)  # type: ignore[arg-type]
            draft = strat.on_bar(symbol, bar)
            if not filled:
                # Drain the resting place/cancel drafts the manager queued this bar (bypass the
                # gate, exactly like the bot's direct emit).
                for d in strat.drain_pending_intents():
                    it = getattr(d, "intent_type", "")
                    if it == "cancel":
                        resting = None
                    elif it == "open" and str(d.metadata.get("order_type", "")).upper() == "STOP_LIMIT":
                        resting = _RestingOrder(
                            stop=float(d.metadata["stop_price"]),
                            limit=float(d.metadata["limit_price"]),
                            place_ts=eff_dt,
                            entry_ref=float(
                                d.metadata.get("entry_price")
                                or d.metadata.get("reference_price")
                                or d.metadata["stop_price"]
                            ),
                        )
                # A bar-close reactive draft (rare) also goes through the gate.
                if draft is not None:
                    _gate_and_maybe_fill(draft, eff_dt)
            elif geometry == "eh_floor_ride":
                # EH floor-ride: a bar-close ATR SELL-flip while holding is the trend exit. The REAL
                # strategy returns a cw_flip CLOSE draft (`_maybe_cw_flip_close`, spec §6b). Mirror the
                # bot->OMS handoff: mark flip_pending so the next bid tick closes via cw_exit_decision
                # (precedence target/arm > stop > flip, exactly like the live block). Resting churn
                # while holding is drained + discarded.
                strat.drain_pending_intents()
                if (draft is not None and getattr(draft, "intent_type", "") == "close"
                        and str(getattr(draft, "metadata", {}).get("cw_flip", "")).lower() == "true"):
                    eh_flip_pending = True
            continue

        # quote
        q: TapeQuote = payload  # type: ignore[assignment]
        sq = _to_stratquote(symbol, q)
        latest_stratquote[symbol.upper()] = sq

        if not filled:
            draft = strat.on_quote(symbol, sq)
            if draft is not None:
                _gate_and_maybe_fill(draft, eff_dt)
            # Resting buy-STOP-LIMIT fill: the stop (S) triggers at ask >= S, then it is a LIMIT buy
            # at L = S*(1+band); it fills only if the ask lands in the band [S, L]. Fill @ ask ∈ [S,L];
            # a break that GAPS the whole band does NOT fill — the honest resting-entry miss.
            if not filled and resting is not None and resting.stop <= float(q.ask) <= resting.limit:
                _record_fill(ReplayEntry(
                    symbol=symbol, mode="resting", order_type="STOP_LIMIT",
                    signal_ts=resting.place_ts, fill_ts=eff_dt,
                    level=resting.stop, fill_price=float(q.ask), entry_ref=resting.entry_ref,
                ))
                resting = None
            continue

        if geometry == "eh_floor_ride":
            # EH open -> the SHARED live exit fn drives the floor-ride tick-by-tick over the bids
            # (the exact call `oms.service._evaluate_v2_managed_exit` makes). No ExitEngine anywhere.
            bid = float(q.bid)
            if bid <= 0:
                continue
            eh_last_bid = (eff_dt, bid)
            entry_px = entry_rec.fill_price  # EH ladder anchors off the FILL (managed-row entry_price)
            action, eh_armed = cw_exit_decision(
                entry_px, bid, eh_armed,
                target_pct=cw_target_pct, stop_pct=cw_stop_pct,
                floor_pct=cw_floor_pct, floor_enabled=cw_floor_enabled,
                flip_pending=eh_flip_pending,
            )
            if action in ("arm", "hold"):
                continue
            # exit — the reference price mirrors the live `_evaluate_v2_managed_exit` leg mapping.
            if action == "target":
                _finish_eh_exit(eff_dt, entry_px * (1.0 + cw_target_pct / 100.0), "target")
            elif action == "floor":
                _finish_eh_exit(eff_dt, entry_px * (1.0 + cw_floor_pct / 100.0), "floor")
            elif action == "stop":
                _finish_eh_exit(eff_dt, entry_px * (1.0 - cw_stop_pct / 100.0), "stop")
            else:  # flip -> close at the current bid
                _finish_eh_exit(eff_dt, bid, "flip")

    # Any resting order still working at EOD that never crossed = honest MISS.
    if resting is not None and not filled:
        result.misses.append(ReplaySkip(
            symbol, "resting_never_filled",
            f"resting buy-stop-limit [{resting.stop:.4f}, {resting.limit:.4f}] placed "
            f"{resting.place_ts.astimezone(EASTERN):%H:%M:%S} ET never saw an ask in the band "
            f"on the tape (never reached the stop, or gapped through the limit)",
        ))

    # An EH-opened position that never hit floor / -stop / flip on the loaded tape: close-at-bell at
    # the last bid seen (the 19:55 flatten backstop is out of scope; this bounds the trade honestly).
    if entry_rec is not None and geometry == "eh_floor_ride" and not exit_done and eh_last_bid is not None:
        ts_, bid_ = eh_last_bid
        _finish_eh_exit(ts_, bid_, "close-at-bell")

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


@dataclass(frozen=True)
class RealExit:
    symbol: str
    exit_price: float
    exit_time_et: datetime


def fetch_real_v2_exit(session_factory, symbol: str, session_day_et: str) -> RealExit | None:
    """Best-effort REAL exit fill for a v2 symbol on a day, from the broker SELL fills
    (`broker_orders` + `broker_order_events`). The last FILL's price/time is the realized exit.
    Returns None if no priced sell fill is found (the ground truth then lives only in the broker
    UI / logs — surfaced as unavailable, never faked)."""
    from sqlalchemy import text

    _price_keys = ("avg_price", "average_price", "filled_avg_price", "fill_price", "price")
    with session_factory() as s:
        rows = s.execute(
            text(
                "SELECT e.event_at, e.payload FROM broker_order_events e "
                "JOIN broker_orders bo ON bo.id = e.order_id "
                "JOIN strategies st ON st.id = bo.strategy_id "
                "WHERE st.code='schwab_1m_v2' AND bo.symbol=:sym AND lower(bo.side)='sell' "
                "AND (e.event_at AT TIME ZONE 'America/New_York')::date = :d "
                "ORDER BY e.event_at"
            ),
            {"sym": symbol, "d": session_day_et},
        ).all()
    last: RealExit | None = None
    for event_at, payload in rows:
        if not isinstance(payload, dict):
            continue
        px = next((payload[k] for k in _price_keys if payload.get(k) not in (None, "", 0, "0")), None)
        try:
            px_f = float(px)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if px_f > 0:
            last = RealExit(symbol, px_f, event_at.astimezone(EASTERN))
    return last


def reconcile_day(
    source, session_day_et: str, settings: Settings, real: list[RealEntry], *, session_factory=None
) -> str:
    """Replay every real-entry symbol for the day and reconcile the replayed **full trade**
    (entry -> exit -> ret/reason) vs the real fills. Returns a human-readable report. Honest about
    feed coverage and about any real-exit the broker tables can't price."""
    lines: list[str] = []
    lines.append(f"=== BACKTEST REPLAY — FULL-TRADE PARITY — {session_day_et} ===")
    lines.append(f"real v2 entries: {len(real)}")
    for r in real:
        res = replay_symbol_day(source, r.symbol, session_day_et, settings)
        real_exit = (
            fetch_real_v2_exit(session_factory, r.symbol, session_day_et)
            if session_factory is not None else None
        )
        lines.append("")
        lines.append(
            f"[{r.symbol}] REAL: {r.entry_path} @ {r.entry_price:.4f} "
            f"{r.entry_time_et:%H:%M:%S} ET | Schwab feed: {res.n_bars} bars, {res.n_quotes} quotes"
        )
        if real_exit is not None:
            rret = (real_exit.exit_price - r.entry_price) / r.entry_price * 100.0
            lines.append(
                f"           REAL exit @ {real_exit.exit_price:.4f} "
                f"{real_exit.exit_time_et:%H:%M:%S} ET | real ret {rret:+.2f}%"
            )
        else:
            lines.append("           REAL exit: (unavailable from broker tables — compare vs logs)")
        for sk in res.skips:
            lines.append(f"    SKIP  {sk.reason}: {sk.detail}")
        for m in res.misses:
            lines.append(f"    MISS  {m.reason}: {m.detail}")
        if not res.entries:
            lines.append("    REPLAY: (no entry)")
        for e in res.entries:
            dp = (e.fill_price - r.entry_price) / r.entry_price * 100.0
            lines.append(
                f"    REPLAY entry {e.mode}/{e.order_type} @ {e.fill_price:.4f} "
                f"fill {e.fill_ts.astimezone(EASTERN):%H:%M:%S} ET (ref {e.entry_ref:.4f}) "
                f"| Δ vs real entry {dp:+.2f}%"
            )
        for t in res.trades:
            gap = ""
            if real_exit is not None:
                gap = f" | Δ exit_px vs real {(t.exit_px - real_exit.exit_price):+.4f}"
            lines.append(
                f"    REPLAY exit  [{t.geometry}] @ {t.exit_px:.4f} "
                f"{t.exit_ts.astimezone(EASTERN):%H:%M:%S} ET reason={t.exit_reason} "
                f"| replay ret {t.ret_pct:+.2f}%{gap}"
            )
    return "\n".join(lines)


def main() -> None:  # pragma: no cover - CLI wrapper (exercised via the VPS reconciliation)
    import argparse

    from project_mai_tai.backtest.data import DbMarketDataSource
    from project_mai_tai.db.session import build_session_factory
    from project_mai_tai.settings import get_settings

    ap = argparse.ArgumentParser(description="Backtest REPLAY — P2 full-trade parity reconciliation")
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
    print(reconcile_day(source, args.date, settings, real, session_factory=sf))


if __name__ == "__main__":  # pragma: no cover
    main()
