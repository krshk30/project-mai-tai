# schwab_1m_v2 — Entry Criteria (MACD Momentum v1.32)

**Purpose:** a faithful, validate-against-the-code reference of every entry rule, so the operator
can confirm / add / remove rules. Source of truth: `src/project_mai_tai/strategy_core/schwab_1m_v2.py`
(`SchwabV2Config` + `SchwabV2Strategy._evaluate_completed_bar`). **Entry side only** — all exits
(MACD-cross-down / stochastic / quick-stop / scaled / hard-stop) are owned by OMS and are NOT in
this module.

> How to use this doc: each tunable rule has its **current value**, whether it's **ON/OFF**, what it
> does, and a **VALIDATE** prompt. Mark keep / change-to-X / remove. A rule's toggle being OFF means
> the gate passes through (no effect).

---

## 0. When the strategy evaluates at all

- **Bar-close only.** Signals are evaluated once per *new* completed 1-minute bar (`on_bar`, only
  when the bar's timestamp is newer than the last). Quotes update freshness but **never** fire a
  signal (`on_quote` returns None). Same-minute bar revisions do **not** re-evaluate.
- **Warmup gate — `min_bars = 135`.** No evaluation (and no indicator-memo update) until the symbol
  has **135** bars in the deque. Derived: `macd_slow(26) + macd_signal(9) + macd_warmup_settling_bars(100)`.
  Rationale: the SMA-seeded EMA is biased for the first ~100 bars; 135 walls off that zone.
  - VALIDATE: keep 135-bar warmup? (Trade-off: needs ~135 min of history before the first possible
    fire; the REST cold-start batch covers it.)
- **Deque size:** last **300** bars retained (`maxlen=300`).

## 1. Indicators (exact math)

| Indicator | Definition | Params |
|---|---|---|
| **MACD line** | `EMA(close,12) − EMA(close,26)` | fast 12, slow 26 |
| **Signal line** | `EMA(macd_series, 9)` | signal 9 |
| **Histogram** | `macd_line − signal_line` | — |
| **EMA (trend)** | SMA-seeded EMA of close | length **9** |
| **Stochastic %K** | `(close − low5)/(high5 − low5) × 100`; flat range → 50 | length **5**, FullK (smoothK=1) |
| **Avg volume** | mean of last **20** bar volumes | length 20 |
| **Session VWAP** | cumulative `Σ(typical×vol)/Σvol`, `typical=(H+L+C)/3` | anchored **04:00 ET** each day; resets at the anchor |

- **EMA seeding:** first `period` values are simple-averaged, then standard EMA. (This is why the
  135-bar warmup exists.)
- **VWAP anchor:** 04:00 ET (matches the scanner-session roll). Only *new* bars update the
  accumulator (same-minute revisions don't double-count).

## 2. Config (all current values)

| Field | Current | Meaning |
|---|---|---|
| `macd_fast / slow / signal` | 12 / 26 / 9 | MACD lengths |
| `stoch_length` | 5 | stochastic lookback |
| `ema_trend_length` | 9 | trend EMA |
| `rel_vol_length` | 20 | avg-volume lookback |
| `volume_threshold` | **5000** | absolute min bar volume |
| `rel_vol_multiple` | **1.5×** | bar volume must exceed 1.5× avg |
| `macd_hist_min_pct` | **0.02%** | min histogram as % of close |
| `cooldown_bars` | **5** | bars suppressed after a position closes |
| `stoch_max_at_entry` | 90.0 | overbought ceiling (only if enabled) |
| `pending_cross_max_gap_secs` | 180 | C2 carryforward window |
| `macd_warmup_settling_bars` | 100 | → min_bars 135 |
| `MAX_BAR_AGE_SECONDS_FOR_EMIT` | **180s** | freshness window |
| `default_quantity` | 100 (env) | shares per entry |

**Toggles (ON/OFF):**

| Toggle | State | Effect when ON |
|---|---|---|
| `require_uptrend` | **ON** | close must be > EMA(9) |
| `require_macd_strength` | **ON** | hist% ≥ 0.02 |
| `require_green_bar` | **ON** | close > open |
| `require_rel_volume` | **ON** | vol > 1.5× avg |
| `require_vwap_filter` | **ON** | Path-1 VWAP gate (below) |
| `allow_vwap_cross_entry` | **ON** | Path-1 accepts a fresh VWAP cross-up |
| `block_overbought` | **OFF** | (would cap stoch < 90) — currently NO stoch ceiling |
| dead-zone (`dead_zone_start/end`) | **OFF** (0/0) | no time-of-day blackout |

## 3. The two entry paths

A signal fires if **EITHER** path's full condition is true on a fresh bar while flat + off-cooldown.
"Cross" conditions use the **previous bar's** memo, so they fire only on the single *transition* bar.

### Path 1 — "MACD Cross"
```
macd_cross_above        # prev_macd ≤ prev_signal  AND  macd > signal   (the up-cross bar)
AND macd_increasing     # macd > prev_macd
AND vwap_filter_path1   # close > vwap  OR  (allow_vwap_cross_entry AND vwap_cross_above)
AND base_filters        # the 7 gates in §4
```

### Path 2 — "VWAP Breakout"
```
vwap_cross_above        # prev_close ≤ prev_vwap  AND  close > vwap   (the up-cross bar)
AND macd_above_signal   # macd > signal  (NOT necessarily a fresh cross; can be macd<0)
AND macd_increasing     # macd > prev_macd
AND base_filters        # the 7 gates in §4
```

- VALIDATE: Path 2 requires only `macd > signal` (momentum confirmation), **not** `macd > 0` — so
  it can fire with MACD below zero (seen in the replay study, e.g. QH 07:39). Keep, or add a
  `macd > 0` requirement?

## 4. Base filter gates (the 7 — all must pass)

Each is ANDed into `base_filters`, used by **both** paths.

| # | Gate | Rule (current) | ON? | Purpose / VALIDATE |
|---|---|---|---|---|
| 1 | **Trend** | `close > EMA(9)` | ON | only enter above short trend. Keep? |
| 2 | **MACD strength** | `histogram/close×100 ≥ 0.02%` | ON | avoid near-flat crosses. 0.02% is *tiny* — validate the threshold. |
| 3 | **Stoch not-chase** | `stoch%K < 90` | **OFF** | overbought ceiling currently disabled → enters even when stretched. Turn ON? |
| 4 | **Green bar** | `close > open` | ON | only on an up-bar. Keep? |
| 5 | **Relative volume** | `vol > 1.5 × avg_vol(20)` | ON | demand a volume surge. Validate 1.5×. |
| 6 | **Absolute volume** | `vol > 5000` | (always on) | floor out illiquid prints. Validate 5000 for this universe. |
| 7 | **Time-of-day** | dead-zone 0/0 → always allowed | OFF | no blackout window. Add one (e.g. avoid first/last minutes)? |

(Plus the **Path-1 VWAP filter**: `close > vwap` OR a fresh VWAP cross-up.)

## 5. Cross-detection semantics (important)

- Cross flags compare against the **prior bar's** stored `prev_macd / prev_signal / prev_close /
  prev_vwap` (the memo), so a path fires **once** on the transition, not every bar the condition
  stays true. The memo updates on every evaluated bar (including warmup).
- `macd_above_signal` / `macd_increasing` are *level* checks (not transitions).

## 6. Suppression rules (a true signal can still be withheld)

| Suppressor | Rule | Notes |
|---|---|---|
| **Freshness** | bar age > **180s** → no fire | stale bars (e.g. warmup replay) never emit |
| **C2 pending carryforward** | a native cross on a *stale* bar is stashed and may be consumed by the **next fresh bar** if within **180s** AND the cross still holds | prevents losing a real cross at the warmup→live seam |
| **Position** | `position_qty > 0` → no fire | one open position per symbol (entry side) |
| **Cooldown** | `cooldown_bars > 0` → no fire | **5 bars** armed when OMS closes a position (True→False); ticks down each bar |

## 7. On fire

Emits a single `open` / `buy` intent: `quantity = default_quantity` (100), `reason = "schwab_1m_v2
<path>"`, metadata incl. `entry_price` (= bar close), `reference_price` (= bar close, drives the sim
fill), and all indicator values. **No scaling, no exits** — OMS owns those.

---

## 8. Rules-to-validate checklist (quick pass)

Tunables, current value → your call (keep / change / remove):

1. Warmup `min_bars` = **135** → ?
2. MACD lengths **12/26/9** → ?
3. MACD strength `hist% ≥ 0.02%` → ? (very low)
4. Trend `close > EMA(9)` ON → ?
5. Green-bar ON → ?
6. Rel-vol `> 1.5×(20)` ON → ?
7. Abs-vol `> 5000` → ? (universe-specific)
8. Stoch overbought ceiling **OFF** (`< 90`) → turn ON?
9. Path-2 allows `macd < 0` (only `macd > signal`) → add `macd > 0`?
10. VWAP filter (Path 1) ON + allow-cross ON → ?
11. Cooldown **5 bars** → ?
12. Freshness **180s** + C2 carryforward → ?
13. Time-of-day blackout **OFF** → add one?
14. Default quantity **100** → ?

> Note (from the Replay Study Phase 1, directional): MACD-Cross outperformed VWAP-Breakout; the
> median signal had no free edge (tail-driven); nothing survived a 2% slippage haircut. Useful
> context when deciding which gates to tighten vs. add.
