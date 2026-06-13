# Exit-Ladder Re-Score — realized P&L per entry path (2026-05-22 → 06-12)

> **⚠️ MODELS THE OLD-BOT EXIT LADDER applied to these entries — NOT what schwab_1m_v2 does today.**
> v2 runs **no** managed exits (see `docs/oms-exit-logic-reference.md` §scope + the TOP open item).
> A positive result here = *"these entries + this ladder would pay"*, **NOT** "v2 is profitable."
> **⚠️ DIRECTIONAL, NOT STATISTICAL** (per-cell N small; fat tails). **⚠️ IDEALIZED FILLS** — and
> partials mean **2–3 exit fills per trade = a larger cost surface** than the 1-fill scalp; the
> **Phase-2 measured spread is decisive** (could erase the edge). **Both-hit ambiguity** (a scale
> tier + the stop/floor in one candle) is **bounded** (favorable-first vs adverse-first), never a
> point estimate; ticks resolve it in Phase 2.

Read-only. Ladder replayed exactly per `docs/oms-exit-logic-reference.md`: scale (+2%→50%, +4%-after
→25% of remainder, fast +4%→75%), floor ratchet (peak: 1%→BE, 2%→+0.5%, 3%→+1.5%, 4%+→trail
peak−1.5%), hard stop −1.5% fixed, macd-cross-below tier exit on the remainder (bar close),
precedence hard>floor>scale>tier, no EOD flat. Universe = 236 symbol-days. `analysis/exit_ladder_
rescore.py`. **v1 limitation:** the stoch tier-exit leg is not modeled (macd-cross-below is).

## Headline — realized % per trade under the ladder (exp% = worst..best, ambiguous-bounded)

| Entry | exp % / trade | median | win% | avg win / loss | exit mix (floor / stop / macd) |
|---|---|---|---|---|---|
| **P1/2 — MACD Cross** | **+2.01 .. +2.56** | −0.13 | 36% | **+8.14 / −1.00** | 183 / 112 / 24 |
| P1/2 (both) | +1.03 .. +1.62 | −0.13 | 37% | +5.32 / −0.99 | 360 / 213 / 45 |
| P1/2 — VWAP Breakout | −0.02 .. +0.61 | −0.13 | 38% | +2.43 / −0.98 | 177 / 101 / 21 |
| **P3 B (touch), floored** | **+0.58 .. +0.66** | 0.00 | **44%** | +2.57 / −0.92 | 638 / 332 / 161 |
| P3 D / A / C, floored | ~0 .. +0.19 | 0.00 | 34–36% | ~+1.8–2.2 / ~−1.0 | floor-heavy |
| P3 raw (all) | −0.11 .. +0.38 | ~0 / neg | 30–40% | ~+1.8–2.3 / ~−0.9 | junk-bar drag |

## What the exit ladder reveals (it inverts the +10% verdict)

1. **The +10% scalp lens was the wrong test — and it mattered.** Under the *real* ladder, **MACD
   Cross is solidly positive: ≈ +2.0–2.6% per trade idealized.** The +10% lens called everything
   "breakeven / cost-negative" because it required a +10% move that rarely happens and ignored the
   +2–4% partials the system actually harvests + the tight loss cap. **The ladder is the system's
   edge mechanism, not the entry alone.**
2. **The engine is win/loss asymmetry, not win rate.** Win rate is only **~36–44%** (most trades
   lose), but **avg loss ≈ −1%** (floor goes to breakeven after +1% peak; hard stop −1.5%) while
   **avg win ≈ +2 to +8%** (scale ladder + trailing floor let runners pay). MACD Cross: +8.1 win vs
   −1.0 loss × 36% → ≈ +2.5% expectancy. Classic momentum profile — small frequent losses, occasional
   big wins — and it works here.
3. **MACD Cross ≫ VWAP Breakout ≫ all Path-3 variants.** VWAP is ~breakeven; **Path 3's best (B
   floored) ≈ +0.6%** — positive, but far below MACD Cross. **Path 3 is STILL not an improvement on
   the existing Path 1/2** under the real ladder (consistent with the +10% study) — but now both look
   far better in absolute terms.
4. **Within Path 3, B (intrabar touch) is best** (highest win 44%, best exp); A/C/D worse; **liquidity
   floor essential** (raw is breakeven-to-negative — junk-bar drag, as flagged).
5. **The floor does the heavy lifting:** ~55–60% of trades exit via the breakeven-ratchet floor,
   ~30–35% via the −1.5% hard stop, ~5–15% via macd-cross-below, ~0 session-end. The ladder design
   (cap losses, ratchet gains) is validated by the exit mix.
6. **Ambiguity is material (~30% of trades):** e.g. MACD Cross +2.01..+2.56 = a **0.55% bounded
   spread** on a ~+2.5% expectancy. The point estimate is genuinely uncertain by ±~0.3% until ticks
   resolve which hit first (Phase 2).

## The cost caveat (now larger)

Partials mean **2–3 exit fills per trade** (scale at +2%, +4%, then floor/stop close), each paying
spread/slippage — a **bigger cost surface** than the single-fill scalp. On MACD Cross's ~+2.5%
idealized expectancy, a ~1% per-side spread across 2–3 fills could **erase much of it**. For these
illiquid pennies the **Phase-2 measured spread is decisive** — it determines whether the idealized
+2.5% survives. Do NOT treat these idealized numbers as a track record.

## Decision input

- **The real ladder makes the existing MACD-Cross entries look viable** (≈+2.5% idealized,
  asymmetric) — a very different conclusion than the +10% scalp lens. **The forward test (now v2
  fills) under a REAL exit ladder is the decisive next measurement** — which loops back to the TOP
  open item: **v2 must actually run an exit ladder** for any of this to be real.
- **Path 3 is not an improvement** over Path 1/2 on either lens; B+floor is its only defensible form.
- **Decisive unknowns:** (a) the Phase-2 measured spread (cost), (b) wiring real exits into v2.
