# ORB Opening-Range Breakout — Entry thesis-check + EXIT-rule research

> **Status: RESEARCH / thesis-check. NOT built, NOT live.** A proposed new entry path (P6 "OPEN")
> plus a multi-day exit-rule sweep. Needs more days before any of this becomes a config. Companion to
> the one-page proposal `orb-opening-path-spec.md`. Run/owned read-only; live ATR/execution untouched.

Backtest engine: [`scripts/orb_exit_backtest.py`](../scripts/orb_exit_backtest.py) (also on VPS at
`/tmp/orb_master.py`; bar cache `/tmp/orb_bars.pkl`). Bar-close backtest on historical 1-min data — fine
for *selecting* a rule; the live version still needs the TIMESALE/intrabar execution layer.

---

## 1. The proposal (entry)

Opening-range breakout on the qualified small-cap scanner names, 09:25–10:30 ET:
- **OR** = high/low of the first 5 one-minute bars (09:30–09:34).
- **Entry (long)** on the first bar after 09:34 that **closes** > OR_high **and** volume ≥ 1.5× OR_avg
  **and** close > VWAP **and** close > EMA9; **skip** if OR_width% > 12% (chop) or < 2% (too tight);
  cutoff 10:30; one trade/side/session. Entry = breakout-bar close.
- This BASE entry was validated on 06-18 (4/4: took CRVO/ATPC, skipped CAST/WKSP) and is **held fixed**
  for the exit research so exit differences are clean.

## 2. ⚠️ Decisive data finding — use REST, not the stored bars

The bot's stored `schwab_1m_v2` bars are **watchlist-gated**: a symbol only has bars while it was on the
v2 watchlist. The day's *winners are promoted late* (CRVO first stored bar 09:35, ATPC 09:51 — at/after
their breakouts), so their 09:30–09:34 opening ranges **do not exist** in stored bars. The faders were on
the watchlist from the open, so they're fully covered → a stored-bar backtest is **biased against the
winners**. Fix: pull 1-min bars from **Schwab REST pricehistory** (`needExtendedHoursData=true`), which
serves the full opening range for any symbol regardless of watchlist. REST validated exactly against the
operator's TradingView/Pine reference on 06-18 (CRVO OR_low 4.610, breakout 5.330).

**Structural implication for going live:** for ORB to work, the **scanner must surface candidates before
09:30** — today the bot wasn't watching CRVO until it had already run, so even a perfect rule couldn't
have traded it from the live feed.

## 3. Method (exit sweep)

- **Universe:** all names the bot engaged (≥2 intents) per day over the last 7 trading days
  (06-10…06-18), reconstructed via REST → 126 symbol-days, 98 with full OR coverage, **35 entries**.
