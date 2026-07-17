# E5 option-1 — v2 exit cancels its native stop-guard before selling (DESIGN-FIRST, NOT BUILT)

**Status:** REVIEW-READY. No code. Live real-money exit path (v2/Schwab).
**Date:** 2026-07-17 · **Rollout:** attended, fleet-flat, flag-gated. Not today.
**This is a PREREQUISITE for P0.3 (v2 native stop), not an independent fix — see §2.**
[[feedback_has_the_other_bot_solved_this]]

---

## 1. The matched pair, and the half v2 is missing

`process_trade_intent` does two things for a sell that only make sense TOGETHER:
- **(a)** `_cancel_native_stop_guard_before_sell` (`oms/service.py:714`) — cancel the resting native
  stop before any non-guard sell.
- **(b)** `get_open_exit_reserved_quantity(include_native_stop_guard=False)` (738/807) — EXCLUDE guard
  orders from the reserved-share count.

**(b) is only safe because (a) ran:** the guard is already cancelled, so it holds no reservation, so
excluding it from the count is correct.

**v2's `_emit_v2_managed_sell` bypasses `process_trade_intent` wholesale** — it builds a
`TradeIntentEvent` (2232), never publishes it, and goes straight to `create_trade_intent` →
`_record_internal_risk_pass` → `submit_order` (2265). So it never reaches (a). **But it re-implements
(b) inline** at `_read_v2_managed_snapshot` (2028–2033), same function, same
`include_native_stop_guard=False`.

⭐ **v2 took HALF the pair — and half is WORSE than neither.** Skip both ⇒
`include_native_stop_guard=True` ⇒ the guard's reservation is counted ⇒ dedup fires ⇒ safe by
omission. Take (b) alone ⇒ the guard is deliberately NOT counted ⇒ dedup false ⇒ market-sell ⇒ oversold
reject. **v2 did not skip a guard; it inherited an optimization whose precondition it never
established.** [[feedback_has_the_other_bot_solved_this]]

## 2. Why it is INERT today, and why it still must ship (the sequencing)

**v2 arms ZERO native stop-guards** — re-verified 2026-07-17: **0 guard orders of 3863 since 07-01**
(ORB 39/77). So today:
- (a) missing = a no-op (`_cancel_native_stop_guard_before_sell` finds `native_order is None`, returns `[]`).
- (b) present = a no-op (excluding guard orders from a count that has none changes nothing).

⇒ **E5 option-1 changes NOTHING observable while v2 has no native stops.** Its entire value is
**making the pair whole so P0.3 can arm a native stop SAFELY.** The moment P0.3 arms one, (b) becomes a
live bug (the guard reserves the shares, dedup deliberately ignores it, the exit is rejected oversold —
the NXTC *signature*, though NXTC itself was a shared-account discrepancy, not this bug).

**Sequencing: E5 option-1 is a PREREQUISITE for P0.3, not a standalone.** Ship it first (inert), then
P0.3 can land without re-introducing the oversell. Shipping P0.3 without E5 would arm a stop the exit
path cannot cleanly sell around.

## 3. The fix (option 1 — the chosen one)

Call `_cancel_native_stop_guard_before_sell` from `_emit_v2_managed_sell`, before the submit, mirroring
the 714 call site. `_emit_v2_managed_sell` already has everything the function needs:
- `session` (its own), `strategy` + `broker_account` (fetched 2195–2198), `symbol` (`row.symbol`).

Shape (after the strategy/account None-guard at 2199–2204, before building the OrderRequest at 2252):
```python
guard_cancel_events = await self._cancel_native_stop_guard_before_sell(
    session=session, strategy=strategy, broker_account=broker_account, symbol=row.symbol,
)
# ... then build request, submit, record; return [*guard_cancel_events, *events]
```
- **Scope it to full-close/scale sells only** — the same predicate `process_trade_intent` uses at 711
  (`intent_type in {close, scale}`, side sell, not itself a guard). A v2 managed exit is always a
  sell-close/scale, so this is satisfied by construction, but assert it rather than assume.
- **Publish the returned cancel events** (they are `OrderEventEvent`s the caller currently drops on the
  floor; `_emit_v2_exit_on_loop` already publishes `events` in a loop at 2167 — extend it).
