# schwab_1m entry-gating — extracted from the actual gate code (read-only reference)

**Status:** extraction/understanding only. **No code ported, nothing changed.** Purpose: understand the
retired `schwab_1m` (1-minute) bot's mature entry gating so we can validate it against v2's diagnosed
failure mode (chasing exhausted overbought tops) before deciding how to bring any of it forward.

## ⚠️ Source-of-truth correction
schwab_1m's gates are **NOT** in `strategy_core/entry.py` (that `EntryEngine` with P1_MACD_CROSS /
pretrigger_reclaim/retest is the **macd_30s / macd_1m** family). schwab_1m runs
**`SchwabNativeEntryEngine` in `strategy_core/schwab_native_30s.py`** (wired at
`services/strategy_engine_app.py:3883,3938` via `_resolve_1m_trading_config(variant="schwab")` →
`TradingConfig.make_1m_schwab_native_variant`). Its paths are **P1_CROSS / P2_VWAP / P3_SURGE / P4_BURST /
P5_PULLBACK** (no pretrigger modes). Config resolution: `TradingConfig` base → `make_30s_schwab_native_variant`
(`exit_logic/config.py:386`) → `make_1m_schwab_native_variant` (`config.py:484`). The env override
`strategy_schwab_1m_config_overrides_json` defaults to `""` (`settings.py:279`), so the resolved defaults
below are exactly what ran. **All numbers below are the deployed defaults.**

---

## 1. Indicators schwab_1m computes (and where) — `SchwabNativeIndicatorEngine.calculate` (schwab_native_30s.py:728-858)

Computed on REAL bars only, then forward-expanded over synthetic/gap bars (`indicators.py` primitives;
`IndicatorConfig` defaults at `strategy_core/config.py:7-16`):

| Indicator | Periods / source | file:line |
|---|---|---|
| **EMA9** | `ema(real_closes, 9)` | 761 |
| **EMA20** | `ema(real_closes, 20)` | 762 |
| MACD line/signal/hist | 12/26/9 | 755 |
| Stochastic %K / %D | len 5, smooth-k 1, smooth-d 3 | 759-760 |
| VWAP | session-anchored 9:30–16:00 ET | 763-773 |
| vol_avg20 / vol_avg5 | SMA(vol,20) / SMA(vol,5) | 774-775 |
| ATR (chop only) | Wilder, len 14 | 1531 |
| ema9_dist_pct / vwap_dist_pct | `((close-X)/X)*100` | 799-800 |
| bars_below_signal | recent real bars with macd≤signal | 796-797 |

Warmup: no signal until `len(real_bars) ≥ 35` (`schwab_native_warmup_bars_required=35`, 733-740,1061).

---

## 2. Universal gates — apply to EVERY path before it can fire

### Hard gates `_check_hard_gates` (1992-2009)
flat (no position) · dedup (one fire/bar) · **cooldown 5 bars** after exit · open-reject cooldown ·
time window.

### Time window `_time_allowed` (2011-2018)
**07:00 ≤ hour < 18:00 ET** (`trading_start_hour=7`, `trading_end_hour=18`); dead-zone disabled.

### Common gate bundle `_common_gate_state` (1674-1713) — **the layered "quality" filter**
All config-driven; ANDed into `common_ok`, then `p1p2_ok`:

| Gate | Condition | Default | Reads |
|---|---|---|---|
| **EMA20 uptrend** | `close > ema20` | `require_above_ema20=True` | EMA20 |
| **Overbought ceiling** | `stoch_k < 90` | `use_stoch_k_cap=True`, `stoch_k_cap_level=90` | Stoch %K |
| **EMA9 over-extension cap** | `ema9_dist_pct < 8%` | `use_ema9_max_dist=True`, `ema9_max_dist_pct=8.0` | EMA9 |
| **VWAP over-extension cap** | `vwap_dist_pct < 10%` (RTH only) | `vwap_max_dist_pct=10.0` | VWAP |
| **Abs volume** | `volume > 2500` | `vol_min=2500` | volume |

`common_ok = ema_gate_ok AND stoch_gate_ok AND ema9_gate_ok`; `p1p2_ok = common_ok AND vwap_gate_ok`;
`p3_ok = (common_ok AND (vwap_gate_ok OR p3_high_vwap_ok))` (P3 may relax VWAP-dist to 30% if
`close>ema9>ema20` + ema9 rising + ema9_dist≤2%, 1688-1696).

