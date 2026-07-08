# ORB tick-driven entry + 2% trail — DESIGN (design-first; review before any build)

**Status:** ✅ **APPROVED + LOCKED 2026-07-08 — no further design changes.** Build split: (1) shadow/paper
harness = BUILD NOW (risk-free, no real orders); (2) live orb_app.py tick-entry + trail_pct flip = HELD
until the forward-accrual shows the intrabar-2% median holding. qty-1 fill-speed test only after BOTH hold.
**Author:** research session 2026-07-08. **Evidence:** the 🎚️ ORB Decision-2 report (40→39 name-day
confirmed-window-gated sweep) + the BJDX #4 mechanism trace.

## 1. Objective
Turn the ORB exit/entry from its current clearly-losing shape into the backtest's best config —
**tick-driven entry + a 2% trailing stop, gated to high-ATR (volatile/grinding) names** — and prove
the live fill is fast enough (~3s) before any real size, because the edge is latency-sensitive.

## 2. Evidence (what the studies actually showed)
- **Best config = "intrabar 2%"**: median **+0.25/nd**, win **55%**, robust to drop-top-3 (median
  +0.275), beats the live-equivalent on **24/31 (77%)**. Survives the confirmed-window gate (dropped
  only 1 name-day). This is the first robustly-positive central tendency in any of the decision studies.
- **Clusters onto high-ATR names**: volatile median +0.50 (win 70%), grinding +0.30 (win 64%) — both
  drop-top-3-robust; **slow names lose** (median ~0, win 30%). → gate to high-ATR.
