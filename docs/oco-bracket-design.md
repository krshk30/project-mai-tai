# Broker-native OCO bracket (RTH-first) — DESIGN (design-first, live stop path; nothing deploys)

> Operator's decision (2026-07-20): build the broker-native OCO bracket. It is the **unifying fix** — it
> **dissolves** the oversell root rather than working around it, and closes three leaks in one structure.
> Builds on [`orb-resting-bracket-entry-design.md`](orb-resting-bracket-entry-design.md) (the OTOCO feasibility
> work); this doc adds the OCO **exit pair** + the E5 framing + today's live constraints. RTH-first, no rush.

## FIRST QUESTION — native or emulated? DECIDED: NATIVE ON BOTH BROKERS, NO EMULATION (operator-confirmed 2026-07-20)
Native on **both** Schwab (v2, wired) and Webull (ORB, wired). No OMS-emulated sibling-cancel anywhere.
- **Webull:** native OTOCO in the SDK — `webull/trade/trade/v3/order_opration_v3.py`:
  `place_order(account_id, new_orders, client_combo_order_id)` + `preview_order(...)` (validate WITHOUT placing)
  + `replace_order(...)`. `ComboType` = `OTO`/`OCO`/**`OTOCO`**/`STOP_LOSS_PROFIT`, US-listed. Adapter uses
  single-leg `PlaceOrderRequest` (v1) today → the v3 combo path is **new adapter work**.
- **Schwab:** native OCO/bracket via `orderStrategyType` = `OCO` / `TRIGGER` with `childOrderStrategies` (the
  entry TRIGGERs the OCO exit pair). Adapter builds `orderStrategyType:"SINGLE"` today (`schwab.py:752`) → the
  combo path is **new adapter work** too.
- **Why no emulation (safety, not preference):** the OMS-on-fill path detects fills by 5s polling
  (`oms_broker_sync_interval_seconds=5`) then places the stop → a **~5s naked window** (prior doc Q2); worse, an
  OMS-managed sibling-cancel can **fail to fire on restart** = the ERNA failure mode. **Native puts atomicity +
  one-cancels-other AT THE BROKER — the exact risk that killed us cannot exist.** Build native on both; there is
  no fallback.

## ★★ THE RELOCATED-COLLISION RISK — the software exit MUST defer when the bracket is armed (design in from day 1)
**A native OCO fill is STILL an oversell if the OMS also runs its software exit on the same position.** Today
`_evaluate_v2_managed_exit` (`oms/service.py:1858`) runs `cw_exit_decision` (`:1929`) → `_emit_v2_managed_sell`
(`:2170`) on every quote tick. If a native OCO bracket is armed (target + stop reserving the shares at the
broker) AND the software ladder also decides to sell, that software market-sell is rejected oversold — **the
NXTC collision, just relocated from two OMS orders to (broker-OCO vs OMS-software).**
- **The fix: arming the native bracket flips the software exit to STAND-DOWN for that position.** When a position
  has a live OCO, `_evaluate_v2_managed_exit` short-circuits at the top and returns — the **broker OCO owns the
  exit**; the OMS does NOT run `cw_exit_decision` for it.
- **Precedent to reuse (already in the code):** the in-memory `_trigger_hard_stop` **already DEFERS to an active
  native guard during RTH** (`service.py:2980`), detected via `_has_active_native_stop_guard_order` (`:3140`).
  The OCO extends this defer from *just the hard-stop* to the **entire cw_exit ladder**, keyed on "is a native
  OCO armed for this symbol?" (extend the same broker-open-order check to recognize the combo's legs).
- **The OMS's residual role for an OCO'd position is exactly two things:** (a) **trail** = raise the OCO stop
  leg as price rises via broker-side `replace_order` (atomic; must not open a naked gap); (b) **reconcile** the
  OCO fill/cancel into `oms_managed_positions`. It does NOT run its own exit ladder. On fill of either leg → the
  broker auto-cancels the sibling → the OMS marks the position closed from the reconciled fill.
- **Symmetry:** arm bracket → software exit stands down; bracket cancelled (window cutoff / no fill) → software
  exit resumes. The stand-down flag lives with the managed position and is set/cleared on arm/cancel, and
  **re-derived from the broker on boot-rehydrate** (never trusted from memory alone).