- **Flag:** `oms_v2_exit_cancel_guard_enabled` (default **False** ⇒ byte-identical; and inert even ON
  until v2 arms a stop).

## 4. ⛔ Why NOT option 2 (route v2 exits through `process_trade_intent`) — settled, do not revisit

Routing v2's managed exit through `process_trade_intent` would get (a) for free — but it would also
newly subject a **protective close** to `_evaluate_risk` AND the **protected-symbol gate**. Those can
**REJECT** an intent. **A protective close that can be refused is the ERNA shape: a position the OMS
does not believe it can sell is one it cannot sell.** Never make a protective exit refusable. v2
currently uses `_record_internal_risk_pass` precisely to keep its own exits unconditional; option 2
throws that away. **Option 1 replicates the ONE thing the exit needs from `process_trade_intent` (the
guard cancel) without inheriting the parts that can refuse it.**

## 5. ⚠️ Interaction with #486 (per-submit claim) — both touch v2's exit-submit path

Both this and #486 modify the v2 exit-submit chain (`_emit_v2_exit_on_loop` → `_emit_v2_managed_sell`).
**They compose; state how so it is not discovered at implementation:**
- #486 wraps the whole exit in a per-`(account, symbol)` `_submit_in_flight` claim, released in
  `finally`.
- E5 adds a guard-cancel `submit_order` **inside** that chain, BEFORE the sell submit.
- ⇒ Under #486, the claim spans **two** broker round-trips (cancel, then sell) for one position. That is
  correct — the claim must cover every submit for that position — but it **lengthens the claimed
  window** by one cancel round-trip. Note it: the race window #486 closes is now cancel+sell, not just
  sell.
- **Ordering within the claimed window:** cancel guard → sell. That IS the manual OCO. E5 provides the
  cancel; #486 provides the mutual exclusion. Neither subsumes the other.
- No shared state; no collision. Build order does not matter, but if both land, the reviewer must
  confirm the cancel submit is inside the claim (it is, if E5's call sits inside `_emit_v2_managed_sell`
  which is already inside #486's claimed `_emit_v2_exit_on_loop`).

## 6. Open review points

- **The cancel is itself a `submit_order`** — a second broker await on the exit path. Bounded by #391
  Fix-1 (≤5s). On a protective close this adds latency before the sell; acceptable (the cancel must
  precede the sell by construction), but size it against the exit-urgency budget.
- **Cancel-fails-then-sell:** if the guard cancel is rejected/errors, does the sell still go? For a
  PROTECTIVE close it MUST (a failed cancel must never abort the exit — the #391 Fix-3 principle:
  instrumentation/dedup is not a safety gate). Design the call so a cancel failure logs LOUD and
  PROCEEDS to the sell, rather than raising. This mirrors `_trigger_hard_stop`'s 3151 handling.
- **Idempotency:** the managed exit re-emits every quote until filled (dedup-gated). The guard cancel
  must be idempotent across re-emits — `_cancel_native_stop_guard_before_sell` already no-ops when
  `native_order is None`, so once cancelled it is a no-op. Confirm.

## 7. Test plan (per [[feedback_mutate_the_code_pin_the_threshold]])

- **The pair is whole:** with a native stop armed (simulate one), a v2 managed exit CANCELS it before
  selling, and the sell is NOT rejected oversold. **Assert it FAILS with the flag off** (or with the
  cancel call removed) — that binds the test to the (b)-without-(a) bug.
- **Cancel failure does not abort the protective sell** (the §6 safety point) — stub the cancel to
  raise, assert the sell still submits and logs LOUD.
- **Inert with no native stop:** flag ON, no guard armed ⇒ byte-identical (cancel is a no-op).
- **Flag off ⇒ byte-identical**, asserted.
- **Cancel events are published**, not dropped.

## 8. Provenance

Found by asking [[feedback_has_the_other_bot_solved_this]] on the v2 19:55-flatten dedup trace: the
guard-cancel mechanism EXISTS (714) and v2's exit path does not reach it, while v2 re-implemented its
partner (b) alone. Third instance of "the mechanism exists, the path doesn't reach it" — and the one
where taking half the pair is worse than taking neither.
