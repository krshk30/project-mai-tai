# ORB shadow/paper harness — DESIGN (piece 1 of the approved V1; risk-free, build now)

**Parent:** `orb-tick-exit-design.md` (APPROVED + LOCKED). This is the BUILD-NOW piece — a shadow that
runs the approved config on live data with **NO real orders**, so the ≥1–2-week shadow run accumulates
in parallel with the forward-accrual. Does not touch the live bot.

## Purpose (what the shadow validates that the accrual does NOT)
The forward-accrual proves the P&L median holds statistically (offline sweep). The shadow adds:
1. **CAUSAL ATR gate (the key new thing).** The backtest classified names using the FULL ORB window's
   bars (hindsight). The LIVE bot must decide at the entry tick using only bars formed SO FAR. The
   shadow computes ATR5% from the ORB-window bars up to the entry tick and gates on it — validating the
   gate is usable causally and measuring the causal-vs-hindsight entry difference (does the gate still
   admit the high-ATR movers when it can only see the first few bars?).
2. **A per-trade shadow-fill record on live data** (entry/exit ts+price, ATR5%-at-entry, gate decision,
   running-high level) — for chart-eyeballing and for the later qty-1 real-fill comparison.
3. **Streaming-logic parity** — the shadow's tick-by-tick engine vs the offline `simulate_intrabar`
   (trail=2, gated, on the same day); agreement means the streaming implementation is faithful.

## Architecture (low-risk, mirrors the accrual)
- Standalone module `backtest/orb_shadow.py` + a daily post-close cron (alongside `orb_trail_accrual`).
- Runs on that day's **live-captured** ticks/quotes (`market_capture_*`) for the ORB-qualified names —
  the captured feed IS the live data; replaying it in arrival order is faithful to a real-time consumer,
  without a long-running Redis service to babysit. (A true real-time consumer is a possible V2 upgrade.)
- **Streaming engine:** consume trades in ts order; build 1-min bars incrementally (`OrbTickAggregator`);
  maintain continuous running-high; on a new-session-high break during 09:30–10:00 **AND** the name
  scanner-confirmed **AND** causal ATR5%(bars-so-far) ≥ threshold → **SHADOW ENTRY** (record, no order);
  trail 2% on the bid (`_run_trail_exit`) → **SHADOW EXIT**. Slow (below-threshold) names → no shadow trade.
- **NO orders, NO OMS calls, NO live-bot state.** Pure read-only; writes only a shadow-trades log.

## Parameters (from the locked parent doc)
- trail_pct = 2%, no hard stop. Entry = tick break, continuous running-high. Gate = causal ATR5% ≥ ~4.3%
  (monitored/recalibrated). Confirmed-window entries. Webull 3s latency (for the modeled shadow exit).
- qty is irrelevant for shadow (log per-share); apply the §8 sizing only in the report, not the fills.

## Outputs
- `orb_shadow/shadow_trades.jsonl` — one row per shadow trade (date, sym, entry/exit ts+px, per-share
  pnl, atr5_at_entry, gate_pass, running_high, exit_reason).
- Daily summary appended to `orb_shadow/shadow_log.txt`: shadow trades taken, causal-gate pass/skip
  counts, per-share median/win, and the **parity result** vs offline `simulate_intrabar` (match/mismatch
  count) — a mismatch means the causal gate changed the trade set (expected, quantified) or a bug (flag).

## Validation of the harness itself (before trusting it)
- Backfill-run it on the 10 captured days (06-24…07-08); confirm: (a) it runs clean, (b) the causal-gate
  trade set is close to the hindsight-gated backtest (quantify the delta), (c) shadow per-share median is
  consistent with the accrual's intrabar-2% median. Report the delta; large divergence = investigate.

## Explicitly OUT of scope for the shadow (held to piece 2)
- Any change to `orb_app.py` / OMS / `trail_pct` on the live bot. Any real or paper-broker order. The
  live tick-entry build is HELD until the accrual gate passes (parent §7).

## Go / no-go for starting the ≥1–2 week live shadow run
Start once the backfill validation above is clean. Then it runs daily post-close alongside the accrual;
after BOTH the accrual median holds AND the shadow run is clean/consistent → the qty-1 fill-speed test.
