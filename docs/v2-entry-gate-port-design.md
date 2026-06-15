# v2 entry-gate port — wave plan + WAVE 1 design (design-first; nothing ported)

**Status:** design only — no code changed, nothing live. Brings the Wave-1 design for operator review.
Source of the gates being ported: [[schwab-1m-entry-gates-extracted]] (`docs/schwab-1m-entry-gates-extracted.md`).
Motivation: the per-path P&L diagnostic showed v2's P1/P2 (MACD/VWAP) chase exhausted overbought tops and
lose **even idealized**; schwab_1m had a gate for every one of those failures. v2's signal isn't broken —
it's missing the gate stack.

## Principles (apply to ALL waves)
1. **P1/P2 ONLY.** The new gates wrap v2's MACD-Cross and VWAP-Breakout native triggers. **ATR-Flip is left
   untouched** (separate later work — its entry logic + the line-676 warmup fix are their own thing).
2. **Config-driven.** Every threshold is a `SchwabV2Config` field, tunable without a code change; every gate
   has an enable flag so it can be toggled independently (and so "all off" == today's behavior, making the
   port **behavior-neutral when disabled**).
3. **Waves, not big-bang.** One small set of gates per wave so each gate's effect is *attributable* on
   forward data, and so we don't over-gate v2 into barely trading. Forward-test each wave before the next.
4. **Thresholds are STARTING POINTS, not frozen.** The values (80–90 stoch, 8%/10% distance, etc.) were
   tuned on **schwab_1m's** context (its symbols, its 1m cadence). v2's penny names can move 30–50% in
   seconds — so these are starting points to **validate + re-tune against v2's forward + Phase-2 data**, not
   ported as gospel.
5. **Idealized + tiny-sample caveat stands.** The diagnostic was idealized fills over one afternoon
   (1 MACD / 6 VWAP / 14 ATR completed). Phase-2 spread data (accumulating) is the profitability arbiter.

## Wave plan at a glance

| Wave | Gates | New indicators | Lift | Status |
|---|---|---|---|---|
| **1** | (a) overbought block; (b) EMA9 + VWAP over-extension caps | **none** (v2 has stoch, EMA9, VWAP) | tiny | **this doc — for review** |
| **2** | (c) EMA20 + `close>EMA20` uptrend; (d) 6-pt quality score + `bars_below_signal≥3` + 1-bar confirmation | EMA20 (trivial) | medium | outlined; build after W1 forward results |
| **3** | chop-regime lock (4-detector + lock/release) | ATR(14) + state machine | large | **DEFERRED** — build only if W1+W2 don't capture most of the gain |

Rationale for the order: cheapest + highest-leverage first (W1 directly targets the dominant *measured*
failure — overbought). Don't build the most complex piece (chop machine) until forward data shows it's
needed.

---

# WAVE 1 — DETAILED DESIGN (the one to review now)

## Where it plugs in
v2's P1/P2 fire only if `base_filters` (and `vwap_filter_path1` for P1) pass — `schwab_1m_v2.py:741-776`.
`base_filters` is an AND of: `trend_ok · macd_strength_ok · stoch_not_chase · green_bar_ok · rel_vol_ok ·
vol_abs_ok · time_allowed`. Wave 1 activates one existing gate and adds two new ones to that AND. **Only the
P1/P2 native-trigger path consumes `base_filters`; ATR-Flip does not** — so ATR is structurally untouched.

## (a) Enable the overbought block — ZERO new code, one flag
v2 already computes stoch %K and already has the gate:
`stoch_not_chase = (not block_overbought) or (stoch_k < stoch_max_at_entry)` (`schwab_1m_v2.py:751`), with
`block_overbought=False`, `stoch_max_at_entry=90.0` (`SchwabV2Config:94-95`). The gate is wired but
**disabled**. Wave 1(a) = set `block_overbought=True` and pick the level.

**Starting level — the diagnostic data (losers vs the one VWAP winner):**

| cap | losers screened (of 6: stoch 99.6/55.3/82.1/82.5/90/95) | PRFX winner (79) kept? |
|---|---|---|
| 90 (schwab universal) | 3 (99.6, 90, 95) | ✅ |
| **85 (proposed start)** | 3 (99.6, 90, 95) | ✅ |
| 80 (schwab P3 cap / diagnostic-optimal) | 5 (all but HUBC 55) | ✅ (79 < 80, just) |

**Proposed start: `stoch_max_at_entry=85`, `block_overbought=True`** — conservative (low over-gating risk)
while still cutting the clearly-overbought entries. The diagnostic suggests **80** is more effective
(screens 5/6) and still keeps the winner, but it's a single-winner sample, so I'd rather start at 85 and
*tighten toward 80 on forward data* than start aggressive and risk over-gating. Operator picks; it's one
config value either way. (The stoch-55 loser, HUBC, is a weak-momentum case that Wave 2's quality score
catches, not the overbought cap.)

