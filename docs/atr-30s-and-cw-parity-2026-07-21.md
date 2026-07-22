# 30-second ATR exploration + CW-v2 backtest parity — 2026-07-21

> **⛔ VERDICT: the 30s idea does NOT escape the wall. Confirmed across 8 days / 279 trades: net
> negative under every exit rule. It is a bet on catching a rare monster runner (~2 of 8 days);
> between runners it bleeds.** The one thing that would change this is *selection* — predicting
> which stock runs before the trade — which remains unanswered.
>
> Kept because a large, careful body of rules was tested and must not be re-explored from scratch.
> Companion to [[project_mai_tai_bar_source_defect]] (the wrong-bars root cause found the same day).

Scripts (VPS): `/tmp/{tv30s,grid,multiday,trace,traildiff,bars30s}.py`;
`/home/trader/wt-atr-ab/atr_cw_v2_variants_schwab.py`; `scripts/{schwab_backfill,rerun_report,ledger}.py`.

---

## PART A — CW-v2 1-minute backtest PARITY (the enabling win)

Before any 30s work, the 1-min CW-v2 backtest was made to **reproduce the live bot's real trades**,
which it had never done. This is what makes any backtest here trustworthy.

**Parity gate = the operator's 5 real v2 trades on 07-21** (OMS-verified): KIDZ 11:40, GREE 12:25,
CPHI 13:25, GREE 14:08, GREE 15:14. Result after the fixes: **5 of 5 reproduced, exit reasons 5/5**,
several exit timestamps identical to the second.

Three findings from the parity work (in order):
1. **Bar source was the defect** — the CW harness (`atr_cw_v2_variants.py`) built bars from Polygon
   `list_aggs`; live v2 decides on Schwab bars. Swapped to the Schwab REST backfill
   (`atr_cw_v2_variants_schwab.py`). See [[project_mai_tai_bar_source_defect]].
2. **ATR seed was NOT the defect** — the backtest's ATR matched the bot's LOGGED production
   `trig`/`flip_level` **44 of 45** times (`traildiff.py`). Two seed hypotheses (session-start,
   7-day) were tried and both were wrong; the trail was never the issue.
3. **The arm-time window gate WAS the fix** — the backtest gated ENTRY to confirmed windows but
   ARMED on every flip; the bot can only arm on a flip it OBSERVED (symbol subscribed). Gating the
   ARM to confirmed windows removed 2 of 3 phantom trades. Proven by GREE's 10:21 flip landing in a
   75-second hole between two confirm windows.

**Residual: 1 phantom (CPHI 10:55).** Everything measurable says the bot should have armed
(subscribed + warm at 10:40, bars + ATR identical) yet it did not. The blocking gate is not in the
current logs. **To close it, add a per-bar CW-state probe** (`cw_armed`/`cw_trigger`/
`cw_entries_this_flip`/`emit_claimed`/`cooldown`, INFO-only, like `[V2-MACD-PROBE]`).

**Known residual bias, all backtests here:** signals from Schwab bars, **fills from Polygon quotes**
(the only quote capture). The live bot fills at Schwab. Smaller than the bar-source error, not zero.
Backtest floor exits book exactly +2.00% while live booked +0.8–1.9% — ~0.3pp optimistic per trade.

---

## PART B — the 30-second exploration

### Data reality (why 30s is hard)
- **Schwab REST has NO sub-minute bars** (`frequencyType=second` → HTTP 400). 1-minute is the floor.
- **No Schwab tick archive** for these v2 symbols (that archive is for other bots).
- ⇒ 30s bars can ONLY be built from **Polygon ticks** (`market_capture_trades`).
- **Calibration is POOR:** tick-built 1-min ATR flips diverge from the trusted Schwab 1-min flips
  (GREE shared 0 of 7). So exact 30s prices are NOT trustworthy. **Operator validates execution on
  his own TradingView 30s ATR chart instead** — that is the accepted footing (directional numbers).

### The indicator params (from the operator's TradingView "ATR Trail")
`lookback 14 · multiplier 3 · Trailing · FLIP ON WICK · body 100 / wick 100 · RMA smoothing`.
- **Our shared `atr_oracle` is WRONG for this chart** — it uses period 5, factor 3.5, flips on
  CLOSE, TOS-modified TR. RMA = Wilder's (matches), but period/factor/flip-source differ.
- **Key insight:** period 14 on 30s = a **7-minute** lookback — *longer/smoother* than the live
  1-min bot (5×1min = 5 min). An earlier 30s run wrongly kept 5/3.5 → 2.5-min lookback → hyper-twitchy,
  all whipsaw. The right 30s trail is smooth, not fast.
- Implemented a local TradingView-style trail (standard TR + RMA + wick flip) so the oracle stays
  untouched (`tv30s.py::atr_trail_tv`). Params are CLI knobs.

### The entry (operator's live rule, on 30s)
After an ATR BUY flip → wait **3 candles** (flip bar + 2) → trigger = highest high of those 3 →
enter intrabar when price breaks the trigger → **rule 7** (whole forming bar above the flip level) →
**scanner-gated** (entry only inside a CONFIRM→drop window; verified — GREE's out-of-window flips are
excluded). ⚠ NO volume filter applied (the live bot has ~10k; adding it would thin chop-name entries).

