# Design — ATR Intrabar Hold-Confirmation Entry (v2, Schwab-pure)

> **Status:** DESIGN-FIRST, for review BEFORE any PR (per the streamer/entry-path
> design-first discipline). No code changed by this doc. Default-OFF, shadow-first rollout.
>
> **Owner decision this answers:** the offline backtest proved the hold-confirmation entry
> edge is real (10-day save:kill 3.26, keeps 86% of winners, hold-out clean) and the 5-day
> Schwab-feed reconciliation proved it **reproduces on the live Schwab feed** for the names
> v2 trades. This doc specifies how to build it on the live v2 ATR bot with minimum blast radius.

---

## 1. Goal & non-goals

**Goal.** After an ATR trail-touch, instead of entering immediately, watch the next **N seconds
of live quotes** and ENTER only if the move *holds* (the `net_delta` rule). Skip the false-flip
wick-touches that revert — the backtest's screened losers.

**In scope:** the v2 ATR entry decision only (`SchwabV2Strategy`), Schwab LEVELONE feed,
`net_delta` confirmation rule, coverage guard, shadow logging.

**Out of scope (unchanged):** the ATR flip/trail math (`_update_atr_state`, pinned to the
oracle); all exits (OMS scale-ladder owns them); P1/P2 momentum paths (disabled in `atr_only_mode`);
the momentum engine's separate intrabar framework in `strategy_engine_app.py` (different service,
not reused here); position sizing; risk gates.