### Chop-regime lock `_evaluate_chop_lock` (1500-1608) — blocks P1/P2/P3 when market is choppy
`schwab_native_use_chop_regime=True`. Four "hits" (COMPRESS / EMA20_FLAT / WHIPSAW / NO_CLEAN_SIDE);
lock engages at **≥2 hits** (`chop_trigger_min_hits=2`); releases on a clean restart (5 closes>vwap +
ema20 rising + breakout). While locked: P1/P2 blocked, P3 blocked unless extreme-momentum override.

### Confirmation + quality score (P1/P2/P3/P4; not P5) — `_advance_confirmation` (1143-1209)
`confirm_bars=1`: waits 1 bar, **cancels if macd_cross_below or stoch_cross_below**, then re-scores.
`_quality_score` (1715-1732) = 6 booleans (hist_growing, stoch_rising, price>vwap, vol>2500,
macd_increasing, price>ema9 AND price>ema20). **Required: P1/P2/P4 ≥ 4; P3 ≥ 6.**

---

## 3. Per-path entry criteria (all gates must pass)

### P1_CROSS — MACD cross (1227-1272)
`macd_cross_above` (fresh cross) · `bars_below_signal_prev ≥ 3` (`p1_min_bars_below_signal=3`) ·
**`p1p2_ok`** (the common bundle) · `volume ≥ vol_avg20 × 1.25` (`p1_min_vol_ratio`) ·
`volume ≥ 7500` (`p1_min_volume_abs`) · `close×volume ≥ 25000` (`p1_min_dollar_volume_abs`) ·
`volume > 2500` · not chop-locked · then confirmation, score ≥ 4.

### P2_VWAP — VWAP breakout (1274-1299)
`price_cross_above_vwap` · `macd_above_signal` · `macd_increasing` · **`p1p2_ok`** · `volume > 2500` ·
not chop-locked · confirmation, score ≥ 4.

### P3_SURGE — MACD continuation surge (1301-1348)
`macd_above_signal AND NOT cross` · `macd_delta ≥ -0.001` · delta accelerating · `hist ≥ 0.01`
(`p3_histogram_floor`) · `price>ema9` · `volume ≥ 20000` · `close×volume ≥ 70000` ·
`volume ≥ vol_avg20 × 1.50` · `ema9_dist_pct < 2%` (`p3_max_ema9_dist_pct`) ·
**bars-since-cross ≤ 2** (`p3_max_bars_since_macd_cross`) · **recent runup ≤ 8% over 4 bars**
(`p3_max_recent_runup_pct` — anti-chase) · `p3_ok` · **stoch_k < 80** (`p3_entry_stoch_k_cap`, tighter than
universal 90) · 30-min pause after a P3 hard-stop · score ≥ **6**.

### P4_BURST — volume/range breakout candle (1350-1381) — fallback (only if P1/P2/P3 unavailable)
green · body ≥ 4% (`p4_body_pct`) · close in top 20% of range · `volume ≥ vol_avg20 × 2.0` ·
new local high · `close>ema9` · `ema9_dist_pct < 3.5%` · confirmation (break setup-high + close-top 50%).
Does NOT use the common EMA20/stoch/vwap bundle — its own structure is the filter.

### P5_PULLBACK — spike-then-pullback resumption (1460-1498) — immediate, bypasses score
Requires a prior spike anchor (green bar ≥2.5% above EMA9). Fires on: pullback giveback ≥2% · support
touch near EMA9 (±1%) · green resume bar · `close>ema9` · body <3.5% · close-pos ≥0.5 ·
`vol ≥ vol_avg5 × 0.9` · breakout over 3 bars · ema9 not falling · 3% upmove over 12 bars. Own structure
is the filter (no common bundle, no score).

---

## 4. SAME vs DIFFERENT — schwab_1m vs v2's current gating

**SAME (shared signals):** the raw MACD-cross (P1) and VWAP-cross (P2) triggers are the same idea; both
read MACD(12/26/9), VWAP (9:30 ET anchor), stoch, EMA, rel-vol. v2's P1/P2 base triggers map to schwab_1m
P1_CROSS / P2_VWAP. **What differs is the surrounding gate stack** — and it's large.

