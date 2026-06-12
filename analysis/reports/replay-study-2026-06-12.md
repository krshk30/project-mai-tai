# Replay Study Phase 1 — schwab_1m_v2 signals (2026-05-22 → 2026-06-12)

> **⚠️ DIRECTIONAL, NOT STATISTICAL.** N=624 signals over 14 days, but every *slice*
> (per-cell × per-path × per-session) is small and the penny-stock return distribution is
> fat-tailed — so confidence intervals are wide. Read this as "which direction / which path /
> which policy looks promising," **not** a validated edge. Statistical claims wait for the
> forward test (now that #284 makes v2 actually fill) to accumulate a real sample.

Read-only, bar-only. Scripts: `analysis/replay_study.py`. Raw: `replay-study-2026-06-12.json`.
Entry = the signal bar **close** (== `entry_price` == the new `reference_price`); forward bars
are strictly after the signal bar. Per the merged design (#285): **MFE/MAE distributions lead**,
the target/stop grid is secondary, both-hit candles are bounded ranges (never point estimates).

**Coverage:** 624 signals — **617 vendor (Schwab pricehistory, byte-exact)**, 7 v2-stored
fallback, **0 uncovered**. 325 MACD Cross / 299 VWAP Breakout; 454 RTH / 170 premarket.

---

## 1. MFE / MAE distributions — the lead metric (all 624 signals)

Max favorable / adverse excursion from entry, by horizon (percentiles, %):

| Horizon | MFE p50 | p75 | p90 | MAE p50 | p75 | p90 |
|---|---|---|---|---|---|---|
| 5 min | 1.85 | 5.47 | 12.59 | 2.37 | 5.03 | 8.75 |
| 15 min | 3.11 | 8.97 | 24.06 | 3.98 | 7.74 | 12.92 |
| 30 min | 4.14 | 10.98 | 30.49 | 5.04 | 10.19 | 16.39 |
| 60 min | 5.65 | 15.40 | 37.99 | 6.83 | 13.23 | 21.55 |

**Reading it:**
- **The median signal goes adverse slightly MORE than favorable** (MAE p50 ≳ MFE p50 at every
  horizon). These names whip both ways; there is no free median edge — capturing it requires
  getting out before the adverse leg.
- **…but the right tail is fat:** p90 MFE reaches **38% at 60 min** (vs p90 MAE 21%). The
  opportunity is in the tail, not the median — which is exactly why a tight stop / generous target
  shape matters (Section 2).
- **By path:** similar magnitudes; VWAP Breakout has marginally higher MFE *and* MAE (more volatile
  entries), MACD Cross marginally tighter MAE (better median risk). 60m MFE p50: MACD 5.63 / VWAP
  5.66; 60m MAE p50: MACD 6.78 / VWAP 6.86.
- **By session:** premarket is higher-variance both ways — 60m MFE p50 **7.38% (premarket) vs 5.34%
  (RTH)**, but premarket early MAE is also larger (5m MAE p50 3.09 vs 2.12). Consistent with
  premarket being the high-energy window for this universe.

---

## 2. Target/stop grid — secondary, and the honesty layer bites here

4 cells (stop × target), expectancy % per trade **conditional on resolving to target/stop within
60 min** (NO_HIT excluded — see caveat). Three fill assumptions; ambiguous candles shown as a
bounded range (only **3 ambiguous total**, all in one cell — bar resolution is clean).

Round-trip cost assumptions: idealized 0% · **spread 1% (ASSUMED placeholder — Phase 2 grounds it
from quote ticks)** · slippage 2% (spread + 2×0.5%/side, partials not modelled).

| Cell | n | idealized | spread (−1%) | slippage (−2%) |
|---|---|---|---|---|
| stop5 / target10 | 472 | −0.14 .. −0.04 | −1.14 | −2.14 |
| stop5 / target20 | 445 | **+0.51** | −0.49 | −1.49 |
| stop10 / target10 | 364 | **+0.60** | −0.40 | −1.40 |
| **stop10 / target20** | 317 | **+1.45** | **+0.45** | −0.55 |

**The finding that matters:** the idealized edge is real but **thin, and it does not survive
realistic execution cost.** Only **stop10/target20** stays positive through the (assumed) 1% spread
(+0.45%); **no cell survives the full 2% slippage haircut.** For these illiquid pennies, execution
cost is the whole ballgame — the signals have idealized *opportunity*, but realized edge depends
entirely on fills, which we do not yet have. This is precisely the gap the forward test (now that v2
fills) and Phase-2 tick-grounded slippage will close.

**By path (idealized):** **MACD Cross dominates VWAP Breakout** — MACD is positive in *every* cell
(even stop5/target10: +0.08..+0.20), VWAP is negative in the stop5 cells. Best cell: MACD
stop10/target20 **+1.65%** vs VWAP +1.22%. Directional takeaway: **prefer MACD Cross; widen stops.**

**⚠️ Grid caveat (state it plainly):** the grid expectancy is **conditional on a ±band being hit
within 60 min.** Across 624 signals only ~317–472 resolved per cell; the rest were NO_HIT (neither
band reached in 60 min) and are *excluded*, not counted as time-stop exits. This is consistent with
the MFE/MAE medians (~5–7% at 60 min) sitting below the 10–20% bands — **most signals never reach
±10–20% within an hour.** So the grid is a conditional view; the MFE/MAE distributions (all 624) are
the unbiased lead.

---

## 3. Limitations (every idealization, named)

- **Entry = signal close** (optimistic; the recorded sim-fill scope). Real entry slips.
- **Spread is an assumed 1% placeholder** — Phase 2 grounds it from captured quote ticks (#282).
- **Slippage/partials** parameterized (2% round-trip); partials not modelled — for illiquid pennies
  real slippage can be far worse.
- **NO_HIT excluded** from grid expectancy (see §2 caveat).
- **These signals were OMS-rejected** (no real fills); P&L here is opportunity, not realized.
- **Actual OMS exit rules NOT modelled** (MACD-cross-down / stochastic / quick-stop / scaled /
  hard-stop) — deferred by design; the **forward test now measures them natively** since #284 fills.
- **3 ambiguous candles** unresolved by bars → Phase 2 tick first-hit resolves them (negligible here).

## 4. What it gates / next

- **Forward test is the real validation** — now v2 actually fills (#284), live sim P&L under the
  strategy's *actual* exits accumulates from the next signal onward. That, not this study, settles
  expectancy.
- **Phase 2 (after tick-capture activation):** resolve the 3 ambiguous candles via
  `replay_exit_from_ticks.py` + replace the assumed 1% spread with quote-tick-grounded spreads (the
  honesty layer's missing input — and the result above says spread is decisive).
- **Directional hypotheses to carry (not conclusions):** MACD Cross > VWAP Breakout; wider stops
  (10%) + asymmetric targets (20%) shape best; the median has no free edge so exit discipline / tail
  capture is where the money is; premarket is higher-variance both ways.