## The structure (from the operator's hand-built "1st trgs OCO" ticket)
```
OTOCO (one combo, one client_combo_order_id):
  parent : BUY  STOP  @ break-level        (entry; fires on the cross up — trigger > market, proven today)
  child  : OCO {
             SELL LIMIT @ entry*(1+target) (the +2%/target leg)
             SELL STOP  @ entry*(1-0.05)   (the -5% protective leg; trigger < market, always valid for a long)
           }
```
On the parent fill, the broker activates the OCO pair; **the broker guarantees exactly one of the two fills and
auto-cancels the other.**

## ★ Why this IS the E5 fix, not a workaround (the load-bearing insight)
The NXTC oversell (07-14 ×3) was **two uncoordinated protective sells reserving the same shares** — a resting
protective sell reserved the qty, so the software exit's market-sell was rejected oversold. A broker-native OCO
makes target+stop **one linked pair**: the broker guarantees only one fills and cancels the sibling. **There is
no second uncoordinated sell to reject.** The oversell root is not patched with cancel-then-sell logic — **OCO
removes the second order entirely.** That is dissolving the root vs working around it.

## Three leaks closed in one structure
1. **The ~26¢ entry-chase** → the resting **buy-STOP** entry (live-proven today) fills AT the break, not the
   chased ask.
2. **Software-vs-broker-stop collision (the oversell)** → **OCO linkage** — one pair, broker-arbitrated, no
   second sell.
3. **Crash-exposure** → both exits are **broker-side**; they survive an OMS restart (no re-arm race, no naked
   window). Reconcile the combo on boot; don't re-place.

## Constraints locked from today's live ground-truth (2026-07-20 place+cancel through the code path)
- **Buy-stop trigger MUST be > market** (else `STOP_PRICE_MUST_BE_GREATER_THAN_MARKET`, the real 07-15 killer).
  Arm the bracket while price is still below the break level.
- **Sell-stop trigger < market** — always true for a −5% stop on a long → no rejection on the exit leg.
- **Webull stop-market is RTH-only.** The operator scoped this **RTH-first deliberately** → the entry+exits are
  plain STOP/LIMIT in NORMAL session, which **sidesteps the EH STOP_LIMIT/ext path** entirely. (EH bracket is a
  later, separate axis — needs limit-family legs + ext, out of scope here.)
- **Entry leg = plain buy-STOP** (his ticket), not STOP_LIMIT. Trade-off vs the prior doc: plain STOP catches a
  gap-over but chases the fill; STOP_LIMIT gives a clean no-fill ceiling but can miss. Default to his plain STOP
  for RTH; a `stop_limit` ceiling variant is a flag, decided from tape, not gut (prior doc Q3/Q5).

## Reuse (correct-the-type + wire-the-combo, not build-from-scratch)
- **Resting-entry primitive** — live-proven today (BUY STOP rests + cancels clean on `live:orb`).
- **Webull v3 combo SDK path** — `place_order`/`preview_order`/`replace_order`; **Schwab** — `orderStrategyType`
  `OCO`/`TRIGGER` + `childOrderStrategies` (new adapter methods on both; keep single-leg paths unchanged; flag-gated).
- **`preview_order`** (Webull) / a dry `orderStrategyType` preview — validate the combo shape with NO order placed.
- **The defer hook** — extend `_has_active_native_stop_guard_order` (`:3140`) to recognize an armed OCO, and add
  the top-of-`_evaluate_v2_managed_exit` (`:1858`) short-circuit (the stand-down). Mirror the existing
  `_trigger_hard_stop` defer (`:2980`). This is the single most important integration point.
- **`_process_cancel_intent`** (OMS) — cancel an un-triggered resting combo at the window cutoff.
- **#388 fill-reconcile + boot-rehydrate** — combo state (parent + OCO children) reconciled from
  `broker_order_events` on restart; fills counted, not emits; the stand-down flag re-derived from the broker.

