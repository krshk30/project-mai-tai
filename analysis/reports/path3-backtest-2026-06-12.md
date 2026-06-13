# Path 3 (ATR Flip) — Phase 1 Backtest, scalp lens (2026-05-22 → 06-12)

> **⚠️ DIRECTIONAL, NOT STATISTICAL.** Per-cell N varies (large for raw Path-3, ~300–600 for the
> baseline); penny-stock returns are fat-tailed → wide CIs. Read as which-direction, not a verdict.
> **⚠️ COST WARNING (decisive here):** at a **+10% target**, a 1–2% round-trip cost is **10–20% of
> gross per trade**. Scalping is the cost-sensitive profile; everything below is **idealized** (fill
> at the entry ref, no slippage). The **Phase-2 measured-spread upgrade is decisive** for this path.

Read-only. Indicator parity TOS-confirmed (VSME/CAST/BYAH, 06-12). Universe = the **236 symbol-days**
that produced Path 1/2 signals (apples-to-apples). `analysis/path3_backtest.py`. Headline metric =
**P(reach +10% before the stop is hit)**, the operator's win-rate question.

## 1. Headline — P(reach +10% before stop), worst..best (ambiguous bounded)

| Entry | stop 3% | stop 5% | stop 10% | t→+10% p50 | n |
|---|---|---|---|---|---|
| **Path 1/2 baseline** | 0.21–0.23 | **0.30–0.31** | **0.41** | **64 min** | 619 |
| — MACD Cross | 0.23–0.24 | **0.31–0.32** | 0.39 | 53 min | 320 |
| — VWAP Breakout | 0.19–0.21 | 0.30 | 0.43 | 51 min | 299 |
| **P3 B (touch), floored** | 0.23 | 0.29–0.30 | 0.39 | 84 min | 1136 |
| P3 A (confirmed), floored | 0.20–0.21 | 0.28 | 0.38 | 85 min | 1098 |
| P3 D (continuation), floored | 0.21–0.22 | 0.27–0.28 | 0.39 | 84 min | 998 |
| P3 C (proximity), floored | 0.19–0.21 | 0.27–0.28 | 0.34–0.35 | 77–87 min | 425–799 |
| P3 (all variants), **raw** | 0.16–0.19 | 0.21–0.25 | 0.27–0.32 | 92–109 min | large |

## 2. What the evidence says

1. **Path 3 does NOT beat the existing Path 1/2 for the +10% scalp.** At every stop, the current
   MACD/VWAP signals match or exceed the best ATR-flip variant (stop5: P1/2 **0.30–0.31** vs best P3
   **0.29–0.30**; stop10: **0.41** vs 0.39) — **and they hit +10% faster** (~64 min vs ~84 min). The
   ATR flip is not an improvement on this objective. **MACD Cross is the single best cell.**
2. **Best Path-3 form = B (intrabar touch) + liquidity floor.** Entering on the touch of the known
   trail level beats waiting for the close-confirm (A); D and C are worse.
3. **The liquidity floor (vol>5000) is essential for Path 3.** Raw P3 ≈ 0.21–0.25 (stop5) → floored
   ≈ 0.27–0.30. The raw signals fire heavily on illiquid junk bars that drag the win-rate. *(Plan's
   "the data will say so loudly" — it did. Floor is mandatory if Path 3 is ever used.)*
4. **The anticipation gamble (C) does not pay — quantified (conversion table):**

   | Proximity | approaches | crossed ≤1 bar | ≤3 bars | ≤5 bars | rejected |
   |---|---|---|---|---|---|
   | C 0.5% | 1392 | 23% | 43% | **53%** | 47% |
   | C 1% | 1981 | 17% | 35% | 45% | 55% |
   | C 2% | 2388 | 10% | 25% | **35%** | **65%** |

   Buying "about to cross" buys a coin-flip-or-worse on the cross actually happening (wider band →
   more signals but worse conversion), and the scalp P does not improve for it. C is rejected.
5. **+10% is NOT a fast scalp on this universe** — median time-to-target is **51–87 min**. At a +10%
   target this is a momentum *swing*, not a seconds/minutes scalp.

## 3. The cost kill (the honest bottom line)

At the operator's **stop 5%**, the best cells (P1/2, B-floored) sit at P(+10%)≈0.30, P(−5%)≈0.60,
censored≈0.07–0.16. Rough **gross** expectancy ≈ `0.30×(+10) + 0.60×(−5) ≈ +3 − 3 ≈ ~0%` —
**breakeven before costs.** At stop 10%: `0.41×10 − ~0.5×10 ≈ −1%`. **After a 1–2% round-trip cost,
every variant — including Path 1/2 — is net negative for the +10% scalp.** This is the kill-risk the
plan flagged up front. **Whether any of this is viable hinges entirely on the Phase-2 measured
spread** (could be better or far worse than the assumed 1–2% for these illiquid names).

## 4. Scalp-lens caveat (important for the decision)

This lens caps the upside at +10% — so it **does not capture the trailing-flip's native edge**:
riding a runner with a *trailing exit* (the MFE tail, where P3 B-floored shows MFE p50 ≈ 9%). The
"ride the big move, eat many small whips" character is a **different objective** than fixed +10%
scalping and is not measured here. If the interest is trend-capture (not scalping), a trailing-exit
backtest is the right next study — this one answers the scalp question as asked.

## 5. Phase 2 decision-gate input

- **As a +10% scalp:** the evidence says Path 3 is **not an improvement** over Path 1/2, and the
  whole family is breakeven-gross / cost-negative. Recommend **do not ship Path 3 as a scalp entry**
  on this evidence.
- **If pursued anyway:** B + liquidity-floor is the only defensible form; C/anticipation is out.
- **Open door:** Path 3 as a **trend/trailing-exit** system (not scalp) is untested here and is the
  ATR flip's natural use — a separate study if the objective shifts.
- **Decisive missing input:** the Phase-2 measured spread. No go-live on this path until it's known.

## 6. Methodology / limitations

- Entry refs: A/C = signal-bar close, B = the trail level touched, D = next-bar close. Forward bars
  = strictly after the entry bar, scored to ET-session close (no overnight). Ambiguous (one candle
  spans both) bounded, never point-estimated (negligible here: ≤6 per cell).
- Censored = neither +10% nor the stop hit by session close (reported per cell).
- Idealized fills (no slippage/partials); these are hypothetical Path-3 signals (never traded) and
  the real (OMS-rejected) Path 1/2 signals. Indicator seeding (sma5/first) shown immaterial.
