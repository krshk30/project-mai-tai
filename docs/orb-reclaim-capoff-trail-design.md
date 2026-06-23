# ORB intrabar-reclaim live test — cap-off + reclaim@OR_high + 3% trail (flag-gated)

**Status:** flag-gated, default OFF. First live trades = the 9:30 open after deploy,
on `paper:orb` (Alpaca paper) — the paper→live ladder. Real-money routing is a
separate, later go-live gate.

## Why
7-day backtest (read-only) found the settled ORB (12% width cap, bar-close entry,
TRAIL-8%) leaves the big moves on the table: the cap screens the most volatile names
(HSCS 06-23, +40% peak, screened at 12.30%), and bar-close entry buys the spike top.
The **reclaim-of-OR_high entry + trailing stop** captured the real movers in the
backtest (EHGO +18%, ATPC +21%, CRVO +24%, HSCS +12%). **But the entry edge rests on
an idealized fill at OR_high we don't trust** — so the point of the live test is to
**measure the real fill**, not chase P&L.

## What this change does (only when `orb_intrabar_reclaim_enabled=true`)
1. **Cap-off** — the 2–12% width band is removed; any in-time OR (≥5 bars) arms
   (`_build_or_no_cap`). Wide, volatile names are no longer screened.
2. **Reclaim entry** — replaces the bar-close breakout. Once the OR is armed, a tick
   at/above OR_high starts a hold timer; if price stays ≥ OR_high for
   `orb_reclaim_hold_secs` (25s), emit ONE open intent as a **resting LIMIT at OR_high**
   (`_check_reclaim`). A tick back below OR_high resets the timer (pullback-then-reclaim
   is fine — the sustained reclaim is the confirmation). Entries only in (OR-end, cutoff].
3. **3% trailing stop** — `orb_reclaim_trail_pct=3.0` flows through the existing OMS
   stop-guard trail (#340, ratchets from HWM, never down). No OMS change.
4. **Size** — `orb_reclaim_quantity=5`.

When the flag is OFF every branch above is skipped — bar-close entry, 12% cap,
TRAIL-8%, qty 10 — **byte-identical to today** (proven: full existing ORB unit suite
passes unchanged; `_reclaim_mode` defaults False at class level so even `__new__`-built
test instances read legacy).

## Fill instrumentation (the required deliverable)
The reclaim intent stamps `orb_intended_or_high` (= OR_high) and `orb_reclaim_emit_ms`
(reclaim-confirm time) into the order metadata, persisted to `broker_orders.payload`.
The actual fill (price, time) lands in the `fills` table as usual. `scripts/orb_fill_slippage.py`
(read-only) joins them and reports, per ENTRY: intended OR_high · actual fill ·
slippage (¢ and %) · time-to-fill · **or UNFILLED** (limit never touched — the key
realism check); and per trailing-stop EXIT: intended trail level vs actual fill.

## Validation gates
- **(a) RTH / flag-off unaffected** — byte-identical; existing ORB suite green.
- **(b) reclaim arms + fires correctly** — cross→hold→one entry; dip resets; window-bound;
  one-trade-per-symbol (`test_orb_reclaim.py`).
- **(c) cap-off** — a >12%-width OR arms in reclaim mode; legacy would reject it.
- **(d) 3% trail + kill-switch/flatten** — trail is the unchanged OMS #340 mechanism with
  `trail_pct=3`; kill-switch/protected-symbol/flatten are unchanged OMS machinery.
  Confirmed live on the paper run (first open after deploy).

## Deploy
PR → review → approve → deploy. Enable on `paper:orb` via
`MAI_TAI_ORB_INTRABAR_RECLAIM_ENABLED=true` + `MAI_TAI_ORB_QUANTITY` unaffected
(reclaim uses `orb_reclaim_quantity=5`); restart `project-mai-tai-orb`. First trades
next 9:30 open. Keep separate from the #350 capture.

## Caveats (carried from the backtest)
- **Idealized OR_high fill is exactly what we're testing** — a resting limit at OR_high
  fills only on a touch/pullback; if price runs away it does **not** fill (logged as
  UNFILLED). Alpaca paper fills are quote-based (a real-ish proxy, not the zero-slippage
  internal sim), so this is a first read, not the real-money number.
- One week of backtest proves direction, not magnitude; real-money is a separate gate.