## (b) EMA9 + VWAP over-extension caps — uses existing indicators
v2 already computes `ema_trend` (this **is EMA9** — `ema_trend_length=9`) and `vwap`. Add the distance
computations + two config-gated caps, mirroring schwab_1m's `_common_gate_state`:

```
ema9_dist_pct = (close - ema_trend) / ema_trend * 100      # ema_trend == EMA9
vwap_dist_pct = (close - vwap) / vwap * 100
ema9_dist_ok  = (not use_ema9_max_dist) or (ema9_dist_pct < ema9_max_dist_pct)
vwap_dist_ok  = (not use_vwap_max_dist) or (vwap_dist_pct < vwap_max_dist_pct)
```
New `SchwabV2Config` fields (defaults are schwab_1m's starting points):
`use_ema9_max_dist=True, ema9_max_dist_pct=8.0`, `use_vwap_max_dist=True, vwap_max_dist_pct=10.0`.
Add `ema9_dist_ok` and `vwap_dist_ok` to the `base_filters` AND (P1/P2 only).

**Honest scoping note (don't oversell this one):** the diagnostic measured that v2's losers were *above*
VWAP+EMA (6/6) and overbought (stoch 84), but it did **not** measure their distance % — so we don't yet know
whether v2's specific losers were >8%/>10% extended or only mildly so. Therefore **(a) the overbought cap is
the primary, evidence-backed fix; (b) the distance caps are a guard against the worst over-extension** whose
8%/10% values are schwab-context starting points likely needing re-tuning for v2's higher-volatility names
(could be too tight *or* too loose). Wave-1 forward data should report the dist% distribution so we can set
v2-appropriate caps.

## What Wave 1 changes (and doesn't)
- Makes P1/P2 **more selective** → fewer, higher-quality MACD/VWAP entries. Expected: lower stop-rate on
  P1/P2, lower entry count. **Watch for over-gating** (entry count collapse → loosen).
- **ATR-Flip: unchanged** (doesn't read `base_filters`).
- **Behavior-neutral when flags off** → the port can ship dormant and be enabled attended, like the exit work.

## Tests (Wave 1)
- Overbought MACD/VWAP setup (stoch ≥ cap) → **no longer fires**; same setup with stoch < cap → still fires.
- Over-extended setup (ema9_dist ≥ cap or vwap_dist ≥ cap) → no longer fires; in-range → fires.
- A clean, in-trend, non-overbought MACD-cross → still fires (no false-negative).
- **ATR-Flip path unaffected** by all three gates (assert an ATR touch still emits with the gates on).
- **Flags-off == current behavior** (behavior-neutral parity: with `block_overbought=False` +
  `use_*_max_dist=False`, P1/P2 fire exactly as today).

## Deploy + forward-test (when approved — design-first for now)
- Ship dormant (flags off) → attended enable (flags on) during/after RTH, v2-only restart (like the exit +
  tick-capture flips). Behavior-neutral until flipped.
- **Attribution:** measure per-path stop-rate, win-rate, and entry-count vs the pre-Wave-1 baseline; toggle
  (a) and (b) independently if needed to isolate each. Report the dist% distribution to calibrate (b).
- **Rollback:** flags off + restart v2 → today's behavior.

---

# WAVE 2 — OUTLINE (build after Wave-1 forward results)
- **(c) EMA20 uptrend:** add an EMA20 indicator (`ema20_length=20`; v2 is EMA9-only today) + a config-gated
  `close > EMA20` gate in `base_filters`. New indicator is trivial (already has closes). This is schwab_1m's
  `require_above_ema20` — a *structural* trend filter stronger than v2's EMA9-only check.
- **(d) Quality score + bars-below + confirmation:** port schwab_1m's 6-point `_quality_score`
  (hist_growing, stoch_rising, price>vwap, vol>floor, macd_increasing, price>ema9&ema20) with a config
  `min_score`; add `bars_below_signal ≥ 3` for P1; add a **1-bar confirmation** state machine (wait one bar,
  cancel on macd/stoch cross-down, re-check). The confirmation introduces new per-symbol state + timing — the
  medium-lift part of W2. All inputs already exist in v2.

# WAVE 3 — DEFERRED (decision-gated)
- **Chop-regime lock:** ATR(14) + the 4-detector state machine (COMPRESS / EMA20_FLAT / WHIPSAW /
  NO_CLEAN_SIDE) + lock/release logic. Biggest lift by far. **HOLD** — only build if Waves 1+2 leave material
  chop-driven losses on forward data. Don't build the most complex piece speculatively.

---

## Out of scope / reminders
- Everything here is **design-first**; any port is a gated, attended change to the live entry path (PR held,
  tests green, dormant-then-attended-enable), validated wave-by-wave on forward + Phase-2 data.
- Thresholds (85/8%/10%, and W2/W3 values) are **starting points from schwab_1m's context**, explicitly to
  be re-tuned against v2's data — not frozen.
- ATR-Flip entry path is **not** touched by any wave.
