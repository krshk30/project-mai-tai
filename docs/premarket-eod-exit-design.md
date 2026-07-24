# Design: EH trading window (07:30–16:00) + EH-fillable entries/exits + EOD OCO cleanup

**Status:** DESIGN-FIRST 2026-07-24. Operator: **design + build today, do NOT deploy during market hours**
(deploy after-hours or tomorrow). Common to **BOTH entry modes** (resting + reactive) and **BOTH brokers**
(Schwab/TOS + Webull) — see [[feedback-assess-both-brokers]].

## Motivation
07-23 showed the day's ATR signal was overwhelmingly **pre-market**; the RTH gate left the bot idle on the
real action ([[project-mai-tai-resting-entry-out-of-window-bug]], [[project-mai-tai-premarket-trading-design]]).

## Broker capability (researched per broker — the constraint that drives everything)
| Order type | Schwab (TOS) in EH | Webull in EH |
|---|---|---|
| **LIMIT** (marketable or resting) | ✅ `session=AM/PM` | ✅ `extended_hours_trading=true` |
| MARKET | ❌ RTH-only | ❌ 417 |
| STOP / STOP-LIMIT **trigger** | ❌ RTH-only | ❌ RTH-only (stops 417 in EH) |
| Native OCO / bracket | ❌ `session=NORMAL` RTH-only | ❌ combo hardcoded `CORE` (RTH) |

**⇒ In EH, only a LIMIT fills — on either broker. Symmetric, no asymmetry.** Every EH order (entry AND exit)
must be a LIMIT. Verified from `broker_adapters/schwab.py` and `webull.py` (webull.py:189-200, 519-526).

## Current state (code)
- **Windows:** resting `_resting_in_window` = 09:30–16:00; reactive/general `entry_window_*` = 07:00–16:00
  (end tightened to 16:00 by Phase A #532; **start → 07:30 by P-B1**).
- **Entry:** OCO bracket emitted **RTH-only** (`_is_regular_market_session`). ⭐ **CORRECTION (P-B1, verified
  from code):** the earlier "NO EH routing for entries" claim was WRONG for the **reactive** entry — the bot
  ALREADY routes a v2 EH open to a `session=AM/PM` LIMIT at the live ask at the `_maybe_emit` chokepoint
  (`_apply_extended_hours_routing`, restored **dc11d5a 2026-06-23**, on main). So the reactive pre-market entry
  is **fillable today**, not an unfillable MARKET. What's genuinely missing (and is P-B1's scope): (a) a
  marketable buffer + max-cross **cap** so a thin-EH fill can't chase past the signal (flag `oms_v2_eh_entry_enabled`,
  OFF); (b) a **live-bar guard** on the reactive arm so a warmup-replayed trigger can't fire pre-market (#528
  mirror). The **resting** EH entry is still un-routed (drained directly, bypassing `_maybe_emit`) — that stays
  P-B2.
- **Exit:** EH routing EXISTS for managed-exit SELLs (MARKET→LIMIT + `session=AM/PM` off the live bid, #390,
  service.py:2277). OCO is the RTH exit; the software CW ladder is the fallback.
- **EOD:** v2 positions ride to **19:55** then `_v2_overnight_flatten` (EH-limit, retry). ⛔ **16:00–19:55 the
  OCO legs are RTH-dead** = the ~4-hour dead-OCO gap the operator identified.

## R1 — Trading window 07:30–16:00, both modes
- `_resting_in_window`: 09:30–16:00 → **07:30–16:00** (schwab_1m_v2.py:1410).
- `entry_window_start_hour_et/minute`: 07:00 → **07:30**; `entry_window_end`: 16:30 → **16:00**.
- **No new entries after 16:00** (both modes). Strategy-side, broker-agnostic. Low-risk, pin thresholds in tests.

## R2 — EH-fillable entries + exits (the core; both brokers)
**Rule: whenever it's EH (before 09:30 or after 16:00), every order is a marketable/resting LIMIT.**

