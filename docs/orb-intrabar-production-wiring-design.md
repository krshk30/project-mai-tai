# ORB (P6 "OPEN") — intrabar production wiring (DESIGN-first, HELD for review)

> **Status: design-first, held for review. NO admin-merge. Default OFF / flag-gated / inert.**
> Research is settled (`orb-opening-range-exit-research.md`). This wires the parity-proven ORB logic
> into production behind `orb_enabled` (default False → never reached → backward-compatible by
> construction). Touches the regression-check hot files (`strategy_engine_app.py`, `oms/service.py`) —
> hence design-first + held + a sliced rollout.

## Settled config (the thing being wired)
- **ENTRY:** 5-min OR from 09:30 · close > OR_high · vol ≥ 1.5× OR_avg · close > VWAP · close > EMA9 ·
  skip if OR_width% > 12% · cutoff 10:30 · one trade/symbol · **only pre-09:25-confirmed names.**
- **EXIT:** TRAIL-8% (ratchets 8% below the high-water-mark, never down).
- **Execution:** intrabar on the current LEVELONE feed (no TIMESALE dependency).

## What's IN this PR (safe, additive, tested)
- `strategy_core/orb_intrabar.py` — pure, import-clean leaf: `build_opening_range`, `bar_confirms_breakout`,
  `entry_fill_price` (BAR_CLOSE = parity / INTRABAR = fill at OR_high), `in_pre_open_universe` (the guard),
  `TrailingStop` (arm / ratchet-never-down / breach). No I/O, no global state.
- `tests/unit/test_orb_intrabar.py` — 12 tests (OR/width-cap/coverage, breakout filter, fill modes, the
  pre-09:25 guard, TRAIL-8% ratchet + inert-default + a full-position parity check). All green.
- `settings.py` — `orb_*` flags, **all default-off/inert**.

## What is DESIGNED here for the NEXT reviewed slice (the hot-file diffs)
Not included as code in this PR — they touch live-money hot files and land as a separate slice with
characterization tests (behavior-identical methodology), gated so default-off is byte-identical.

### Architecture decision (recommend: isolated bot)
Two options. **Recommended: run ORB as an isolated bot/service like `schwab_1m_v2`** (its own process,
own watchlist registration), NOT a new path inside the shared `strategy_engine_app.py`. Rationale: v2 was
deliberately isolated to escape the shared-engine regression chain; ORB inherits that safety and the
P1/P4/P5/ATR paths are physically untouched. The shared-engine-path option is viable too (the accepted
`intrabar-execution-design.md` per-path `ExecutionPolicy` map guarantees isolation: a `PendingEntry` is
created only by a non-bar_close path, so other paths stay byte-identical), but it puts ORB in the hot file.
Decision for review: **isolated bot** unless there's a reason to share the engine.

### Entry — arm-on-window-open trigger (the new bit vs the accepted confirm design)
The accepted intrabar design arms a `PendingEntry` *after* an on_bar setup. ORB instead **arms at the
watch-window open** with the frozen OR_high as the trigger level, then fires intrabar on the cross:
1. At 09:30, for each symbol **in the pre-09:25 universe** (guard below), start building the 5-min OR.
2. At 09:35 (OR close), freeze OR_high/low/avg-vol; if `build_opening_range` returns None (insufficient
   in-time coverage **or** width > 12%) → **do not arm** (skip-this-symbol).
3. From 09:35 to 10:30, on each LEVELONE tick/bar: if `bar_confirms_breakout` (close>OR_high + vol + VWAP +
   EMA9) → emit the open intent at `entry_fill_price(mode)`; INTRABAR fills at OR_high. One trade/symbol.

### The pre-09:25 universe guard (the binding rule — operator decision 2026-06-18)
ORB considers **only names on the scanner list / confirmed before 09:25.** A name that confirms during
09:25–10:00 is **out of scope by design** (no clean OR; typically off a downtrend) — not a missed trade.
Predicate (already available — `lifecycle_states[symbol].last_confirmed_at`):
`in_pre_open_universe(last_confirmed_at, session_open, lead_minutes=5)` → arm iff
`last_confirmed_at ≤ 09:25`. ARM-0 on a day with no pre-09:25 names is **correct** (ORB sits the day out).

### Exit — OMS TRAIL-8% ratchet (the only OMS change)
`oms/service.py` `ArmedHardStop` today has a **fixed** `stop_price`. Add two additive fields
(`trail_pct: float = 0.0`, `high_water_mark: Decimal`) and, in `_evaluate_hard_stop_market_event` (already
runs per LEVELONE quote/trade, #333), ratchet before the breach check:
`if trail_pct>0: hwm=max(hwm, price); stop_price=max(stop_price, hwm*(1-trail_pct/100))`.
**Default `trail_pct=0` → no ratchet → byte-identical to today.** ORB arms its stop with `trail_pct=8`.
Gap-through risk is quantified and accepted (0.34% of bars; 9.4% of trades; worst ~14–16%).

## Backward-compatibility & isolation
- `orb_enabled=False` (default) → the strategy-engine hook is never entered and no ORB intent is emitted →
  byte-identical to today. `trail_pct=0` default → OMS ratchet inert.
- BAR_CLOSE mode == canonical ORB (parity proven EXACT 159/159, 25 days).
- Per-path / per-bot isolation: other paths/bots untouched (isolated-bot option = physically; shared-engine
  option = via the `ExecutionPolicy` guarantee).

## Slice / rollout plan
1. **This PR (held):** leaf + tests + settings (inert). ← review the logic + the design.
2. **Slice 2 (held, characterization tests):** OMS `ArmedHardStop` ratchet (additive, gated).
3. **Slice 3 (held, characterization tests):** the ORB bot/path wiring + arm-on-window-open + universe guard.
4. **Small attended live** (operator): flip `orb_enabled` for a tiny, attended, thin-tail-sized run.

## Honest framing (carried from the research — do not oversell)
ORB is a **thin-edge, runner-dependent** strategy; **leading candidate, not a verdict.** Whether enough
pre-09:25 names appear to make it worthwhile, whether intrabar actually helps, and the real fills are all
**live-only** questions — parity (done) is sufficient for backtest. TIMESALE is a later refinement, not a
blocker. Gap-through on the hard trailing stop is rare but real; thin-tail sizing absorbs it.
