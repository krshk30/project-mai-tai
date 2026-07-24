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

## R3b — pre-market-opened position across the 09:30 open (the MIRROR of R3)
**✅ DECISION LOCKED (operator 2026-07-24): option 1 — KEEP ON THE CW LADDER, do NOT convert to OCO.**

R3 is the 16:00 RTH→post transition (OCO → software ladder). R3b is the *symmetric* pre-market→RTH
case at 09:30, and the answer is the same shape: **keep managing on the software CW ladder; do NOT
emit an OCO at the open for an already-held position.**

A v2 position that opens in **extended hours** has **no broker-native OCO** — `_apply_v2_oco_bracket_entry`
skips when `not _is_regular_market_session()` (service.py; the native OCO is a `session=NORMAL` RTH-only
construct). It is therefore managed by the software CW +2%/−5% ladder, **continuously across 09:30**:

- **No stand-down.** With no confirmed broker bracket, `_native_oco_stand_down_active` **FAILS OPEN**
  (returns False — the deliberate asymmetry, service.py: a wrong *True* would leave the position with no
  exit at all, the ERNA shape). So `_evaluate_v2_managed_exit` runs the ladder; it is never stood down.
- **Session-aware exit routing (#390).** The same ladder EH-routes the exit **before** 09:30 (LIMIT +
  `session=AM` off the live bid, via `_emit_v2_managed_sell`/`_extended_hours_session`) and uses the
  **normal MARKET exit after** 09:30 — one ladder, two routes, no gap.
- **Continuously monitored.** The fillable gate (`_market_is_fillable`, default 7 AM–8 PM ET) is True on
  **both** sides of 09:30, so the quote consumer keeps evaluating the position across the open.
- **No OCO emitted at the open.** The OCO-emit path is **entry-only** (`_apply_v2_oco_bracket_entry` is
  reached solely from the buy-open intent handler, service.py ~907). A held position generates no
  buy-open at 09:30, so **no bracket is ever emitted for it** — the open is not an OCO / re-entry event.

**Why keep it (not upgrade to a 09:30 OCO emit):** this is the clean mirror of the Phase A 16:00 EOD
OCO→ladder transition (R3, decision A) — the exit geometry is identical (+2%/−5% off the actual fill),
and the fail-open stand-down already makes the ladder own the exit without any new code. Converting to
an OCO at 09:30 would add a broker round-trip and a re-arm flip-flop risk for zero change in geometry.

**Regression pin:** `tests/unit/test_v2_premarket_position_across_open.py` — stand-down fails open in
both EH and RTH wall-clocks; the ladder emits the EH-LIMIT exit pre-09:30 and the MARKET exit in RTH;
no bracket/buy-open intent is produced for the held position at the open; and a guard/mutation test
pins that a wrongly-*True* stand-down would silence the ladder (naked) while fail-open keeps it alive.

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

## R2b — Webull MIRROR extended-hours parity (built 2026-07-24, flag-gated OFF)
The v2→Webull mirror-on-fill (`_mirror_v2_fill_to_webull`, #531) builds a **MARKET master +
native-OCO combo** — BOTH RTH-only on Webull (417 in EH). So a primary Schwab v2 fill in
**extended hours** made the mirror reject → EH trading was Schwab-only. This closes that gap so the
dual-broker goal holds in EH too.

**Branch (at mirror time):** when `not _is_regular_market_session()` **and**
`strategy_schwab_1m_v2_webull_mirror_eh_enabled` is ON, swap the combo for a **single-leg marketable
EH-LIMIT master** (`_build_v2_mirror_eh_master`): `order_type=limit` + `extended_hours=true` +
`session=AM/PM`, priced off OUR fresh ask (`_latest_quotes_by_symbol`), buffered above the ask so it
crosses, and **bounded by the P-B1 max-cross cap vs the Schwab FILL price** (`oms_v2_eh_entry_*`
constants, shared with the reactive EH entry). **No fresh ask / ask past the cap → ABANDON** (no
submit — nothing is opened, so no naked EH position). NO `bracket`/`native_oco_bracket` keys →
`webull.py::_is_bracket_request` is False → the adapter's single-leg EH path (`_submit_blocking` +
`set_extended_hours_trading(True)`, granted only for a LIMIT-family type).

**Exit coverage (confirmed from code — no naked EH position):** the mirrored Webull position is
exit-managed by the **account-aware software CW EH-limit ladder**, automatically:
- the Webull buy FILL creates a managed row + arms `_managed_v2_symbols` for the **Webull account**
  (`_on_v2_fill`); `_v2_accounts()` already includes the Webull account when the mirror flag is on;
- the ladder emits via `_emit_v2_exit`, which routes off `row.broker_account_name` (THE INVARIANT)
  and **EH-routes** via `_extended_hours_session()` → `session=AM/PM` LIMIT off the live bid (#390);
- with **no** native-OCO combo emitted in EH, `_native_oco_stand_down_active` **fails open** (no
  confirmed bracket) → the software ladder runs and anchors +2%/−5% off the ACTUAL Webull fill.

**Flag (separate, not shared):** `strategy_schwab_1m_v2_webull_mirror_eh_enabled` (OFF). NOT reusing
the primary's `oms_v2_eh_entry_enabled`: the mirror writes to the **shared live:orb account** (ORB
also trades it — see the collision guard), so a shared flag would make enabling the *isolated* Schwab
reactive-EH entry also start writing EH orders to that shared real-money account. Separate flags let
the operator enable primary-EH first (isolated, observe) and mirror-EH later, independently. Mirror-EH
also requires `strategy_schwab_1m_v2_webull_mirror_enabled` (both ON). **RTH, or either flag OFF →
byte-identical MARKET + combo** (the current mirror). `docs/webull-mirror-on-fill-design.md` is the
base mirror spec.

## ⚠ Risks / open research (settle during build)
1. **Webull EH pricing** — webull.py:521 says "no Webull market-data entitlement to price EH limits." We must
   price the Webull EH-LIMIT off OUR feed (the shared Polygon/Schwab quotes we already stream), not Webull's.
   Confirm the quote path reaches the Webull entry/exit builder. **RESEARCH before P2/P3.**
2. **Pre-market DATA** — Schwab REST serves no same-day pre/post bars; live EH bars need the CHART_EQUITY
   streamer, and warmup replays STALE bars (#528 trap). The EH entry MUST carry the same live-bar guard the
   resting entry got, or it fires on stale prices. **Hard prerequisite for P3.**
3. **Thin-EH slippage** — a marketable EH-LIMIT in illiquid pre-market can slip; bound it (band + a max-cross
   cap) and prefer no-fill over a bad fill.
