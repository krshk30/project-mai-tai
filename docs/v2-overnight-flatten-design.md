# v2 overnight flatten (design) — the last unbounded exposure

**DESIGN-FIRST. Flag-gated, byte-identical off. Attended deploy.** Live-money exit path. 2026-07-16.
Mirror of #471 (ORB window-flatten), adapted for v2 (which arms **zero** native stops).

## 1. The hole (not theoretical)

v2's entry window runs to **16:30**; the OMS fillable-exit gate is **07:00–20:00**; v2 arms **zero**
native broker stops at any hour. ⇒ **a v2 position held past 20:00 has no broker stop AND no fillable
software stop for ~11h — fully naked.** Live 07-15: ASTN (filled 16:11) + CPHI (16:28), both after-hours
entries, both held past the close; operator closed ASTN by hand. ORB got #471; v2 has nothing.

**#464 does NOT fix this.** #464 closed the false-flat *deletion* path. This is the **clock**: a
position with a perfectly intact software stop is still naked after 20:00 because nothing can fill.

## 2. The fix — same shape as #471, driven off the v2 ledger

At **T = 19:55 ET**, for every OMS-managed v2 position, emit a **full-qty close**.

- **Drive off `_managed_v2_symbols`** (the OMS's in-memory set of open v2 managed positions, rebuilt
  from `oms_managed_positions` on fill/rehydrate) — **OMS-owned by construction**, so the scoping
  invariant holds for free: a manual holding is invisible and can never be flattened. (Analogous to
  #471 driving off `_armed_hard_stops`; v2 has no armed stops, so the managed-row set is the equivalent.)
- **Reuse the existing v2 exit primitive `_emit_v2_exit_on_loop`** with `sell_qty=current_quantity`,
  `reason="V2_OVERNIGHT_FLATTEN"`. It already routes **LIMIT + session (#390)** so it is **EH-fillable**
  at 19:55 — a raw market order would NOT fill in extended hours (the #390 lesson). Same close path as a
  managed exit → same reconcile/close-on-fill semantics (#392/#464).

## 3. ⚠ Why time T = 19:55 (safety only)

    07:00–20:00   OMS gate can fill        FILLABLE
    after 20:00   gate closed              NAKED (no software fill, no native stop)

**19:55 = exit while the gate can still fill.** It changes **zero strategy** — every position that
exits normally today still does (they exit well before 19:55). It only catches a position *still open*
at 19:55, which is the naked-overnight case. Flag `oms_v2_overnight_flatten_hour_et=19` / `minute_et=55`.

**⛔ DO NOT FUSE WITH A STRATEGY CHANGE.** #471 fused a safety hazard (overnight-naked, which only needed
a late flatten) with an un-backtested 10:00 strategy cap, and it cost a day to untangle. This ships the
**safety** at 19:55. The separate strategy question — *should v2's entry window run to 16:30 at all?*
(both overnight holds were after-hours entries into thin books) — is **out of scope here**, decided later.

## 4. Not blocked by E5 (the oversell root)

E5 is a **resting protective sell-stop** reserving the shares so the OMS's own close is rejected as
oversold. This flatten is a **single close order**, not a resting stop — it reserves nothing and creates
no reverse/oversell conflict. Holds whether the close routes market or limit; we route **limit** only for
EH fillability (§2), which does not change the non-reserving property.

## 5. Failure must be LOUD (the #471 rule)

A flatten that fails is exactly the naked state it exists to prevent. So:
- **No fresh bid to price the limit** (thin AH) ⇒ log at **error**, **release the idempotency claim**,
  retry each loop until 20:00. If still unfilled at 20:00 ⇒ **ntfy** (the operator noticing is the
  current control that has worked). Do NOT treat a failed/unfilled close as flat.
- **Dedup guard:** if an exit order already works for the symbol (`snapshot.dedup_active`), do NOT
  re-emit — the managed exit already has it.
- Log `[OMS-V2-OVERNIGHT-FLATTEN] sym qty -> closing (gate closes 20:00)` per symbol.

## 6. Sketch (grounded in `_evaluate_v2_managed_exit`)

    def _v2_overnight_flatten_due(now): return (et.hour, et.minute) >= (hh=19, mm=55)   # session-aware

    async def _v2_overnight_flatten():
        if not settings.oms_v2_overnight_flatten_enabled: return
        if not _v2_overnight_flatten_due(): return
        for (acct, symbol) in list(self._managed_v2_symbols):
            key = (session_day, acct, symbol)
            if key in self._v2_overnight_flattened: continue
            snapshot = await _run_db(read_v2_managed_snapshot(acct, symbol))   # None => no open row
            if snapshot is None: self._managed_v2_symbols.discard((acct,symbol)); continue
            if snapshot.dedup_active: continue          # an exit already works
            quote = self._latest_quotes_by_symbol.get(symbol); bid = float(quote.get("bid") or 0)
            if bid <= 0:                                  # can't price the EH limit -> LOUD, retry
                log.error("[OMS-V2-OVERNIGHT-FLATTEN] %s no bid, cannot place — retrying", symbol); continue
            self._v2_overnight_flattened.add(key)         # claim BEFORE the await (one/symbol/day)
            position = self._hydrate_v2_position(snapshot); position.update_price(bid)
            await self._emit_v2_exit_on_loop(acct, symbol, position, snapshot.entry_price,
                kind="overnight_flatten", reference_price=bid, reason="V2_OVERNIGHT_FLATTEN",
                bid=bid, close_on_fill=True, sell_qty=snapshot.current_quantity)

Hook: call `_v2_overnight_flatten()` in the run loop next to `_window_flatten_armed_stops()`.

## 7. Edge cases

| case | expected |
|---|---|
| already flat (operator closed by hand) | no open row ⇒ discard, no churn (the #464 path) |
| an exit order already works at 19:55 | dedup guard ⇒ skip (managed exit owns it) |
| thin AH, no bid | LOUD error + retry until 20:00 + ntfy; never read as flat |
| fresh fill just before 19:55 | flattens it too — correct, it must not ride overnight |
| OMS restart at 19:54 | `_managed_v2_symbols` rehydrates from `oms_managed_positions` ⇒ still fires |
| manual holding (CYN/CELZ) | not in `_managed_v2_symbols` ⇒ untouched (scoping invariant) |
| flag off | byte-identical |

## 8. Tests (each must fail before the fix)

1. Open v2 row at T ⇒ full-qty close emitted via `_emit_v2_exit_on_loop`, reason V2_OVERNIGHT_FLATTEN.
2. Before T ⇒ no flatten. Flag off ⇒ no flatten (byte-identical).
3. No open row ⇒ discard, no emit. Manual (non-managed) symbol ⇒ never touched.
4. dedup_active ⇒ no re-emit.
5. No bid ⇒ error log + claim NOT held (retries next loop).
6. Idempotent: two loop passes after T ⇒ exactly one close per symbol per day.
7. Session-aware T (half-day) ⇒ fires relative to the real 20:00 gate, not a wall-clock literal.

## 9. Rollout

Flag `oms_v2_overnight_flatten_enabled` default **false**. Attended deploy (OMS-only choreography,
fleet-flat). First live exercise is a day v2 actually holds to 19:55 — rare, so **the tests are the
verdict**, not the first session. Rollback = flag false + restart.

## 10. ⚠ Scope note (separate, later)
Whether v2's entry window should run to 16:30 at all (both overnight holds were after-hours entries into
thin books) is a **strategy** question — decided separately, never fused into this safety ship.