- **Entry fixed** at BASE; only the **exit** varies (clean isolation; one variable at a time).
- **RGNT (06-15) excluded** from every number via an exception list — it was a +209%-MFE freak that all
  loose exits held (~+158%), distorting totals. (RGNT's 06-11 entry is a separate, legitimate trade, kept.)
- **Metrics:** Win%, Avg/Total return, **Median Capture (Ret÷MFE)**, **Avg give-back (MFE−Ret)**.
  Judge on win% + median capture + give-back (robust); **total return is monster-driven, least robust.**

## 4. MASTER EXIT COMPARISON (RGNT-06-15 excluded, 35 trades, same basis)

| Exit | Win% | Avg% | Total% | **Med Cap** | **Give-back** | CRVO | ATPC | Total (RGNT *in*) |
|---|---|---|---|---|---|---|---|---|
| **TRAIL-3%** | 54 | 3.5 | 124 | **0.41** | **4.1** | +21%(.84) | +19%(.73) | new |
| **TRAIL-8%** | **63** | 5.7 | 200 | 0.36 | 8.3 | +15%(.60) | +17%(.62) | new |
| D — give-back | 57 | 1.5 | 54 | 0.33 | 4.6 | +6%(.22) | +11%(.42) | 162 |
| F — ladder | 57 | 2.5 | 88 | 0.26 | 10.0 | +6%(.23) | +5%(.18) | 143 |
| TRAIL-5% | 49 | 3.5 | 124 | 0.23 | 6.4 | +19%(.75) | +19%(.73) | new |
| **COMBO (T8 OR 2×EMA9)** | 54 | 5.1 | 179 | 0.22 | 7.6 | +15%(.60) | +17%(.62) | new |
| E2 — 2of3 | 54 | 3.7 | 128 | 0.15 | 8.8 | +2%(.10) | +4%(.16) | 236 |
| B — VWAP | 49 | 5.6 | 195 | 0.13 | 12.2 | +6%(.22) | +11%(.42) | 353 |
| C — 2×EMA9 | 46 | 7.6 | 265 | 0.04 | 12.5 | +20%(.52) | +17%(.44) | 422 |
| A — EMA9 | 43 | 3.2 | 111 | −0.02 | 10.4 | +2%(.10) | +4%(.16) | 270 |
| B — EMA20 | 43 | 5.3 | 184 | −0.04 | 15.2 | +20%(.52) | +8%(.21) | 335 |
| E3 — 3of3 | 40 | 8.9 | 312 | −0.13 | 16.5 | +2%(.10) | +62%(.72) | 470 |

## 5. Verdict

1. **The operator's trailing-% hard stop BEATS the EMA exits on the robust metrics, decisively.**
   TRAIL-8% wins win% (63% vs C 46%); TRAIL-3% wins median capture (0.41 vs C 0.04); both win give-back
   (4–8% vs 12.5%). C's *only* win is total return (265% vs 200%), which is monster-driven and fragile.
2. **TRAIL-8% is the best all-rounder** — the only exit top-tier on both axes (#1 win%, #2 median
   capture, respectable #3 total). **TRAIL-3%** maximises capture/consistency but **shakes out of slow
   grinders** (stopped CCTG at −3% before its +64% run). **TRAIL-5% is no-man's-land** (worst of the three).
3. **The COMBO (TRAIL-8% OR 2×EMA9, first to fire) does NOT beat pure TRAIL-8%.** It tracks TRAIL-8%
   (the trailing stop fires first nearly always), and where the 2×EMA9 leg binds it exits *earlier* —
   slightly lower give-back (7.6 vs 8.3) but **worse** win% (54 vs 63) and capture (0.22 vs 0.36).
   **Logical reason:** "whichever fires first" (OR) can only make exits *earlier, never later*, so it
   **cannot** add C's monster-holding (which comes from C being a *later* exit on clean trends). To get
   "both," you'd need different logic (trailing as a floor while *holding to a target/EMA on strong
   trends*), not first-to-fire.
4. **The earlier multi-layer "2-of-3" (E2) was a misread of the operator's idea and underperforms** —
   its three ingredients (swing-break / volume-dry / red-bar) are correlated, so it fires about as early
   as EMA9 on the runners. Not recommended.

**Leading candidate: TRAIL-8%** (or TRAIL-3% if weighting capture over catching sustained grinders).

## 6. Per-runner (which exit held each; ret% / capture)

| Runner | Best trailing | C (2×EMA9) | A (EMA9) | E3 (loosest) | COMBO | Read |
|---|---|---|---|---|---|---|
| **CRVO** 06-18 | T3% +21%(.84) | +20%(.52) | +2%(.10) | +2%(.10) | +15%(.60) | trailing wins the choppy pop |
| **ATPC** 06-18 | T3/5% +19%(.73) | +17%(.44) | +4%(.16) | **+62%(.72)** | +17%(.62) | sustained → only E3 caught the extended run |
| **CCTG** 06-16 | T8% +50%(.78); **T3% −3%** | +65%(.60) | +69%(.64) | +23%(.21) | +50%(.78) | grinder → 3% shook out, 8% held |
| **CAST** 06-12 | T8% +15% | +7% | +3% | **+159%(1.00)** | +15% | rare sustained monster → only loosest caught it |

**The irreducible tradeoff:** tight = consistent + locks the typical move; loose = rare monster capture +
bleeds everything else. No single exit dominates; the right one depends on whether a runner *sustains*.

## 7. Guardrails
- RGNT-06-15 excluded from every number (n=35). Totals still monster-driven (E3/C lean on
  CCTG/MTEN/GLXG/CAST-06-12/ATPC-extended) → trust win% + median capture + give-back.
- Trailing is a **hard intrabar stop** → exposed to gap-through slippage (cf. the CDT −3.7% incident);
  fills modeled at the stop (open on a gap-down), optimistic on thin books. EMA exits act on close and
  dodge that — the trailing edge could erode somewhat live.
- 35 trades / 7 days = **leading candidate, not a verdict.** Exit timing is sensitive to EMA precision
  (mine vs Pine) — spot-check before committing. Live needs the TIMESALE/intrabar layer.
- One parameter at a time; don't co-optimize entry + exit.

## 8. Next steps
1. Extend to ~20–30 days to de-monster the sample and confirm TRAIL-8% holds.
2. Spot-check TRAIL-8%/C exits against the operator's TradingView EMA.
3. Confirm the scanner can promote candidates **pre-09:30** (the live prerequisite).
4. If chasing "discipline + monster-holding," design a *hold-to-target-on-strong-trend* rule (trailing
   floor + ride), not the first-to-fire OR — which structurally can't hold longer.