**Evidence base.** `docs/` backtest result (this session's handoff entry) + memory
`[[project-mai-tai-tick-confirmation]]`. Key numbers the build inherits:
`net_delta @ N=20s` → save:kill ~3.3 full-universe / ~6.5 on liquid covered names, 82–86% winner
retention. `vwap_hold` rejected (degrades on thin ticks: 5.0 vs 7.0). `upticks`/`price_hold`
rejected (over-screen, kill winners).

---

## 2. Current behavior (grounded in code)

File: `src/project_mai_tai/strategy_core/schwab_1m_v2.py` (all v2 strategy logic lives in this one
file by design). Bot/service: `src/project_mai_tai/services/schwab_1m_v2_bot.py`.

```
CHART_EQUITY bar completes
  → bot._handle_bar → strategy.on_bar(symbol, bar)              # :397
      → _evaluate_completed_bar
          → _update_atr_state(state, cur)                       # :463  advances flip state
              touch = (atr_prev_state=='short' and cur.high >= atr_prev_trail
                       and not atr_fired_in_short_seg)          # :533  TOUCH AT BAR CLOSE, uses bar.high
              touch_price = atr_prev_trail; atr_fired_in_short_seg=True
              ... roll atr_prev_trail = atr_trail               # :567
          → _maybe_atr_emit(...) → TradeIntentDraft(entry=touch_price)   # :592 / :637
  → bot._maybe_emit(draft) → intent_emitter.emit               # :1289 / :1365 (reason must contain "ATR Flip")

quote arrives
  → bot._handle_quote → strategy.on_quote(symbol, quote)        # :437 / bot :1291
      state.last_quote = quote; return None                     # NO-OP for entries today
  → bot._maybe_emit(None) → nothing
```

Two facts that make this cheap to build:

1. **`on_quote`'s return is already emitted.** `_handle_quote` does `await self._maybe_emit(draft)`
   exactly like `_handle_bar`. So when `on_quote` returns a real `TradeIntentDraft`, it flows to the
   OMS with zero new plumbing. We only need `on_quote` to *produce* a draft when the hold confirms.
2. **`on_bar` and `on_quote` are both synchronous strategy calls on the single asyncio loop** (the
   `await` is only in the bot's `_maybe_emit`, after the strategy returns). So strategy state is
   **never mutated concurrently** — no lock needed inside the strategy; only logical interleaving
   order matters (a bar may close between two quotes).

**Fidelity note (why intrabar, not bar-anchored):** today the touch is detected at *bar close* from
`bar.high`. The backtest's hold window started at the **intrabar touch instant** (first tick to reach
the trail) and watched N seconds from there. To match the validated edge we must detect the touch
**intrabar in `on_quote`** against the resting trail, not at bar close. The v2 bot already books the
idealized touch_price fill (that's why intrabar-*alone* was a wash), so intrabar touch detection is
consistent with the existing entry-price model — it adds the *hold*, not a new fill assumption.

---

## 3. Proposed design

### 3.1 Core mechanism

The resting trail from the last completed bar (`state.atr_prev_trail`, set while
`state.atr_prev_state=='short'`) is the live touch level. `on_quote` watches it:

```
on_quote(symbol, quote):
    state.last_quote = quote                          # unchanged (freshness)
    px = quote price (mid or last — see 3.4)
    now_ms = quote event time (NOT processing time)

    resolved = _resolve_pending_hold(state, now_ms)   # may return a draft (expiry path)
    if resolved is not None: return resolved

    if HOLD_CONFIRM_ENABLED and _atr_armed(state) and not state.atr_fired_in_short_seg:
        if px >= state.atr_prev_trail:                 # INTRABAR TOUCH
            state.atr_fired_in_short_seg = True         # one-per-segment (shared guard)
            state.pending_hold = PendingHold(
                touch_price = state.atr_prev_trail,
                touch_ms    = now_ms,
                deadline_ms = now_ms + N*1000,
                ticks       = [(now_ms, px)],
            )
            return None                                 # do NOT emit yet — wait out the window
    return None

_resolve_pending_hold(state, now_ms):
    ph = state.pending_hold
    if ph is None: return None
    if now_ms < ph.deadline_ms:
        ph.ticks.append((now_ms, px)); return None      # still accumulating
    # window closed → decide
    state.pending_hold = None
    if len(ph.ticks) < MIN_TICKS:                        # COVERAGE GUARD → fallback = enter
        return _atr_draft(ph.touch_price, reason="ATR Flip [hold:thin-fallback]")
    net_bps = (ph.ticks[-1].px - ph.touch_price) / ph.touch_price * 1e4
    if net_bps >= NET_DELTA_BPS:
        return _atr_draft(ph.touch_price, reason="ATR Flip [hold:confirm]")   # ENTER
    return None                                          # SKIP (false flip screened)
```

`_atr_armed(state)` = `state.atr_prev_state=='short' and state.atr_prev_trail is not None`
(the same precondition the bar-close touch uses).

### 3.2 New state (on `SymbolState`)

```python
pending_hold: PendingHold | None = None     # at most one per symbol (single-position semantics)

@dataclass
class PendingHold:
    touch_price: float
    touch_ms: int
    deadline_ms: int
    ticks: list[tuple[int, float]]           # (event_ms, price) within the window
```

### 3.3 Resolution guarantee (thin-feed safety)

`on_quote` is event-driven; if quotes go silent a pending hold could never reach its expiry check.
Therefore **`_resolve_pending_hold` is also called at the top of `on_bar`** (every completed bar =
≤60s heartbeat). With N=20s a pending hold is always resolved within ≤ N + one bar. This also means
a silent symbol resolves via the coverage-guard fallback (thin → enter), matching the backtest's
`BAR_CLOSE_FALLBACK`.

### 3.4 Feed & price field (Schwab-pure)

Per the 5-day reconciliation, **build stays Schwab-only** — no Polygon wiring, no isolation break.
Use the LEVELONE quote already delivered to `on_quote`. Price field: use **last/mid consistently**
with what the backtest's `market_trade_ticks.price` held (LEVELONE last trade price). Confirm the
live `Quote` object exposes the same field; if only bid/ask are present, use mid and **recalibrate
`NET_DELTA_BPS` accordingly** (the backtest's 5 bps was on last-price).

### 3.5 Order type (the slippage question)

The backtest entered at idealized `touch_price`. Live, after an N=20s hold the market may sit above
`touch_price`. Options:
- **Marketable limit at `touch_price * (1 + SLIP_CAP_BPS)`** (recommended) — fills near touch when
  price held, caps the worst case, surfaces "price ran away" as an unfilled order the OMS already
  handles. `SLIP_CAP_BPS` starts conservative (e.g. 20–30 bps).
- Plain limit at `touch_price` — most faithful to the backtest but will miss fills on exactly the
  winners we kept (price moved up during the hold). **Rejected** — defeats the purpose.

Shadow mode (3.7) measures the real touch→confirm price drift **before** any order type is committed;
the cap is set from that data, not guessed.

### 3.6 Config (all default-OFF / inert)

```
MAI_TAI_STRATEGY_SCHWAB_1M_V2_HOLD_CONFIRM_ENABLED      = false   # master gate
MAI_TAI_STRATEGY_SCHWAB_1M_V2_HOLD_CONFIRM_SHADOW       = false   # log decisions, emit NOTHING new
MAI_TAI_..._HOLD_CONFIRM_N_SECONDS                       = 20
MAI_TAI_..._HOLD_CONFIRM_NET_DELTA_BPS                   = 5
MAI_TAI_..._HOLD_CONFIRM_MIN_TICKS                       = 5
MAI_TAI_..._HOLD_CONFIRM_SLIP_CAP_BPS                    = 25
```

With `HOLD_CONFIRM_ENABLED=false` the code path is byte-inert: `on_quote` returns `None` as today,
no `pending_hold` ever created, `_update_atr_state` bar-close emit unchanged. Proven by a
characterization test (4.1).

---

## 4. State-mutation reconciliation audit (the design-first requirement)

The new intrabar path and the existing bar-close path **both touch the ATR segment state**. Audit of
every shared field and the rule that prevents double-fire / orphaned holds:

| field | bar-close path (`_update_atr_state`) | new intrabar path (`on_quote`) | reconciliation |
|---|---|---|---|
| `atr_fired_in_short_seg` | sets True on bar-high touch; reset False on fresh short segment (:558) | sets True when intrabar touch pends | **shared one-shot guard.** Whichever fires first claims the segment; the other sees `True` and does not re-emit. |
| `atr_prev_trail` / `atr_prev_state` | rolled to this bar's values at bar close (:567) | **read-only** | intrabar reads the *resting* (last-closed-bar) trail — exactly the backtest's "prev short-trail." Never written by on_quote. |
| `atr_state` / `atr_trail` / `atr_loss` | advanced every bar (flip machine) | untouched | flip engine keeps running; hold-confirm is a *gate on emission*, not on the math. |
| `pending_hold` | resolved (read+clear) at top of on_bar | created/accumulated/resolved | single owner per symbol; on_bar heartbeat guarantees resolution. |

**Double-emit safety:** because `atr_fired_in_short_seg` is set the instant a hold *pends* (before the
window closes), a bar that closes mid-window will NOT re-detect the touch. The bar-close emit is thus
suppressed for any segment the intrabar path has claimed.

**Suppress the bar-close ATR emit when enabled:** when `HOLD_CONFIRM_ENABLED`, `_maybe_atr_emit` must
**not** emit on the bar-close touch for an armed segment — that path is replaced by the intrabar
hold. (If a touch is *only* detectable at bar close — `bar.high >= trail` but no quote ever crossed,
e.g. a one-print gap — treat it as the thin/coverage fallback: emit, matching `BAR_CLOSE_FALLBACK`.)
This is the single most delicate edit; the characterization test pins both branches.

---

## 5. Edge cases

1. **Flip-to-long mid-window.** A bar closes during the hold and the segment flips long
   (`atr_state` long). The short setup is invalidated → **discard `pending_hold`, emit nothing.**
   Detect in on_bar resolution: if `atr_prev_state != 'short'`, drop the pending hold.
2. **Position already open / pending_open for the symbol.** If v2 already holds or has a working
   open for the symbol, do not start a hold (mirror today's single-position semantics). Check before
   creating `PendingHold`.
3. **New touch while a hold is pending.** Impossible within a segment (`atr_fired_in_short_seg`
   blocks it). Across a fresh segment, the prior hold has already resolved (fired flag was reset
   only on segment change, which resolves/clears any stale pending).
4. **Thin / zero ticks (illiquid, premarket, unsubscribed).** `< MIN_TICKS` → coverage-guard
   fallback = enter at touch_price. This is the 87%-empty case from the Schwab study; the guard makes
   the feature *inert-safe* on names Schwab doesn't densely cover (it never *blocks* a fill it can't
   judge — it falls back to today's behavior).
5. **Restart while a hold is pending.** `pending_hold` is in-memory only. On restart it is lost →
   the segment's `atr_fired_in_short_seg` also resets on warmup → the touch may be re-evaluated on the
   next bar via the bar-close fallback (enter). Acceptable: worst case is "entered without hold" =
   today's behavior. Document; do NOT persist pending holds (complexity not worth it). Ties into
   OPEN restart-while-holding item — verify under that test.
6. **Session anchor roll (04:00 ET).** `_update_atr_state` resets segment state at the anchor; any
   pending hold straddling the roll is dropped (treated like a flip reset).
7. **Quote event-time vs processing-time.** Use the quote's **event timestamp** for window timing,
   not `datetime.now()` — same lesson as the OMS tick-consumer fix (#333) where processing-time
   staleness mis-stamped late quotes. If LEVELONE lacks a reliable event ts, fall back to receive
   time but widen MIN_TICKS tolerance.
8. **After-hours.** v2 ATR now fills after-hours (#358). LEVELONE is sparser after-hours →
   coverage guard will fall back more often. Acceptable (fallback = today). Watch the
   confirm/fallback ratio in shadow.

---

## 6. Rollout (shadow-first, attended)

1. **Shadow mode** (`HOLD_CONFIRM_SHADOW=true`, `ENABLED=false`): the strategy computes the hold
   decision and **logs** (would-enter / would-skip / would-fallback, tick count, net_bps,
   **touch→deadline price drift = the live slippage proxy**) but emits exactly as today. Runs with
   zero order-path risk. **This is the missing measurement** — the slippage the offline backtest
   could not see. Run ≥5 trading days.
2. **Review shadow data** against the backtest: does the live confirm/skip split, screen rate, and
   winner-retention match ~3–6 save:kill? Is the touch→confirm drift within `SLIP_CAP_BPS`? Set the
   cap from observed drift. If shadow contradicts the backtest, STOP — do not enable.
3. **Enable** (`ENABLED=true`), attended, flat pre-flight, after-close v2-only restart (the
   high-stakes live-money deploy discipline: explicit GO, flat at restart, two-stage verify).
   Marketable-limit order type with the calibrated cap.
4. **Verify live**: first sessions, confirm the hold actually fires, fills land near touch, and the
   skipped setups were genuinely the reverting ones. Roll back instantly via flag (no restart needed
   to disable if read each evaluation) or `ENABLED=false` + restart.

---

## 7. Testing

- **7.1 Characterization (must be green on unmodified main first):** with `ENABLED=false`, the new
  code path produces byte-identical intents to today across a recorded bar/quote sequence
  (replay the v2 determinism harness). Proves inert-when-off.
- **7.2 Unit:** `PendingHold` lifecycle — touch→accumulate→confirm (net_bps≥thr → enter),
  touch→accumulate→reject (net_bps<thr → skip), thin (<MIN_TICKS → fallback enter), flip-mid-window
  (drop), position-open (no hold), on_bar heartbeat resolution, event-time windowing.
- **7.3 Reconciliation:** assert `atr_fired_in_short_seg` blocks the bar-close emit when the intrabar
  path claimed the segment; assert bar-close fallback still fires when no quote crossed.
- **7.4 Real-emit:** a recorded LEVELONE quote stream for a known CDT/SKYQ touch → assert the same
  ENTER/SKIP the offline `feed_compare.py` produced for that candidate.

---

## 8. Risks & open questions

- **R1 — small/concentrated validation.** The Schwab-feed proof is 102 candidates, mostly CDT/SKYQ,
  5 days. Mechanical-transfer proven; robustness not. **Shadow mode is the mitigation** — it is the
  real out-of-sample test on the live feed before any money moves.
- **R2 — slippage unmodeled offline.** The N=20s wait costs entry price on kept winners. Shadow
  measures it; `SLIP_CAP_BPS` bounds it; if the drift routinely exceeds the kept-winner edge, the
  feature is net-negative and must not ship. **This is the one that can still kill the build.**
- **R3 — price field mismatch** (last vs mid). Recalibrate `NET_DELTA_BPS` on whatever the live
  `Quote` exposes; do not assume 5 bps transfers.
- **R4 — interaction with OMS revalidation (#358).** The hold delays the intent by N seconds; verify
  the (now fail-open for tape-less v2) setup-revalidation guard doesn't cancel the delayed-but-valid
  intent. Likely benign (the intent is just emitted later) but assert in an OMS integration test.
- **Q1** — N=20 vs N=15: backtest near-identical; pick 20 (slightly higher save:kill) but make it a
  flag and sweep in shadow.
- **Q2** — should a *confirmed* entry fill at touch_price (limit) or at-market-capped? See 3.5;
  decide from shadow drift data.

---

## 9. Decision summary

| question | answer |
|---|---|
| Build justified? | Yes — backtest save:kill 3.26 / 86% retain, hold-out clean. |
| Feed? | **Schwab-pure** (LEVELONE) — reproduces the edge for v2's traded names; no Polygon, no isolation break. |
| Rule? | **`net_delta` @ N=20s** (feed-robust). Not vwap (thin-tick degrade), not upticks/price_hold (kill winners). |
| Plumbing needed? | Minimal — `on_quote`'s draft already emits; this is strategy logic + one suppression edit + shadow logging. |
| Biggest risk? | Slippage of the 20s wait (unmeasured offline) → **shadow mode measures it before enable.** |
| Rollout? | Default-OFF → shadow ≥5 days → review vs backtest → attended after-close enable → live verify. |

**Recommended next step:** implement behind the flags, land **shadow mode** first (no order-path
change), and let it accrue the live slippage + confirm/skip data the offline work could not produce.
Enable only if shadow confirms the backtest.