v2's gate code = `schwab_1m_v2.py:725-776` (`base_filters` + `vwap_filter_path1`), config
`SchwabV2Config:65-111`. Comparison of the gates that decide whether a cross fires:

| Gate (schwab_1m) | schwab_1m value | v2 equivalent | v2 status | Gap |
|---|---|---|---|---|
| **Overbought ceiling (stoch %K)** | `< 90` universal, `< 80` on P3 | `stoch_max_at_entry=90` exists but `block_overbought=False` | ❌ **DISABLED** | **THE gap** — v2 has the field but the gate is off, so it chases overbought (losers avg stoch 84) |
| **Trend filter** | `close > EMA20` (+ EMA9>EMA20 + ema9 rising on P3) | `require_uptrend=True` but on **EMA9** (`ema_trend_length=9`) | ⚠️ weaker | v2 has **no EMA20** at all; EMA9-only is a much shorter/looser trend |
| **EMA9 over-extension cap** | `ema9_dist_pct < 8%` (P3: <2%) | none | ❌ missing | v2 doesn't cap how far above EMA9 it enters (buys extended) |
| **VWAP over-extension cap** | `vwap_dist_pct < 10%` | only `close>vwap` / cross | ❌ missing | v2 enters any distance above VWAP (buys extended) |
| **MACD strength / score** | P1 `bars_below_signal≥3` + 6-pt quality score (≥4, P3≥6) | `macd_hist_min_pct=0.02%` only | ⚠️ near-off | v2's 0.02% is basically off; losers fired at hist% 0.18–0.56, the winner at 2.10 |
| **Confirmation bar** | 1-bar confirm, cancels on macd/stoch cross-down | none (fires on the signal bar) | ❌ missing | v2 has no "did it hold?" recheck |
| **Chop-regime lock** | 4-signal lock, blocks P1/P2/P3 in chop | none | ❌ missing | v2 fires into chop |
| **Anti-chase: recent runup** | P3 `runup ≤ 8% / 4 bars`; cross-age ≤ 2 | none | ❌ missing | v2 fires late into extended moves |
| **Rel-volume** | P1 ×1.25, P3 ×1.50, P4 ×2.0, +abs+dollar floors | `rel_vol_multiple=1.5`, `volume_threshold=5000` | ✅ comparable | roughly matched (rel-vol was NOT the discriminator in the diagnostic) |
| **Abs/dollar volume floors** | P1 ≥7500 & ≥$25k; P3 ≥20k & ≥$70k | `volume_threshold=5000` only | ⚠️ weaker | v2 lacks the dollar-volume floor |
| **Green bar** | (structural in P4/P5) | `require_green_bar=True` | ✅ has | — |
| **Cooldown after exit** | 5 bars | (OMS-side) | ~ | n/a here |
| **Extra paths** | P3 surge, P4 burst, P5 pullback | none (v2's 3rd path is ATR-Flip, different) | — | different path families |

**Bottom line:** v2 fires a MACD/VWAP cross on essentially the raw trigger + a near-off MACD-strength check
+ a green bar + EMA9-uptrend + rel-vol + `close>vwap`. schwab_1m wraps the **same trigger** in a layered
quality gate: **EMA20 uptrend, stoch<90 overbought block, EMA9/VWAP over-extension caps, a 6-point quality
score with a confirmation bar, a chop-regime lock, and anti-chase runup/cross-age limits.** Every one of
those except rel-vol is **missing or disabled in v2.**

---

## 5. Direct match to v2's diagnosed failure (exhaustion-chasing)

The diagnostic found v2's MACD/VWAP losers: **stoch ~84 (overbought), 6/6 above VWAP+EMA (extended),
hist% 0.18–0.56 (weak), immediate reversal (peak ~0)**. schwab_1m had a gate for **every one** of those:

| v2 loser symptom | schwab_1m gate that would have blocked it |
|---|---|
| stoch ~84 (overbought) | **stoch < 90** universal (`stoch_k_cap_level`), **< 80** on P3 |
| extended above VWAP | **`vwap_dist_pct < 10%`** cap |
| extended above EMA | **`ema9_dist_pct < 8%`** cap + **EMA20** uptrend structure |
| weak momentum (hist% ~0.3) | **6-pt quality score ≥ 4** (hist_growing, macd_increasing, …) + P1 `bars_below_signal≥3` |
| fired late into the move | **anti-chase**: P3 `runup ≤ 8%/4bars`, `cross-age ≤ 2 bars` |
| fired into chop → immediate reversal | **chop-regime lock** |

So the retired bot's gates are **directly on-point** for v2's failure mode — strongly suggesting v2's P1/P2
problem is **fixable by porting gates**, not a fundamentally broken signal. (The two highest-leverage,
lowest-effort ports, per the diagnostic: **enable the overbought block** — v2 already has the field, just
flip `block_overbought=True` at ~80–90 — and **add EMA20-distance + VWAP-distance caps**.)

---

## 6. Config-driven vs hardcoded
**Effectively everything is config-driven.** All thresholds above are `TradingConfig` fields set by
`make_1m_schwab_native_variant` (← `make_30s_schwab_native_variant`), tunable per-bot and overridable via
`strategy_schwab_1m_config_overrides_json` (validated against `TradingConfig.__dataclass_fields__`;
default empty). The only **hardcoded** literals in the gate code are structural constants: the VWAP session
window (9:30–16:00 ET), the 6-boolean score shape, P5's `ema9 ≥ ema9_prev × 0.995`, ATR Wilder math, and
the warmup count usage. (Full config field list with defaults is in the extraction notes; the gate-relevant
ones are inlined above.)

---

## 7. Portability flags — what v2 would need to ADD to honor each gate

v2 today computes (in `_evaluate_completed_bar`, schwab_1m_v2.py): MACD(12/26/9), VWAP, **EMA9 only**
(`ema_trend_length=9`), stoch %K (len 5), avg_volume(20). To honor schwab_1m's gates, v2 would need:

| schwab_1m gate | v2 has the input? | Port requirement |
|---|---|---|
| stoch < 90 / < 80 overbought | ✅ (stoch %K computed; field exists) | **flip `block_overbought=True`** + set level — zero new indicators |
| EMA20 uptrend | ❌ v2 has no EMA20 | compute EMA20 (cheap; already has closes) |
| EMA9 over-extension cap | ✅ (EMA9 + close) | add `ema9_dist_pct` + a cap field |
| VWAP over-extension cap | ✅ (VWAP + close) | add `vwap_dist_pct` + a cap field |
| 6-pt quality score + confirmation bar | ⚠️ partial | port the score (all inputs exist) + a 1-bar confirmation state machine (new state) |
| chop-regime lock | ❌ needs ATR + the 4 chop detectors | compute ATR(14) + the chop state machine (largest lift) |
| anti-chase runup / cross-age | ✅ (has bars) | add the lookback computations + caps |
| P1 `bars_below_signal ≥ 3` | ✅ (has macd history) | add the counter + gate |
| abs/dollar volume floors | ✅ (vol + close) | add floor fields |
| P3 surge / P4 burst / P5 pullback paths | n/a | these are whole new paths (out of scope for fixing P1/P2) |

**No gate requires a data source v2 can't compute from its existing bar stream** — the only genuinely new
indicator is **EMA20** (trivial) and **ATR(14)** (for the chop lock). Everything else is new *config + gate
logic* on indicators v2 already has. The single biggest lift is the chop-regime state machine; the single
biggest win-per-effort is **enabling the overbought block** (already wired, one flag).

---

## Scope notes
- Numbers are deployed defaults (env overrides empty). Cite `schwab_native_30s.py` + `exit_logic/config.py`
  for schwab_1m; `schwab_1m_v2.py:65-111,725-776` for v2.
- This is extraction only — **validate against forward/Phase-2 data before porting**, and remember v2's
  P&L diagnostic was idealized + tiny-sample (1 MACD / 6 VWAP / 14 ATR, one afternoon). Porting gates is a
  *design-first* change to the live entry path, gated like everything else.
- The `entry.py` `EntryEngine` (P1_MACD_CROSS + pretrigger reclaim/retest) belongs to the **macd_30s/macd_1m**
  family, NOT schwab_1m — documented here only to avoid future confusion about which engine is which.
