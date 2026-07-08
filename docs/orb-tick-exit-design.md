# ORB tick-driven entry + 2% trail — DESIGN (design-first; review before any build)

**Status:** DESIGN ONLY. No code beyond the research branch. Review required before implementation.
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
- **Open question (review):** early-window entries (09:30–09:32) may have <5 bars for ATR5%. Fallback
  options: (a) require ≥5 ORB-window bars before the tick-entry config is eligible (miss the earliest
  breaks), or (b) seed ATR from pre-market bars. Recommend (a) for safety; quantify the missed-entry cost.
- Below threshold (slow) → **keep the current behavior** (bar-close entry, 3% trail) or don't trade —
  do NOT apply the 2% tick config.

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

## 8. Risks & review points
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

## 9. Decision for review
Approve/modify: (a) proceed to build tick-entry + 2%-trail + ATR-gate as one change (design-first per
piece), (b) the ATR gate threshold + early-window fallback, (c) v1 keeps the market-close exit (verify
speed) vs jumping to a resting bracket, (d) the paper→qty-1-speed-test sequence before real size.
**No build starts until this is reviewed and the forward-accrual shows the median holding.**