**Entry (pre-market 07:30–09:30):** neither the resting buy-STOP-LIMIT (stop trigger dead in EH) nor the
reactive MARKET fills in EH. So in EH the **strategy** detects the trigger (ATR up-cross for resting; the
3-bar-break for reactive) and emits a **marketable EH-LIMIT buy** — priced at the ask, bounded by the 0.5%
band for resting — with `session=AM` (Schwab) / `extended_hours_trading=true` (Webull). The broker OCO is NOT
emitted in EH (stays RTH-gated); the position is managed by the software ladder (below).

**Exit (EH):** reuse the existing #390 EH exit routing — software CW ladder target/stop as EH-LIMITs off the
live bid. Already built + live-proven (KIDZ 07-06); it applies to any EH-managed position.

**Per broker:** Schwab `session=AM/PM` LIMIT; Webull `extended_hours_trading=true` LIMIT. Both proven for
exits; the new work is routing the ENTRY as an EH-LIMIT (mirror the exit routing on the open path).

## R3 — EOD OCO cleanup at 16:00
At the 16:00 RTH→post transition, for each OMS-managed position still open:
1. **CANCEL the native OCO legs** (RTH-dead). Reuse the OCO-cancel path (`_cancel_native_stop_guard_before_sell`
   / the stand-down clear, service.py:1223).
2. Transition the exit to the **software EH-LIMIT ladder** (R2 exit) so the +2%/−5% keeps working post-16:00.
3. Backstop: the existing 19:55 overnight flatten still closes anything unfilled.

**✅ DECISION LOCKED (operator 2026-07-24): (A) KEEP MANAGING.** At 16:00 cancel the dead OCO legs, keep the
+2%/−5% exit running as EH-LIMITs through post-market, 19:55 flatten as the backstop. NOT an immediate
liquidation — "this is the right thing; we don't just wanna cancel it."

## Phasing (build today, deploy after-hours) — REVISED for the P1/P3 coupling
⚠ **P1-start and P3 are COUPLED:** opening the RESTING window to 07:30 without the EH-entry mechanism sends a
pre-market STOP_LIMIT the broker rejects (the #523 bug). So split by *safety/independence*, not by requirement:
- **Phase A (safe, standalone, deploy-first):** R3 EOD OCO cleanup at 16:00 (decision A) **+** the entry-window
  END → 16:00 (no entries after 4 PM, both modes). Reuses the existing OCO-cancel + #390 EH-exit; the
  highest-value safety fix (closes the 16:00–19:55 dead-OCO gap). No pre-market entry risk.
- **Phase B (coupled):** entry-window START → 07:30 **+** the EH-LIMIT entry mechanism (software-emulated,
  both brokers). Needs the Webull-pricing research + the live-bar guard (#528). The big piece.
Each phase its own PR + tests + safety mutation. Flag-gated → merge to main today, deploy dark, enable
after-hours (deploy = VPS pull + restart, held for after-hours; merging to main does NOT deploy).

## ⚠ Risks / open research (settle during build)
1. **Webull EH pricing** — webull.py:521 says "no Webull market-data entitlement to price EH limits." We must
   price the Webull EH-LIMIT off OUR feed (the shared Polygon/Schwab quotes we already stream), not Webull's.
   Confirm the quote path reaches the Webull entry/exit builder. **RESEARCH before P2/P3.**
2. **Pre-market DATA** — Schwab REST serves no same-day pre/post bars; live EH bars need the CHART_EQUITY
   streamer, and warmup replays STALE bars (#528 trap). The EH entry MUST carry the same live-bar guard the
   resting entry got, or it fires on stale prices. **Hard prerequisite for P3.**
3. **Thin-EH slippage** — a marketable EH-LIMIT in illiquid pre-market can slip; bound it (band + a max-cross
   cap) and prefer no-fill over a bad fill.