## Per-broker verification (READ + STEP-1, before any build — the broker is the arbiter)
| Check | Webull (ORB, `live:orb`) | Schwab (v2, `live:schwab_1m_v2`) |
|---|---|---|
| Native OCO/bracket API | v3 `place_order(new_orders, combo_type=OTOCO)` | `orderStrategyType=OCO`/`TRIGGER` + `childOrderStrategies` |
| Entry leg (buy-stop) | STOP (→`STOP_LOSS`), **trigger > market** (proven today) | STOP, `stopPrice` > market |
| Exit stop leg (sell-stop) | STOP, **trigger < market** — never hits the >market rejection | STOP, `stopPrice` < market |
| Exit target leg (sell-limit) | LIMIT above entry | LIMIT above entry |
| Session | **stop-market RTH-only → NORMAL/RTH only** (EH out of scope) | RTH; verify duration DAY vs the combo |
| OMS reads OCO fills | reconcile combo fill/cancel events → ledger correct | reconcile OCO fill/cancel → ledger correct |
| One-cancels-other | STEP-1: one leg marketable → sibling auto-cancels, no oversell | STEP-1: same |

⚠ **Sell side never hits `STOP_PRICE_MUST_BE_GREATER_THAN_MARKET`** (that's a BUY-stop rule) — a −5% sell-stop is
always below market on a long, so the protective leg is structurally accept-safe on both brokers.

## STEP 1 — the GATE, run PER BROKER (far-from-market qty-1, before any real use)
Same discipline as the 2026-07-20 ticket test + the 06-30 F-test, but for a combo — on **both** `live:orb`
(Webull) and `live:schwab_1m_v2` (Schwab), qty-1, controlled:
0. **preview first** — Webull `preview_order` / Schwab dry combo — the account accepts the shape, ZERO orders placed.
1. **Rests:** place the OCO far from market (buy-stop entry far ABOVE market so it can't trigger; sell-stop far
   below, sell-limit far above) → broker accepts the combo + rests.
2. **Atomic-at-fill:** trigger the entry just above market on a qty-1 lot → confirm **BOTH exit legs go live at
   the broker the instant the entry fills** (query open orders).
3. **One-cancels-other:** make one exit leg marketable → confirm the sibling **auto-cancels** and there is **no
   oversell** (this is the E5 proof).
4. **Defer:** confirm the OMS software exit **stands down** for the armed position (no `_emit_v2_managed_sell`
   while the OCO is live) and **resumes** after the combo cancels.
5. **Cancel + flat:** un-triggered combo cancels clean; account flat. **Only if ALL pass on that broker does its
   build proceed.** Deploy-and-find-out is forbidden — an unattached stop is the naked-on-a-gapper risk this eliminates.

## The trailing ratchet — still live OMS work (unchanged by OCO)
OCO makes the **initial** target+stop atomic-at-fill. The **trailing** stop still needs the OMS to raise the
stop leg as price rises — via `replace_order` on the combo's stop child (broker-side replace). ⚠ **the replace
must not open a naked gap** (prefer atomic `replace_order` over cancel-then-add). Rides the existing tick-by-tick
consumer (#333), a different path from the ORB consume-loop lag (#387, already fixed).

## ⚠ Scope (operator-stated — do NOT re-argue the edge; he owns it)
- **OCO does NOT fix gap-slip.** A sell-stop still becomes a market order and walks a gapped book (SOBR
  −5%→−13.2%). This fixes **collision + chase + crash-exposure**, NOT the tape.
- **"Not going to save the strategy."** The entry family is dead in both forms. This is
  **fix-the-leaks-before-the-strategy-decision** — the right order, not an edge rescue.

## Sequencing (prereqs largely met; both brokers together)
1. #387 consume-loop + #388 phantom/fill-count — **DONE** (deployed).
2. **Adapter combo path on BOTH** — Webull v3 `place/preview/replace/cancel/detail`; Schwab `orderStrategyType`
   OCO/TRIGGER combo — flag-gated, single-leg paths untouched, byte-identical off.
3. **OMS bracket intent + the stand-down defer** (`_evaluate_v2_managed_exit` short-circuit when OCO armed) +
   combo-as-one-managed-unit + boot-reconcile (re-derive the stand-down from the broker).
4. **STEP 1 per broker** = go/no-go on each.
5. Wire the emit (ORB→Webull, v2→Schwab) → attended first-window validation → survival test (behaviour-identical
   off; qty-1 proven before size). Ship each broker only after its own STEP-1 + survival pass.

Nothing deploys today. Token re-auth tonight is the only committed live action.
