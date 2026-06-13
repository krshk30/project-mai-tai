# Weekly P3-B Ledger — June 8–12 2026 (qty 10/entry)

> **ANECDOTE, NOT STATISTICS** — 5 days, top-5 movers/day; directional, not a verdict.
> **IDEALIZED fills** (no slippage/spread). **P3-B trades the MOST → most fills → most cost exposure**; the Phase-2 measured spread is decisive and could flip P3-B's sign.
> **`fav$ / adv$`** = both-hit ambiguity bound (favorable-first vs adverse-first), never a point estimate; ticks resolve in Phase 2.
> Models the OLD-bot exit ladder applied to these entries — NOT what v2 runs today (v2 has no exits — TOP open item).
> **Selection (reproducible):** per day, top-5 by intraday range % from the v2-scanner universe (≥30 bars after 11:00 UTC). **Completeness gate:** ≥330/390 RTH minutes.

## Per-day summary

### 2026-06-08 — RTH 390/390 ✅ complete
Movers (range%): INHD(3893%), BYAH(364%), CHAI(301%), BGI(265%), CCTG(208%)

| Path | Entries | Wins | Losses | Ambig | P&L fav $ | P&L adv $ |
|---|---|---|---|---|---|---|
| P3-B(touch,vol>5k) | 13 | 10 | 3 | 4 | 17.5 | 13.99 |
| P1-MACD Cross | 5 | 3 | 2 | 2 | 6.26 | 1.8 |
| P2-VWAP Breakout | 6 | 5 | 1 | 4 | 5.64 | -0.3 |

### 2026-06-09 — RTH 390/390 ✅ complete
Movers (range%): AZI(667%), CCTG(396%), AHMA(315%), CHAI(306%), DAIC(260%)

| Path | Entries | Wins | Losses | Ambig | P&L fav $ | P&L adv $ |
|---|---|---|---|---|---|---|
| P3-B(touch,vol>5k) | 29 | 18 | 11 | 7 | 18.54 | 13.78 |
| P1-MACD Cross | 3 | 3 | 0 | 1 | 5.54 | 4.14 |
| P2-VWAP Breakout | 5 | 5 | 0 | 5 | 5.13 | 1.39 |

### 2026-06-10 — RTH 390/390 ✅ complete
Movers (range%): DSY(469%), CIIT(300%), DAIC(180%), BATL(124%), CNET(121%)

| Path | Entries | Wins | Losses | Ambig | P&L fav $ | P&L adv $ |
|---|---|---|---|---|---|---|
| P3-B(touch,vol>5k) | 28 | 17 | 11 | 11 | 11.58 | -1.88 |
| P1-MACD Cross | 11 | 7 | 4 | 7 | 4.77 | 2.39 |
| P2-VWAP Breakout | 5 | 1 | 4 | 3 | -0.37 | -0.84 |

### 2026-06-11 — RTH 390/390 ✅ complete
Movers (range%): PPCB(240%), EDHL(212%), PPBT(209%), CCHH(194%), GLXG(190%)

| Path | Entries | Wins | Losses | Ambig | P&L fav $ | P&L adv $ |
|---|---|---|---|---|---|---|
| P3-B(touch,vol>5k) | 32 | 13 | 19 | 10 | 9.17 | 6.42 |
| P1-MACD Cross | 4 | 2 | 2 | 3 | 3.39 | -3.38 |
| P2-VWAP Breakout | 6 | 6 | 0 | 2 | 9.16 | 5.4 |

### 2026-06-12 — RTH 390/390 ✅ complete
Movers (range%): BYAH(166%), CUPR(157%), UBXG(105%), DSY(93%), CAST(87%)

| Path | Entries | Wins | Losses | Ambig | P&L fav $ | P&L adv $ |
|---|---|---|---|---|---|---|
| P3-B(touch,vol>5k) | 32 | 11 | 21 | 10 | 8.11 | 0.17 |
| P1-MACD Cross | 7 | 3 | 4 | 3 | -0.2 | -1.27 |
| P2-VWAP Breakout | 9 | 3 | 6 | 6 | 1.31 | -4.54 |

## Grand total — per path, all 5 days

| Path | Entries | Wins | Losses | Total P&L fav $ | Total P&L adv $ |
|---|---|---|---|---|---|
| P3-B(touch,vol>5k) | 134 | 69 | 65 | 64.9 | 32.48 |
| P1-MACD Cross | 30 | 18 | 12 | 19.76 | 3.67 |
| P2-VWAP Breakout | 31 | 20 | 11 | 20.88 | 1.1 |

## P3-B daily consistency (the real question)

| Day | Entries | P&L fav $ | P&L adv $ |
|---|---|---|---|
| 2026-06-08 | 13 | 17.5 | 13.99 |
| 2026-06-09 | 29 | 18.54 | 13.78 |
| 2026-06-10 | 28 | 11.58 | -1.88 |
| 2026-06-11 | 32 | 9.17 | 6.42 |
| 2026-06-12 | 32 | 8.11 | 0.17 |

**P3-B positive (fav) on 5/5 days.** Full per-fill rows in the CSV (300 rows).

## What this settles (and the catch)

**06-12 was NOT a lucky day.** P3-B is **positive (favorable) on 5/5 days** and beats P1/P2 on
**total $** (fav +$64.90 vs +$19.76 / +$20.88). The adverse (pessimistic-ambiguity) bound is also
positive overall (+$32.48) and on 4/5 days (only 06-10 dips to −$1.88). So the strong 06-12 result
holds across the week — directionally.

**The catch — P3-B wins on FREQUENCY, not per-trade quality:**

| Path | entries (5 days) | $/entry (fav) | win rate |
|---|---|---|---|
| P3-B | **134** | **$0.484** | 51% |
| P1-MACD Cross | 30 | **$0.659** | 60% |
| P2-VWAP Breakout | 31 | **$0.674** | 65% |

**Per entry, P1/P2 actually pay ~37% MORE than P3-B, with better win rates (60–65% vs P3-B's
coin-flip 51%).** P3-B's higher *total* comes purely from firing **~4.3× more often** (134 vs ~30) —
consistent with the 236-day re-score (P3-B's per-trade edge ≈ +0.6% vs MACD Cross ≈ +2.5%). So:
- **Quality:** P1-MACD / P2-VWAP > P3-B (fatter per-trade, higher win rate).
- **Volume/total $ on movers:** P3-B > P1/P2 (it just shoots more).

**Why this is decisive for the cost question (page-one warning realized):** P3-B's 134 entries × 2–3
exit fills ≈ **300+ fills** vs P1/P2's ~80 — **~4× the cost surface** on a **thinner per-trade edge
($0.48)**. A realistic per-fill spread on these sub-$3 pumps could **erase P3-B's edge while P1/P2
(fewer, fatter trades) survive.** The Phase-2 measured spread isn't just decisive for the sign — it
specifically threatens the high-frequency path most.

**Also note:** the $ figures are inflated by extreme pumps (INHD +3893%, AZI +667% range) where the
scale ladder + trailing floor harvest big $ from a few names — anecdote-scale, dominated by tails.

**Bottom line:** P3-B is a *real, consistent, but THIN and high-frequency* edge that beats P1/P2 on
gross $ yet loses on per-trade quality and is the most exposed to execution cost. **The verdict
hinges on the Phase-2 measured spread** — until then this is directional, not a green light.