- **Part 1 (BJDX #4)**: the −7.3% give-back was **exit-side fill latency, not trail lag** — the trail
  triggered correctly at 1.73, then the market-sell filled 3s later at 1.65 as the bid went vertical.
  → the whole edge assumes a genuinely ~3s (or faster) exit fill.

## 3. Current live ORB architecture (verified in code, 2026-07-08)
- **Entry: BAR-CLOSE.** `orb_app.py` running-high `_on_bar` fires only when `OrbTickAggregator.add_tick`
  returns a *closed* 1-min bar; the break is evaluated at bar close. (Confirmed: intrabar entry is a
  NEW requirement, per the 07-06 study.)
- **Exit: ALREADY TICK-DRIVEN.** OMS `_handle_quote_tick_event → _evaluate_hard_stop_market_event`
  runs on every quote tick: `_ratchet_trailing_stop` (identical math to the backtest
  `_ratcheted_trailing_stop`) then triggers a **market close** when `bid/last <= stop_price`.
  `trail_pct` currently **3%**; plus a native Webull `STOP_LOSS` backup (belt-and-suspenders, F2 mirror).
- **So the live ORB ≈ the backtest's "bar-close 3%" config** (bar-close entry + 3% tick trail) =
  **median −0.20, win 16%** — i.e. the losing shape it's parked at.

## 4. The change (and why it is entry + exit, not exit alone)
The winning delta over the live shape decomposes into **two synergistic changes**:
1. **Tick-driven ENTRY** (bar-close → break-tick, with a *continuous* running-high so a stale
   bar-lagged level can't be re-crossed — the CELZ-phantom fix): median −0.20 → 0.00.
2. **2% trail** (exit `trail_pct` 3% → 2%): median 0.00 → +0.25.
   **⚠ The 2% trail ALONE, on the current bar-close entry, is WORSE (−0.25).** The benefit only
   appears WITH the tick entry. So this cannot ship as an exit-only tweak — both pieces are required.
Both **gated to high-ATR names** (§5). The exit is already tick-driven, so no new exit *evaluation*
machinery is needed — only `trail_pct` (per-name) and the entry path change.

## 5. The high-ATR gate (hard constraint #1)
Slow names whipsaw on a 2% trail (median −0.03/loss). Gate the new config to volatile/grinding:
- **Signal:** period-5 ATR% of price over the ORB-window bars so far, computed live in `orb_app.py`
  at the entry evaluation (the bot already builds these bars). Grinding-vs-volatile need NOT be
  distinguished — both win; only slow is excluded, so the gate is a single ATR% threshold.
- **Threshold (starting):** ATR5% ≥ ~4.3% (the sample's slow/active tertile boundary). **This is a
  small-sample number — ship it as a monitored parameter, not a constant, and recalibrate as the
  forward-accrual sample grows (the daily cron is now feeding it).**
- **Early-window resolution (R&D 2026-07-08, closes the open question):** the causal period-5 ATR needs
  ~9 ORB-window bars → not ready until ~**09:34**. R&D quantified the cost and rejected every seed:
  - **The 09:30–09:34 prize is large and concentrated on flood days:** ~**87% of the hot-day high-ATR
    edge** (+12.70 of +14.63 over the sample) is in those first 4 minutes. Fail-closed-until-09:34
    guts the strategy on exactly the days that matter.
  - **Pre-market / prior-day / shorter-period seeds REJECTED:** premkt@09:30 agrees with RTH@09:34 only
    **46%** (11/24), and every disagreement is one-directional (premkt says *slow* when RTH says
    *high-ATR*) — pre-open volatility is systematically ~2–3× lower because **the 09:30 open is a regime
    change you cannot measure from quiet pre-open data**; a fixed threshold would gate out the hot
    movers (worst outcome). Only 24/40 name-days even have pre-market data.
  - **RESOLUTION — ungate the first ~4 min, then gate from 09:34.** Slow names rarely break out that
    early (1 slow early break across all hot days), so the running-high break is itself the filter.
    Ungating 09:30–09:34 recovers ~**89%** of the flood-day early edge (+11.30) at a small **−1.40**
    slow-whipsaw cost. Implemented as its own flag-gated config `orb_tick_entry_gate_after_minutes`
    (0 = gate from open; 4.0 = the validated recovery). **The causal ATR gate applies unchanged from
    09:34 onward.**
- Below threshold (slow), from 09:34 on → **keep the current behavior** (bar-close entry, 3% trail) or
  don't trade — do NOT apply the 2% tick config.

## 6. Fast-fill requirement (hard constraint #2)
The edge assumes the exit fills ~3s after the trigger (the backtest's honest Webull latency; Part 1
showed a slower fill erodes it on fast fades). The live exit already submits a **market close** on the
tick trigger — that IS the ~3s path the backtest models. So the requirement is to **verify, not build**:
- **Measure the live decision→fill latency on real ORB exits** (submit ts → fill ts), especially on
  thin names, and confirm it's ~3s and not degrading.
- **Future option (NOT in v1):** a pre-staged resting bracket (native OTOCO stop) fills at exchange
  speed, independent of OMS reaction — the `orb-resting-bracket-entry-design.md` idea. This would beat
  the ~3s market close on fast fades, but adds combo-order complexity and is a separate design. v1
  keeps the existing market-close exit and simply verifies its speed.

## 7. Paper / forward validation (hard constraint #3 — before any real size)
1. **Forward-accrual (LIVE now):** the daily gated sweep grows the sample ~+4/day; watch the running
   intrabar-2% median/win/drop-top-3 in `accrual_log.txt`. Ship only if the positive median holds as
   the sample climbs (guards against the CLRO/UPC-style dissolve).
2. **Paper/shadow the new config:** run tick-entry + 2% trail + ATR gate in shadow (no real orders) on
   live data for ≥1–2 weeks; compare shadow fills to the backtest's modeled fills.
3. **Live fill-SPEED test at qty-1** (the operator's explicit gate): a handful of real qty-1 tick-entry
   exits, measuring submit→fill latency vs the ~3s assumption, BEFORE any size. If live fills are
   materially slower than 3s on the names that matter, the edge is not bankable and v1 stops here.

## 8. Position sizing (research, 2026-07-08) — price two-tier vs ATR/behavior
Tested on the gated intrabar-2% config (31 classifiable name-days); P&L is linear in qty so this is
a clean rescale. Baseline flat qty5. All are **leverage on a modest median edge — the median is the
signal, and bigger size = bigger swings.**

| rule | avg qty | total $ | median | win% | drop-top |
|---|--:|--:|--:|--:|--:|
| flat qty5 (baseline) | 5.0 | +7.2 | +0.25 | 55% | −3.1 |
| PRICE two-tier (>$5→10 / <$5→20) | 17.4 | +36.1 | +0.70 | 55% | +15.5 |
| ATR two-tier (slow→10 / high-ATR→20) | 16.8 | +45.8 | +0.70 | 55% | +4.6 |
| **ATR edge-only (slow EXCLUDED / high-ATR→20)** | 20.0 | **+62.7** | **+1.40** | **67%** | **+21.5** |

- **Price two-tier helps** vs flat, and the sub-$5 (qty20) tier is genuinely NET-POSITIVE
  (median/share +0.06, win 61%, drop-top +24.3) — because in the ORB universe cheap ≈ high-ATR.
  The cheap 2× names are a healthy mix (volatile/grinding +, slow ~flat), NOT slow losers — with one
  exception, **DSY** (slow, $4.27) amplified to −6.20 at qty20.
- **Swapping the sizing AXIS (price → ATR) is a WASH:** same median (+0.70) and win (55%). ATR fixes
  the DSY over-sizing (slow→qty10, halved; or excluded) but **creates an SDOT over-sizing** — SDOT is
  high-ATR *volatile* but a loser, and ATR amplifies it to qty20 (−22.6 vs −11.3 under price). ATR's
  higher total is PLSM-driven (drop-top +4.6 < price's +15.5).
- **The real lever is EXCLUDING slow, not the axis.** Per-share edge: volatile +0.10/win70,
  grinding +0.06/win64, **slow −0.005/win30**. Dropping slow entirely (edge-only) dominates every
  metric and is drop-one-robust (+21.5). This just **reinforces the §5 ATR gate** (gate out slow, size
  high-ATR up) — it is NOT an argument for a price rule.
- **Recommendation:** v1 sizing = **gate out slow (§5) + size the high-ATR names up** (e.g. ~2× the
  current qty5). Price-tiering within the high-ATR set is optional and marginal (≈ATR-tiering). Size up
  only as far as the 4×-drawdown on a bad high-ATR name (SDOT: −22.6 @ qty20) is tolerable. Re-check
  the cheap/high-ATR-tier positivity on the forward-accrual before committing size.

## 9. Risks & review points
- **Scope is entry + exit**, not an exit tweak — the entry-side change touches `orb_app.py`
  (design-first + real-emit test + after-close restart, per the ORB discipline).
- **Magnitudes are modest** ($0.05–0.13/share @ qty5) and the total leans on big movers; the median is
  the edge. Slippage/fees in the live path could erase a modest median — the fill-speed test is decisive.
- **Threshold fragility:** the ATR gate is small-sample; must be monitored and recalibrated off the
  accrual. A wrong threshold re-admits slow names (which lose).
- **Interaction with the parked ORB entry:** this improves how a running-high break is entered/exited;
  it does NOT by itself validate the running-high *entry thesis* (still weak). It makes ORB the
  least-bad it can be, not a proven winner — frame expectations accordingly.
- **Native STOP backup:** the 2% trail change must keep the F2 native-stop mirror + `−1.5%`/hard-stop
  safety net coherent (don't widen risk).

## 10. Decision for review
Approve/modify: (a) proceed to build tick-entry + 2%-trail + ATR-gate as one change (design-first per
piece), (b) the ATR gate threshold + early-window fallback, (c) v1 keeps the market-close exit (verify
speed) vs jumping to a resting bracket, (d) the paper→qty-1-speed-test sequence before real size,
(e) sizing (§8): gate out slow + size high-ATR up (~2× qty5); price-tiering is optional/marginal.
**No build starts until this is reviewed and the forward-accrual shows the median holding.**
