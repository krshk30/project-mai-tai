# Fix (b): DB-seed `state.bars` on cold-start (kills the ~135-min restart blackout)

**Status:** design-first, brought for operator confirm BEFORE implementation (a new safety finding —
the pending-cross stash — landed beyond the original approval). Live entry path. Option 1 (DB-seed),
per operator direction; option 2 (fight the Schwab same-day limit) explicitly NOT pursued.

## Problem
`state.bars` is effectively **live-only after a restart**: the C3 dedup gate `_should_skip_rest_strategy_feed`
(`schwab_1m_v2_bot.py:1078`) suppresses the REST warmup batch whenever the streamer's current-minute bar
out-timestamps the historical bars (the streamer subscribes immediately). So every symbol sits at ~minutes
-since-restart, and `_evaluate_completed_bar` bails at the line-676 `min_bars=135` guard → **MACD/VWAP/ATR
all blind for ~135 minutes (>2h) after any restart.** Empirically confirmed 2026-06-15: the 16:14Z restart
produced ZERO entries until ~18:30Z (=16:14+135min), then a VWAP and the first ATR-Flip fired immediately.

## Approach (Option 1): hydrate `state.bars` from `strategy_bar_history`
v2 already persists every bar to `strategy_bar_history` (QTEX had 173 today). On cold-start, load the recent
persisted bars for a symbol and replay them through the strategy so the indicators clear their warmup at once,
bypassing the dedup race and the Schwab same-day-intraday question entirely.

## Scope answers (the operator's questions)

**1. How many bars to load?** Last **N = 250** 60s bars (`interval_secs=60`) for the symbol, ascending.
Clears `min_bars=135` with headroom for the MACD settling (≈135) and leaves room under the `deque(maxlen=300)`
for ~50 live bars before the oldest evict (eviction of the oldest is harmless — MACD/VWAP read the recent tail).
Bounded by what's persisted (a fresh symbol with <135 persisted bars simply warms as the day goes — same as
today, no regression).

**2. Cross-session ordering / VWAP-ATR correctness.** Loading cross-session bars is SAFE: VWAP and ATR both
self-reset at the 04:00-ET session anchor (`vwap_session_anchor_ms`, `atr_session_anchor_ms`), so by the time
today's bars replay, both reflect only today's session — matching the session-sliced backtest. MACD uses
continuous closes (cross-session warmup is *more* correct, mirroring TOS). So we DON'T need to restrict to
today's session.

**3. Dedup against the first live bars.** The seed runs ONCE per symbol at cold-start, before live bars. The
strategy's `_ingest` already drops out-of-order bars and updates same-timestamp bars, so when the first live
(newer-timestamp) bar arrives it appends cleanly. The bot's C3 dedup is REST-vs-streamer only and untouched.

**4. Don't re-trigger entries on replayed history — THE WRINKLE.** `bar_is_fresh` (line 869) blocks *direct*
emits on replayed bars ✔. BUT a NATIVE MACD/VWAP cross on a non-fresh bar is **stashed as a pending cross**
(line 832–843) that the next FRESH bar can consume if `pending_gap_secs ≤ pending_cross_max_gap_secs`
(line 884). The **last** seed bar can be only ~1–2 min before the first live bar → a replayed cross could
fire a spurious entry. **Mitigation (required): after seeding, explicitly clear**
`state.pending_path_macd = False`, `state.pending_path_vwap = False`, `state.pending_cross_bar_ts_ms = 0`.
Keep `prev_macd/prev_signal/prev_close/prev_vwap` (those are the whole point — warm memos so live crosses
detect correctly). Net: the seed warms indicators + memos but arms NO cross; the first live bar detects
crosses freshly.

## Design
New bot method, called once per symbol when it first enters the watchlist (in the scanner-apply path,
before/with subscription), guarded by a `self._db_seeded: set[str]`:

```py
def _seed_strategy_bars_from_db(self, symbol: str) -> None:
    if self.session_factory is None or symbol in self._db_seeded:
        return
    self._db_seeded.add(symbol)
    with self.session_factory() as session:
        rows = session.execute(
            select(StrategyBarHistory)
            .where(StrategyBarHistory.strategy_code == STRATEGY_CODE,
                   StrategyBarHistory.symbol == symbol,
                   StrategyBarHistory.interval_secs == 60)
            .order_by(StrategyBarHistory.bar_time.desc())
            .limit(250)
        ).scalars().all()
    for r in reversed(rows):                      # ascending
        ts_ms = int(r.bar_time.timestamp() * 1000)
        self.strategy.on_bar(symbol, ChartBar(symbol, float(r.open_price),
            float(r.high_price), float(r.low_price), float(r.close_price),
            int(r.volume), ts_ms))                 # old ts → not fresh → no emit
    st = self.strategy.watchlist_state(symbol)     # clear the pending-cross stash
    st.pending_path_macd = False
    st.pending_path_vwap = False
    st.pending_cross_bar_ts_ms = 0
```

`_db_seeded` is pruned alongside the watchlist on de-selection so a re-added symbol re-seeds.

## Tests
- `test_db_seed_clears_min_bars_warmup`: persist 200 bars, seed → `len(state.bars) >= min_bars` and a
  subsequent FRESH cross/touch emits immediately (no 135-bar wait).
- `test_db_seed_emits_nothing_on_replay`: the seed replay itself emits no intent (all bars stale).
- `test_db_seed_clears_pending_cross`: a seed whose LAST bar is a native cross does NOT fire on the first
  fresh bar (pending-cross cleared) — the spurious-entry guard.
- `test_db_seed_idempotent_and_bounded`: re-seed is a no-op; load caps at 250 / `maxlen=300`.

## Out of scope
- The dedup gate itself (left as-is for steady-state REST-vs-streamer).
- `_update_atr_state` / indicator math (frozen oracle).
- Fix (a) (separate PR #313).
