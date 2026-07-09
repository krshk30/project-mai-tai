"""v2/ATR strategy adapter — plugs into the validated engine core (data/harness).

THREE feeds (the KIDZ anchor picked this split): ATR signal + entry hold-window + entry FILL on
the SCHWAB LEVELONE feed (what v2 saw; reproduces the real 1.1887 Schwab fill); EXIT ladder on
the MASSIVE bid (the gateway the OMS reads). Schwab latency ~0s.

Entry (mirror schwab_1m_v2.py): variant-B ATR touch (analysis.atr_flip.compute_atr_trail) + the
intrabar hold-confirm fallback_thin path. Exit (mirror oms _evaluate_v2_managed_exit): the
ExitEngine ladder — hard -1.5% / floor tiers / scales — MARKET fills at the observed massive bid.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from project_mai_tai.backtest.atr_oracle import ATR_FACTOR, ATR_PERIOD, compute_atr_trail
from project_mai_tai.exit_logic.config import TradingConfig
from project_mai_tai.exit_logic.engine import ExitEngine
from project_mai_tai.exit_logic.position import Position

SCHWAB_LATENCY_S = 0.0     # measured near-instant (median 0s) — NOT Webull's band
VOL_FLOOR = 10000          # env override on the live schwab-1m-v2 service
HOLD_N_SECONDS, HOLD_MIN_TICKS, HOLD_BPS = 20, 5, 5.0
BAR_MS = 60_000
POSITION_POLL_INTERVAL_SECS = 5.0   # mirrors schwab_1m_v2_bot.POSITION_POLL_INTERVAL_SECONDS (the re-arm
#                                     release only fires on a position poll, so quantize to this grid)


@dataclass(frozen=True)
class V2Trade:
    entry_ts: datetime
    entry_price: float       # Schwab ask fill
    touch_price: float       # ATR trail level (idealized reference)
    exit_ts: datetime | None
    exit_price: float | None  # massive bid, qty-weighted over legs
    qty: int
    pnl: float
    exit_reason: str
    n_legs: int


def _v2_cfg():
    return TradingConfig().make_v2_variant()


def _utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def _px(q):  # hold-confirm px = last, fallback mid (mirror on_quote:487-493)
    return q.last if (q.last is not None and q.last > 0) else (q.bid + q.ask) / 2.0


class _Book:
    def __init__(self, quotes):
        self._q = quotes
        self._ts = [q.ts for q in quotes]

    def at(self, t):
        i = bisect_right(self._ts, t) - 1
        return self._q[i] if i >= 0 else None

    def slice(self, t0, t1):
        return self._q[bisect_left(self._ts, t0):bisect_left(self._ts, t1)]

    def index_at_or_after(self, t):
        return bisect_left(self._ts, t)


def detect_atr_touches(bars):
    """variant-B touches (mirror _update_atr_state:709-751): (bar_idx, touch_ms, touch_price).
    One per short segment; fired resets on a SELL flip (new short opens)."""
    rows = compute_atr_trail(bars, period=ATR_PERIOD, factor=ATR_FACTOR)
    fired = False
    out = []
    for i in range(1, len(bars)):
        prev = rows[i - 1]
        if prev["state"] == "short" and prev["trail"] is not None and bars[i].high >= prev["trail"] and not fired:
            out.append((i, bars[i].ts, prev["trail"]))
            fired = True
        if rows[i]["flip"] == "SELL":
            fired = False
    return out


def detect_atr_touches_independent(bars, period=ATR_PERIOD, factor=ATR_FACTOR):
    """INDEPENDENT 2nd touch-detector for the PARITY check — a single forward pass mirroring the
    live `_update_atr_state` structure (TR/Wilders/trail/flip/touch inline), a DIFFERENT code path
    from detect_atr_touches (which derives from the multi-pass oracle). Same spec (seed=sma5); if
    the two agree, that's strong evidence of no silent entry-signal bug (there's no real intrabar
    fill anchor beyond the single KIDZ trade). Returns [(bar_idx, ts, touch_price)]."""
    hl = [b.high - b.low for b in bars]
    tr_seed: list[float] = []
    wilders = None
    state = trail = prev_state = prev_trail = None
    fired = False
    out = []
    for i, b in enumerate(bars):
        tr = None
        if i >= period - 1:
            s = sum(hl[i - period + 1:i + 1]) / period
            prev = bars[i - 1]
            hilo = min(hl[i], 1.5 * s)
            href = (b.high - prev.close) if b.low <= prev.high else (b.high - prev.close) - 0.5 * (b.low - prev.high)
            lref = (prev.close - b.low) if b.high >= prev.low else (prev.close - b.low) - 0.5 * (prev.low - b.high)
            tr = max(hilo, href, lref)
        loss = None
        if tr is not None:
            if wilders is None:
                tr_seed.append(tr)
                if len(tr_seed) == period:
                    wilders = sum(tr_seed) / period
            else:
                wilders = wilders + (tr - wilders) / period
            if wilders is not None:
                loss = factor * wilders
        if loss is None:
            continue
        if prev_state == "short" and prev_trail is not None and b.high >= prev_trail and not fired:
            out.append((i, b.ts, prev_trail))
            fired = True
        if state is None:
            state, trail = "long", b.close - loss
        elif state == "long":
            if b.close > trail:
                trail = max(trail, b.close - loss)
            else:
                state, trail, fired = "short", b.close + loss, False
        else:
            if b.close < trail:
                trail = min(trail, b.close + loss)
            else:
                state, trail = "long", b.close - loss
        prev_state, prev_trail = state, trail
    return out


def _run_exit(massive, start_idx, entry_price, qty, cfg, engine):
    """ExitEngine ladder on the massive bid; legs fill at the observed bid (MARKET, ~0s).
    Returns (exit_ts, wavg_price, pnl, reason, n_legs)."""
    pos = Position("V2", entry_price, qty, scale_profile="NORMAL")
    legs = []  # (qty, price)
    reason = "OPEN"
    exit_ts = None
    i, n = start_idx, len(massive)
    while i < n and pos.quantity > 0:
        q = massive[i]
        if q.bid > 0:
            pos.update_price(q.bid)
            action = engine.check_hard_stop(pos, q.bid) or engine.check_intrabar_exit(pos)
            if action and action["action"] == "CLOSE":
                legs.append((pos.quantity, q.bid))
                reason = action["reason"]
                exit_ts = q.ts
                pos.quantity = 0
                break
            if action and action["action"] == "SCALE":
                sell = min(int(action["sell_qty"]), pos.quantity)
                if sell > 0:
                    legs.append((sell, q.bid))
                    pos.apply_scale(action["level"], sell, q.bid)
                    reason = f"{action['reason']}+"
                    exit_ts = q.ts
        i += 1
    if pos.quantity > 0:  # window ended holding the remainder
        if massive:
            legs.append((pos.quantity, massive[-1].bid))
            reason = "WINDOW_END" if not legs[:-1] else f"{reason}WINDOW_END"
            exit_ts = massive[-1].ts
        else:
            return None, None, 0.0, "NO_QUOTES", 0
    tot = sum(lq for lq, _ in legs)
    pnl = sum((p - entry_price) * lq for lq, p in legs)
    wavg = sum(p * lq for lq, p in legs) / tot if tot else None
    return exit_ts, wavg, pnl, reason, len(legs)


def _hold_verdict(sbook, bar, touch_price, mode):
    """The live hold-confirm verdict for a graze (mirror _resolve_hold + simulate_v2:192-207).
    Returns (verdict, decision_ts): 'confirm'|'fallback_thin'|'skip'|'bar_close'."""
    if mode == "bar_close":
        return "bar_close", _utc(bar.ts + BAR_MS)          # hold-confirm OFF: enter at bar close
    win = sbook.slice(_utc(bar.ts), _utc(bar.ts + BAR_MS))
    touch_q = next((q for q in win if _px(q) >= touch_price), None)
    if touch_q is None:
        return "fallback_thin", _utc(bar.ts + BAR_MS)      # no intrabar cross -> bar-close settle
    t1 = touch_q.ts + timedelta(seconds=HOLD_N_SECONDS)
    window = sbook.slice(touch_q.ts, t1)
    n_ticks = len(window)
    last_px = _px(window[-1]) if window else _px(touch_q)
    net_bps = (last_px - touch_price) / touch_price * 1e4
    if n_ticks < HOLD_MIN_TICKS:
        return "fallback_thin", t1
    if net_bps >= HOLD_BPS:
        return "confirm", t1
    return "skip", t1                                       # rejected: neither thin nor confirmed


def simulate_v2(schwab_bars, schwab_quotes, massive_quotes, *, qty=10, vol_floor=VOL_FLOOR,
                mode="intrabar", latency_s=SCHWAB_LATENCY_S, rearm=False, rearm_timeout_secs=12.0,
                rearm_poll_interval_secs=POSITION_POLL_INTERVAL_SECS, reject_bar_idxs=None):
    """mode='intrabar' = live hold-confirm path (the leak path); mode='bar_close' = variant-B
    touch at bar close (hold-confirm OFF, comparison). Entry fill = Schwab ask; exit = massive-bid
    ExitEngine ladder. One position at a time (flat gate).

    rearm=False (default) = the SHIPPED behavior (one touch per short segment; a skip/no-fill consumes
    the segment -> the real flip is missed). rearm=True = the fix (guard claimed only on a FILL; skip /
    no-fill release the segment; a BUY flip that finds the segment unclaimed enters at the flip close).
    `rearm_timeout_secs` = the emit->fill release window (config-pinned, WALL-CLOCK not bars: a broker
    round-trip is measured in seconds, and a bar-based window can straddle the next real flip and rebuild
    the bug). Default 12.0s: above the 5s position poll, well under the 60s bar, erring short (a too-long
    window re-misses the flip; a too-short one is caught by the caller's flat/cooldown one-per-symbol
    guard). `rearm_poll_interval_secs` = the live position-poll cadence: the bot only releases a working
    order on a poll, so its real release is the first poll >= emit+timeout, i.e. in [timeout, timeout+poll].
    The backtest quantizes to the UPPER bound (emit+timeout+poll) so it is never MORE optimistic than the
    bot (residual is <=poll pessimism — the safe direction for a go/no-go instrument). `reject_bar_idxs`
    (test hook) = bar indices whose emit is forced to NO-FILL (models restricted-name downstream rejects).
    See docs/schwab-1m-v2-atr-flip-rearm-fix-design.md."""
    if rearm:
        return _simulate_v2_rearm(schwab_bars, schwab_quotes, massive_quotes, qty=qty, vol_floor=vol_floor,
                                  mode=mode, latency_s=latency_s, timeout_secs=rearm_timeout_secs,
                                  poll_interval_secs=rearm_poll_interval_secs,
                                  reject_bar_idxs=set(reject_bar_idxs or ()))
    reject = set(reject_bar_idxs or ())
    cfg = _v2_cfg()
    engine = ExitEngine(cfg)
    sbook = _Book(schwab_quotes)
    mbook = _Book(massive_quotes)
    trades = []
    flat_after = None
    for bar_idx, touch_ms, touch_price in detect_atr_touches(schwab_bars):
        bar = schwab_bars[bar_idx]
        if bar.volume <= vol_floor:            # liquidity gate (mirror _build_hold_draft:577)
            continue
        if bar_idx in reject:                  # test hook: emit reaches broker but is rejected (no fill)
            continue
        if mode == "bar_close":
            decision_ts = _utc(bar.ts + BAR_MS)
        else:  # intrabar hold-confirm
            win = sbook.slice(_utc(bar.ts), _utc(bar.ts + BAR_MS))
            touch_q = next((q for q in win if _px(q) >= touch_price), None)
            if touch_q is None:                # sparse feed, no intrabar cross -> bar-close settle
                decision_ts = _utc(bar.ts + BAR_MS)
            else:
                t1 = touch_q.ts + timedelta(seconds=HOLD_N_SECONDS)
                window = sbook.slice(touch_q.ts, t1)
                n_ticks = len(window)
                last_px = _px(window[-1]) if window else _px(touch_q)
                net_bps = (last_px - touch_price) / touch_price * 1e4
                if not (n_ticks < HOLD_MIN_TICKS or net_bps >= HOLD_BPS):
                    continue                   # skip (not thin, not confirmed)
                decision_ts = t1               # fallback_thin OR confirm -> ENTER at window end
        if flat_after is not None and decision_ts < flat_after:
            continue                           # still holding a prior position
        fq = sbook.at(decision_ts + timedelta(seconds=latency_s))
        if fq is None or fq.ask <= 0:
            continue
        entry_ts = decision_ts + timedelta(seconds=latency_s)
        entry_price = fq.ask                   # honest Schwab ask (market buy)
        start = mbook.index_at_or_after(entry_ts)
        exit_ts, wavg, pnl, reason, n_legs = _run_exit(massive_quotes, start, entry_price, qty, cfg, engine)
        trades.append(V2Trade(entry_ts, entry_price, touch_price, exit_ts, wavg, qty, pnl, reason, n_legs))
        flat_after = exit_ts
    return trades


def _simulate_v2_rearm(schwab_bars, schwab_quotes, massive_quotes, *, qty, vol_floor, mode,
                       latency_s, timeout_secs, poll_interval_secs, reject_bar_idxs):
    """THE FIX (backtest side). Single pass with the pending-order lifecycle, so the guard is claimed
    ONLY when a position opens (a fill). Guard states: UNCLAIMED (free) -> PROVISIONAL (emit sent, order
    working) -> CLAIMED (fill) | released back to UNCLAIMED on skip / no-fill / SELL flip. A BUY flip
    that finds the segment UNCLAIMED enters at the flip close (variant-A backstop).

    The emit->fill release is TIME-BASED (wall-clock, not bars — a bar-count window can straddle the next
    real flip). A PROVISIONAL emit re-arms once `now >= emit_ts + timeout_secs + poll_interval_secs`: the
    bot releases on the first position poll >= emit+timeout (real release in [timeout, timeout+poll]), so
    we quantize to the UPPER bound and are never MORE optimistic than the bot. The release is an EXPLICIT
    step (`_release_if_expired`, called once per decision point); the entry gate is a PURE `guard ==
    'UNCLAIMED'` read — no mutate-on-query. Mirrors schwab_1m_v2 so the backtest measures the fixed bot."""
    cfg = _v2_cfg()
    engine = ExitEngine(cfg)
    sbook = _Book(schwab_quotes)
    mbook = _Book(massive_quotes)
    rows = compute_atr_trail(schwab_bars, period=ATR_PERIOD, factor=ATR_FACTOR)
    trades = []
    guard = "UNCLAIMED"
    prov_release_ts = None        # wall-clock at which a PROVISIONAL (emit-no-fill) re-arms
    flat_after = None

    def _release_if_expired(now_ts):
        """Explicit re-arm (a NAMED side-effect, not a query): a PROVISIONAL emit that has not filled by
        its poll-quantized release time collapses to UNCLAIMED. Call once at each decision point, BEFORE
        reading `guard` — mirrors the bot's position poll releasing a working order after the timeout."""
        nonlocal guard, prov_release_ts
        if guard == "PROVISIONAL" and prov_release_ts is not None and now_ts >= prov_release_ts:
            guard, prov_release_ts = "UNCLAIMED", None

    def _enter(decision_ts, touch_price, bar_i):
        """Attempt an entry at decision_ts. Returns ('fill', V2Trade) | ('nofill', None) | ('holding', None)."""
        nonlocal flat_after
        if flat_after is not None and decision_ts < flat_after:
            return "holding", None
        if bar_i in reject_bar_idxs:                       # test hook: forced downstream reject
            return "nofill", None
        fq = sbook.at(decision_ts + timedelta(seconds=latency_s))
        if fq is None or fq.ask <= 0:                      # missing quote = no fill (restricted-name proxy)
            return "nofill", None
        entry_ts = decision_ts + timedelta(seconds=latency_s)
        entry_price = fq.ask
        start = mbook.index_at_or_after(entry_ts)
        exit_ts, wavg, pnl, reason, n_legs = _run_exit(massive_quotes, start, entry_price, qty, cfg, engine)
        flat_after = exit_ts
        return "fill", V2Trade(entry_ts, entry_price, touch_price, exit_ts, wavg, qty, pnl, reason, n_legs)

    def _resolve_emit(decision_ts, touch_price, bar_i):
        nonlocal guard, prov_release_ts
        outcome, tr = _enter(decision_ts, touch_price, bar_i)
        if outcome == "fill":
            trades.append(tr)
            guard = "CLAIMED"
        elif outcome == "holding":
            guard = "CLAIMED"                              # a prior position still open — don't re-enter
        else:                                             # nofill -> PROVISIONAL; release quantized to the
            guard = "PROVISIONAL"                         # first poll >= emit+timeout (upper bound)
            prov_release_ts = decision_ts + timedelta(seconds=timeout_secs + poll_interval_secs)

    for i in range(1, len(schwab_bars)):
        prev, cur, bar = rows[i - 1], rows[i], schwab_bars[i]
        if cur["flip"] == "SELL":
            guard, prov_release_ts = "UNCLAIMED", None      # new short segment
        # (1) graze while short -> hold-confirm verdict; emit only if the segment is free at decision_ts
        if (prev["state"] == "short" and prev["trail"] is not None and bar.high >= prev["trail"]
                and bar.volume > vol_floor):
            verdict, decision_ts = _hold_verdict(sbook, bar, prev["trail"], mode)
            if verdict != "skip":
                _release_if_expired(decision_ts)            # explicit re-arm at this decision point
                if guard == "UNCLAIMED":                    # pure read
                    _resolve_emit(decision_ts, prev["trail"], i)
            # skip -> no claim; a later graze/flip re-arms
        # (2) BUY flip backstop -> flip-close entry when the segment is free at the flip
        if cur["flip"] == "BUY" and bar.volume > vol_floor:
            decision_ts = _utc(bar.ts + BAR_MS)
            _release_if_expired(decision_ts)                # explicit re-arm at this decision point
            if guard == "UNCLAIMED":                        # pure read
                _resolve_emit(decision_ts, cur["trail"], i)
    return trades
