# ORB Resting Stop-Buy Entry — Design (2026-07-13)

**Status:** design-first, flag-OFF. Supersedes the reactive entry for ORB running-high mode when
enabled. Companion to the parked OTOCO bracket design (`orb-resting-bracket-entry-design.md`) —
this is the **entry-only, Phase-1** step (keep the current OMS-armed stop; add the floor/scale
exit later).

## Why (the R&D conclusion, 2026-07-13)
On the correct pre-open confirmed universe with **honest fills**, ORB's leak is **execution, not
the entry**: the current path detects the break on **bar close**, then the OMS quote-prices a limit
~3–14s later → it fills the **faded ask 6–7% below the break** (VEEE 12.35→11.57, AGEN 5.49→5.11),
then the stop cuts it. The fix is to fill **AT the break** via a **resting native buy-stop-limit**
placed *before* the move. Backtest `--mode resting` (PR #442) models this; it beats bar-close.
Operator chose **resting over intrabar** (intrabar's backtest edge won't survive a volatile live
tape — it buys micro-spike tops). Full arc: memory `project_mai_tai_orb_rnd_2026_07_13`.

## Current flow (Piece-1, reactive) — for contrast
Bar closes with `high > running_high` → ORB emits an `open` intent (order_type=limit,
price_source=ask, `orb_intended_break_level`, omit limit) → OMS reprices at placement
`limit = min(ask+tick, level·(1+gap_cap))` → single LIMIT order, filled at the then-ask.

## New flow (resting stop-buy)
1. **At 09:30** (running high known from the 09:25–09:30 bars) ORB places a **resting BUY
   STOP_LIMIT** at the running high: `stop = running_high`, `limit = running_high·(1+gap_cap)`,
   qty = `orb_reclaim_quantity` (=2), TIF=DAY, RTH.
2. **Each completed bar (09:30–10:00)** the running high may advance (`max(rh, bar.high)`). When it
   rises, ORB **replaces** the resting order at the new (higher) level (cancel-old→place-new, or a
   native Replace — see Order management).
3. The order **rests at Webull**; it fills the instant price trades up through the stop, at/near the
   level (bounded by the limit = gap-cap). A violent gap-through above the limit **does not fill** —
   correct (the next bar's higher level replaces it).
4. **On fill** → position open → OMS arms the protective stop as today (Phase-1 keeps the current
   native hard-stop/trail; Phase-2 swaps in the floor/scale ladder).
5. **Reclaim** (2-attempt cap): after an exit while still in-window, place a new resting order at the
   current running high. Attempts counted on the same `_ENTRY_ATTEMPT_CAP=2` rule as today.
6. **10:00 window end**: cancel any resting order; no new entries. (An open position still exits.)

## Components to build
### A. Webull adapter — BUY stop-limit  ⛔ GATE
- A resting entry = **side=BUY, order_type=STOP_LIMIT** → adapter maps STOP_LIMIT→`STOP_LOSS_LIMIT`.
  `STOP_LOSS` is semantically a *sell* stop; **whether Webull accepts a BUY `STOP_LOSS_LIMIT` is
  unverified** (only SELL STOP_LOSS is proven — see #386/#434/#435).
- **GATE = `validate_buy_stop.py` (RTH only — Webull stops are RTH-only):** place qty-1 BUY
  STOP_LIMIT far above market (stop=live·1.5, can't fill), confirm ACCEPT+REST, cancel, verify FLAT.
- If it **rejects**, add a side-aware mapping (BUY STOP → a Webull buy-stop enum, if one exists) OR
  fall back to the OTOCO/single-leg alternative. Do NOT enable live until this passes.

### B. OMS — resting-entry order lifecycle
- New intent shape `orb_resting_entry` (or metadata `entry_mode=resting`): the OMS places the
  BUY STOP_LIMIT (not the quote-priced limit), records the broker order id, and supports
  **replace** (cancel-confirm→place, mirroring the v2 reverse-race lesson — never leave two live
  buy orders). Reuse `_replacement_client_order_id` (bounded ≤40, #436) for replace ids.
- Fill handling unchanged (arm the protective stop on fill). Cancel-on-window-end + cancel-on-flip.

### C. ORB bot — resting-order state machine
- Replace the bar-close `_on_bar_running_high` **emit** with: maintain the running high; on each bar,
  ensure a resting order exists at the current level (place / replace-up); on fill, mark held + arm
  reclaim after exit; cancel at 10:00. `traded`/`attempts`/reclaim semantics reuse the #388 fill-
  reconcile (count fills, not emits).

## Flag & rollback
`MAI_TAI_ORB_RESTING_ENTRY_ENABLED` (default **false**). OFF ⇒ **byte-identical** to the current
Piece-1 reactive entry. Rollback = flag false + ORB restart.

## Edge cases (must handle)
- **Replace race:** cancel-old + place-new can leave two live orders or an oversell-style conflict
  (cf. the v2 reverse-race). Prefer a native **Replace** if Webull supports it; else cancel-confirm
  before place, and dedup on the client_order_id.
- **Fill during replace:** if the old order fills while replacing, detect the fill (order-events) and
  do NOT place the new one; go to held/exit.
- **Gap-through:** price gaps above `level·(1+gap_cap)` → the resting limit rests unfilled → the next
  bar's higher level replaces it (never chase past the gap-cap — same as `entry_fill` ASK_PAST_GAP_CAP).
- **Never naked:** Phase-1 the buy-stop has no attached sell-stop; the OMS arms the native stop on
  fill (as today). Phase-2 = OTOCO (buy-stop + attached sell-stop live at fill).
- **Restart while a resting order is live:** the order rests at the broker across an ORB restart;
  rehydrate/re-track it on boot (or cancel+re-place at the current level).

## Staging
- **Stage 1 (this):** resting entry + **current** exit (OMS native stop/trail), qty 2, flag-gated.
- **Stage 2 (later):** floor + 2%/4% scale exit (route ORB to the OMS ladder).

## Test plan (tomorrow, RTH, attended)
1. **09:30 ET:** `validate_buy_stop.py F GO` → must PASS (Webull accepts BUY STOP_LIMIT). Gate.
2. If PASS → enable `MAI_TAI_ORB_RESTING_ENTRY_ENABLED=true` on a **flat** ORB, attended, watch the
   first resting placement + a fill at the level (`[ORB-RESTING-PLACED]`/`[ORB-RESTING-FILL]`).
3. Forward-test small-qty (2), compare fills-vs-break-level to the honest-backtest expectation.