### The exits tested
| Exit | Rule |
|---|---|
| **hard−5 / hard−3** | ride to the trail flip; hard stop −5% / −3% |
| **floor+2 / floor+3** | arm a floor at +2%/+3%, exit on fall-back to it (else −5% before arm) |
| **f2t2** | arm +2%, then trail 2% below the running peak |
| **f3t1** | arm +3%, then trail 1% below the peak |

---

## PART C — RESULTS (all 30s, 14/3/wick, 3-candle, scanner-gated, honest quote fills)

### C1 — 3 names, 07-21 (the hand-picked mix: 1 runner, 1 slow, 1 chop)
| Exit | Total | Win% | CPHI | KIDZ | GREE |
|---|---|---|---|---|---|
| hard−5 | +77.7 | 23% | +108.5 | −25.1 | −5.7 |
| hard−3 | +78.2 | 14% | +101.9 | −18.0 | −5.7 |
| floor+2 | +47.6 | 45% | +57.7 | −9.0 | −1.1 |
| floor+3 / f2t2 / f3t1 | all negative | | | | |

- CPHI ran 1.7→16 → morning runner **2.45→3.97 (+62%)** and afternoon **9.71→14.30 (+47%)**, both
  VERIFIED on Schwab bars. The +47% is the "13.98 floor" the operator saw on his chart.
- KIDZ/GREE **never moved** — max favorable excursion +2.4% on every trade (`trace.py`). Their loss
  is not entry timing; the *stocks* chopped. No exit rides a trend that isn't there.
- **The winner for nearly every exit is the SAME trade: CPHI 11:00.** The exit barely matters;
  catching the one runner is the day.

### C2 — 10 tick-covered names, 07-21 (broader, still mover-biased)
| Exit | Total | Win% |
|---|---|---|
| hard−5 | +55.1 | 28% |
| **hard−3** | **+64.8** | 21% |
| **floor+2** | **+4.9** (breakeven) | 45% |

floor+2 fell from +47.6 (3 names) to **+4.9** (10 names) — essentially breakeven, and **entirely
CPHI** (+57.7 vs −52.8 for the other 8). hard−3's +64.8 is also entirely CPHI (+101.9).

### C3 — MULTI-DAY, 8 days, 279 trades (the confirmation) ⭐
| Day | Trades | hard−5 | hard−3 | floor+2 | Best trade |
|---|---|---|---|---|---|
| 07-10 | 30 | −71.4 | −50.2 | −22.8 | GMM +9% |
| 07-13 | 52 | −2.9 | −46.4 | −15.9 | VEEE +29% |
| 07-14 | 33 | −83.9 | −61.8 | −26.1 | NXTC +6% |
| 07-15 | 31 | −26.8 | −37.5 | −27.8 | SOBR +23% |
| 07-16 | 46 | −47.6 | −43.5 | −13.1 | ATPC +11% |
| 07-17 | 9 | −19.4 | −16.7 | −9.6 | CJMB +3% |
| **07-20** | 25 | **+118.1** | **+127.5** | −11.2 | **ZYBT +144%** |
| **07-21** | 53 | +54.8 | +64.6 | +4.6 | **CPHI +76%** |
| **GRAND** | **279** | **−79.2** | **−63.9** | **−121.7** | |
| **win%** | | 22% | 18% | **50%** | |

**Every exit is net-negative over 8 days.** Only **2 of 8 days** were positive, both a single monster
runner (ZYBT +144%, CPHI +76%). The other 6 days had no runner and bled under every exit. The two
runner-days (+192) do not cover the six bleed-days (−256).

**⭐ THE floor+2 TRAP:** best win rate (50%) and the WORST total (−121.7). It caps the ZYBT/CPHI
runners small (giving up the +144%/+76% that actually pay) while still eating −5% stops on the
choppers. **High win rate, negative expectancy** — the exact reason we judge on expectancy, never win
rate. [[project_mai_tai_percentages_not_dollars]]

---

## PART D — CONCLUSIONS

1. **30s does not escape the wall.** Same shape as the 1-min work all month: runner-dependent, net
   negative between runners, can't select the runner in advance.
2. **No exit / factor / timeframe fixes it** — the problem was never the exit or the bar size.
3. **Operator instincts that WERE validated (real findings):**
   - −5% is unnecessary: **hard−3 ≈ hard−5** on total (tighter stop, less per-trade risk).
   - Ride-to-trail-flip catches the monster runners the +2% floor throws away (CPHI +47 vs +2).
   - Arming a floor above +2% (floor+3, f3t1) loses — most trades never reach +3% to arm.
   - 30s ATR must use the operator's TV params (14/3/wick), NOT the live 5/3.5 (that was the
     twitch that made the first 30s run garbage).
4. **The mover-bias** (tick-covered names only; 21 quiet names excluded on 07-21) means the true
   universe result is **worse** than these already-negative numbers.
5. **The one open, game-changing question:** can the runner be predicted BEFORE the trade
   (selection)? If yes, this becomes a real strategy at any timeframe. If no, the exploration is
   closed with a clear negative.

**Do NOT re-tune exits or timeframes on this data.** The 8-day confirmation is decisive.
