# Track 1 — P3-B (ATR Flip touch-entry) as a v2 entry path — DESIGN

**Status:** design for review. No code yet. Ships **dormant** (flag default OFF).
**Scope:** add a third entry path to `strategy_core/schwab_1m_v2.py` alongside Paths 1/2.
Entry-side only — exits remain the open Track-2 item (v2 still has no managed exits).

**Validation going in:** P3-B (ATR-flip variant B, intrabar touch + liquidity floor) is
positive 5/5 days in the week ledger (#293) — a real but thin, high-frequency edge whose
cost-fragility is the open question, to be answered by live paper + the Phase-2 measured spread.

---

## 1. What ships

A new entry path **"ATR Flip"** that fires when, in an ATR-trailing-stop *short* state, the
current bar's HIGH touches the prior bar's resting trail level — **variant B** from the
parity-confirmed `analysis/atr_flip.py` + `analysis/path3_backtest.py`. Variant A (confirmed
flip) is dropped (settled weak). The **only** filter is the liquidity floor (bar volume > 5000);
**none** of the Paths 1/2 gates apply (operator's "just the script" instruction).

- Indicator: modified true range, ATRPeriod 5, ATRFactor 3.5, Wilders average — **identical math
  to `compute_atr_trail`**, ported verbatim into the v2 module (no cross-import; the module's
  "every fix touches ONLY this file" rule holds).
- Own enable flag `strategy_schwab_1m_v2_atr_flip_enabled`, **default False → deploys dormant**.
- Quantity 10 for the live-paper phase (own setting, independent of the Paths-1/2 default of 100).
- Metadata: `path:"ATR Flip"`, `reference_price` (#284 convention), the trail value, loss, state-age,
  the touch high, and `bar_time_ms` — for auditability + replay reproduction.
- Probe line `[V2-ATR-PROBE]` per evaluated bar (probe-symbol-gated, like `[V2-MACD-PROBE]`) so the
  overnight-audit machinery can reproduce every flip/touch decision.

---

## 2. The core decision: incremental state, NOT recompute-over-slice

The offline `compute_atr_trail` runs over a full session slice (`fetch_day`: 04:00→20:00 ET, state
inits at `bars[0]`). The production strategy can't replicate that by slicing the deque, because
**`SymbolState.bars` is `maxlen=300`** (5 h of 1-min bars) and an ET session is up to 960 min — mid/
late session the deque no longer reaches the 04:00-ET anchor, so a recompute would init the flip
state machine at the wrong point and diverge from the validated backtest.

**Therefore: maintain the ATR-flip state incrementally on `SymbolState`, updated on every bar,
reset at the 04:00-ET session anchor** (the *same* anchor VWAP already uses — `session_start_ts_ms`).

This is correct because **`on_bar` is invoked for every bar — warmup replay AND live** (the cold-
start path feeds the 7-day lookback through `on_bar` in order). So the incremental ATR state sees
every bar since the session anchor even though the deque only *retains* the last 300. State that
must persist across the rolling window lives on `SymbolState`; the short TR/Wilders lookback reads
the deque tail. O(1) per bar.

Trade-off accepted: a new stateful path on `SymbolState` is exactly the kind of state-mutation the
streamer-design-first discipline warns about. Mitigation = a **determinism test that pins the
incremental series against `compute_atr_trail` as the oracle** (§5) — incremental MUST equal batch.

---

## 3. State added to `SymbolState` (all ATR-scoped, reset at session anchor)

| field | purpose |
|---|---|
| `atr_session_anchor_ms` | session-reset detector (mirrors `vwap_session_anchor_ms`) |
| `atr_wilders` | running Wilders(TR,5) value (None until seeded) |
| `atr_tr_seed` | list of the first ≤5 session TRs, for the SMA5 seed (plan's documented seeding) |
| `atr_state` | `'long' \| 'short' \| None` |
| `atr_trail` | current trail level (None until first valid loss) |
| `atr_prev_trail` | prior bar's trail — the level the touch compares HIGH against |
| `atr_prev_state` | prior bar's state — gates "touch only while short" |
| `atr_state_age` | bars since the last flip (audit/metadata) |
| `atr_fired_in_short_seg` | one-entry-per-short-segment guard (offline B `break`s after first touch) |

**Reset rule:** when `session_start_ts_ms(bar_ts)` ≠ `atr_session_anchor_ms`, clear all of the
above and re-seed — identical to how VWAP rolls. This reproduces `fetch_day`'s session windowing.

**Overlapping-state audit (per design-first discipline):** the ATR fields are *write-disjoint* from
every existing `SymbolState` field. They do **not** read or write `prev_macd/prev_signal/prev_close/
prev_vwap`, the VWAP accumulators, or the pending-cross fields. They **share read-only** access to
`state.bars` (deque tail) and **share** the two *gates* `position_qty` and `cooldown_bars_remaining`
(an ATR entry respects flat-and-no-cooldown exactly like Paths 1/2). No existing field is mutated by
the ATR path. → Paths 1/2 behavior is provably unchanged whether the flag is on or off.

---

## 4. Control flow inside `_evaluate_completed_bar`

Inserted **after** the existing Paths 1/2 resolution, as a strictly-lower-precedence third path:

1. **Always** (independent of the flag) update the incremental ATR state for this bar: session-reset
   check → compute modified TR from the deque tail → update/seed Wilders → run the flip state machine
   (close vs prior trail; ratchet or flip), advancing `atr_state/atr_trail/atr_state_age` and rolling
   `atr_prev_*`. Computing unconditionally keeps the state **warm** so Monday's flag-flip fires on a
   correct series immediately (no cold-start gap). This is side-effect-free w.r.t. Paths 1/2 (§3 audit)
   and emits nothing — so it satisfies "dormant / no behavior change."
2. **Touch detection** (before the state machine consumes the current close): if `atr_prev_state ==
   'short'` AND `cur.high >= atr_prev_trail` AND not `atr_fired_in_short_seg` → a touch at level
   `atr_prev_trail`. Set `atr_fired_in_short_seg=True` (reset to False when a new SELL flip opens a
   fresh short segment) — replicating offline B's first-touch-per-segment `break`.
3. `[V2-ATR-PROBE]` log (probe-symbol-gated): ts, close, high, low, tr, loss, trail, prev_trail,
   state, state_age, touch bool, vol, fresh bool, pos_qty, cooldown.
4. **Emit gate** (only here does the flag matter): emit an "ATR Flip" open intent **iff**
   `atr_flip_enabled` AND a touch fired AND `bar_is_fresh` AND `cur.volume > atr_vol_floor` AND
   `position_qty == 0` AND `cooldown_bars_remaining == 0` AND neither Path 1/2 already fired this bar.
   Entry/reference price = the **trail level touched** (`atr_prev_trail`), matching the backtest.

**Precedence:** MACD Cross > VWAP Breakout > ATR Flip. If Path 1 or 2 fired on this bar, ATR does not
also fire (one open intent per bar). Guarantees the ATR path can never alter a Paths-1/2 outcome.

### Bar-close approximation (flagged, per the plan)
v2 is bar-close-only. True variant B is an *intrabar* stop-buy that triggers the instant price rises
through the resting trail. We approximate it by evaluating `high >= trail_prev` **at close** and
filling at the trail level. Consequences, stated honestly:
- **Fill price** = the trail level (idealized, no slippage) — identical to the backtest's `tp`, so
  live sim fills and the replay study agree *by construction* (same rationale the #284 comment gives
  for Paths 1/2 using the bar close).
- **Timing** is one-bar-coarse: we learn of the touch at the close of the bar that touched, not at the
  tick. A real intrabar stop-buy is a **later architecture item** (needs the tick feed — Track 3).
- A bar can have `high >= trail_prev` yet `close < trail_prev` (a wick). Offline B still counts that
  as an entry at the trail; we match it. Live, that fill realism is exactly what Track-3 ticks + the
  Phase-2 measured spread will test. This is the cost-fragility the week ledger flagged.

---

## 5. Tests (mirroring #284 — real emit path + determinism, no hand-injected metadata)

1. **Indicator determinism / parity oracle** — feed a fixed bar fixture *incrementally* through
   `SchwabV2Strategy.on_bar` and assert the per-bar `(trail, state, state_age, flip)` series **equals
   `analysis.atr_flip.compute_atr_trail`** run over the same bars (batch oracle). This is the load-
   bearing test: it proves incremental == the validated offline math, including the SMA5 seed and the
   session reset. (Test may import the analysis oracle; production code does not.)
2. **Real emit-path fill** — engineer a deterministic short→touch sequence so a genuine ATR-Flip
   touch fires; assert the strategy's OWN metadata (verbatim) carries `path:"ATR Flip"` +
   `reference_price` == the trail level, then push it through `SimulatedBrokerAdapter` and assert it
   **fills at the trail level** (no `rejected`). Nothing hand-set — same standard as
   `test_v2_reference_price.py`.
3. **Dormant by default** — with the flag OFF, the same touch sequence emits **no** intent (state is
   computed but nothing fires); and Paths 1/2 output is **byte-identical** with the flag on vs off on
   a Paths-1/2 fixture (proves §3's no-interference claim).
4. **One-entry-per-short-segment** — a short segment with two bars touching the trail yields exactly
   one intent; a later, distinct short segment can fire again.
5. **Liquidity floor** — a touch on a `vol <= 5000` bar does not emit; `vol > 5000` does.

---

## 6. Settings added (defaults = dormant + safe)

```python
strategy_schwab_1m_v2_atr_flip_enabled: bool = False      # the dormant flag
strategy_schwab_1m_v2_atr_flip_quantity: int = 10         # live-paper size
strategy_schwab_1m_v2_atr_flip_vol_floor: int = 5000      # the only filter
strategy_schwab_1m_v2_atr_flip_period: int = 5            # ATRPeriod (parity)
strategy_schwab_1m_v2_atr_flip_factor: float = 3.5        # ATRFactor (parity)
strategy_schwab_1m_v2_atr_flip_probe_symbols: str = ""    # [V2-ATR-PROBE] gate, like the MACD probe
```

---

## 7. Deploy / rollback

- Build → PR → review → after-close/weekend attended deploy, **flag OFF** → verify dormant (no new
  intents, Paths 1/2 unchanged, v2 healthy, token cadence intact). Standard pattern.
- Monday (attended, market hours, paper): flip `…atr_flip_enabled=true`, watch `[V2-ATR-PROBE]` +
  real "ATR Flip" intents fire on live data, compare live timing/fills vs the idealized $64.90.
- Rollback = flag back to false + restart `project-mai-tai-schwab-1m-v2.service`. No schema change,
  no migration; the ATR state is in-memory only.

---

## 8. Honest flags carried into the build

1. **Cost-fragile edge.** P3-B is thin per-trade and the highest-frequency path → the most fills →
   the most spread exposure. The idealized fill at the trail level still has the spread cost *in* it.
   Live paper + Phase-2 measured spread is what makes the number real — don't pre-judge it.
2. **Bar-close ≠ intrabar.** What ships is the close-eval approximation; true intrabar stop-buy is a
   later item gated on the tick feed (Track 3).
3. **Entries without exits.** This adds a v2 entry path while v2 still runs **no** managed exits
   (Track 2, the TOP open item). An ATR-Flip paper position, once opened, has nothing to close it
   until Track 2 lands. This path is only "complete" once exits exist.
