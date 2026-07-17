# OMS per-submit claim — design (DESIGN-FIRST, NOT BUILT)

**Status:** REVIEW-READY. No code. Live real-money stop path (ORB/Webull + v2/Schwab).
**Date:** 2026-07-17 · **Rollout:** attended, fleet-flat, flag-gated, one bot at a time.
**Supersedes nothing. Collapses two queued designs into one** (v2's per-submit claim + ORB's
`close_in_flight` participation).

---

## 1. The seam, in one sentence

**Two callers on two asyncio tasks both decide to sell the same position by reading a DB guard, and
the DB cannot see an order that is still inside its `await submit_order`.**

The OMS holds **ZERO locks** (`asyncio.Lock|threading.Lock` count = 0 in `oms/service.py`). All
safety is "single loop thread." That assumption is sound right up until a coroutine **yields** — and
`await submit_order` yields for **median +1.4s, max +4.5s** (measured, see §3).

## 2. The four callers (two per bot, one per task)

| bot | control-loop caller | tick-task caller | shared guard |
|---|---|---|---|
| **ORB** | `_window_flatten_armed_stops` (called 468) → `process_trade_intent` (3037) | `_evaluate_hard_stop_market_event` (called 2809 quote / 2830 trade) → `_trigger_hard_stop` (2861) → `process_trade_intent` (3194) | `get_open_exit_reserved_quantity` (803) |
| **v2** | `_v2_overnight_flatten` (called 476) → `_emit_v2_exit_on_loop` | `_evaluate_v2_managed_exit` (called 2822) → `_emit_v2_exit_on_loop` | `dedup_active` → `get_open_exit_reserved_quantity` (2028–2033) |

The decoupling is **deliberate and correct** — `_run_control_loop`'s own docstring:

> *"market-data ticks are handled by `_run_tick_consumer` on its own task so a slow broker-sync here
> cannot delay an exit decision."*

⭐ **The property that makes exits fast is the property that opens the window.** Do not "fix" it by
recoupling them.

## 3. Evidence (verified, not inferred)

- **Tick consumer is a separate task:** `asyncio.create_task(self._run_tick_consumer(stop_event))` at **382**.
- **The commit is AFTER the submit on BOTH paths:**
  - v2: `_emit_v2_exit_on_loop` → `await submit_order` (2265) → `session.commit()` at **2163**.
  - ORB: `process_trade_intent` → reserved-qty read **803** → build request 844–856 →
    `await submit_order` **857** → `_record_order_reports` **859** (this is where the `broker_orders`
    row is CREATED) → commit later.
    **Checked the falsifier:** the `session.commit()` at **837** is the ORB-quote-priced-entry ABANDON
    early-return, **not** the success path. There is **no commit between 803 and 857**.
  - ⇒ `get_open_exit_reserved_quantity` reads `broker_orders`; the row does not exist until after the
    submit returns. **The guard is blind for the whole submit.**
- **Window is measured, not guessed:** #459 found `[OMS-V2-MANAGED-EXIT]` (logged after `submit_order`
  + `_record_order_reports`) postdates the broker's own fill stamp by **median +1.4s, max +4.5s**, 30/30.
- **The race is SYMMETRIC.** Either caller can go first; the second one's read is blind either way.

## 4. Why the obvious fixes are TRAPS

### 4a. ⛔ "The flatten sets `close_in_flight = True` before its await"

**`close_in_flight` is NOT a scoped claim. It is a sticky state-machine flag.** Lifecycle:

| event | effect |
|---|---|
| `_trigger_hard_stop` submits (3167) | `True` |
| accepted / submitted / partially_filled (3195 → **returns at 3202**) | **stays `True`** |
| filled (3197, 3569) | stop popped |
| rejected, non-placing (3203) | `False` |
| rejected *"quantity already reserved"* / *"duplicate_exit_in_flight"* (3576) | **stays `True`** (deliberate stand-down) |

It means **"a close is working at the broker"**, not **"I am inside a submit await."** Different
lifetimes. If the flatten sets it and its close is **accepted then never filled**, the flag **never
clears** ⇒ the trail is permanently muted at 2851 ⇒ **ORB rides naked with its only exit disabled** —
the exact P0.6 case the flatten exists to prevent. **The naive fix causes the disease it treats.**

### 4b. ⛔ "The flatten CHECKS `close_in_flight` and skips" (rejected 2026-07-17)

Same stickiness, one level up. A close **accepted-and-never-filled** leaves the flag `True` forever
⇒ the flatten **skips forever**.

⭐ **The principle, not the case: the flatten is the BACKSTOP. A backstop gated on the optimism of the
thing it is backing up is not a backstop.** (It looks narrow in RTH — a market close fills. "Narrow"
is what ERNA was, and the flatten exists precisely for when things do not work.)

**What clears it on accepted-never-filled? — checked: nothing reliable during RTH.**
`_collect_drift_cancel_candidates` explicitly skips it:
```python
if str(intent.intent_type).lower() != "open":
    continue  # don't auto-cancel close/scale chases here
```
Both the flatten and the hard stop emit `intent_type="close"`. `MARKET_CLOSED` does not fire at 10:00
(inside the 7–20 ET fillable session). ⇒ **confirmed sticky.**

### 4c. ⛔ Dropping `include_native_stop_guard=False` (the v2 variant)

Makes dedup permanently true while a native stop rests ⇒ the +2% floor could never fire.

### 4d. ⛔ A per-DAY claim

That was #478's, and it was **removed for cause**: it silently gave up when a thin-AH limit expired
unfilled — the naked-overnight case. **#478's claim was not wrong in kind, it was wrong in SCOPE: it
protected a day when it needed to protect an await.**

## 5. The design

A **submit-scoped, in-memory claim**, released in `finally`:

```python
# OmsRiskService.__init__
self._submit_in_flight: set[tuple[str, str]] = set()   # (broker_account_name, symbol)
```

At each of the four call sites, wrapping ONLY the submit await:

```python
key = (broker_account_name, symbol)
if key in self._submit_in_flight:
    continue            # another caller is mid-submit for this position — do NOT double-submit
self._submit_in_flight.add(key)
try:
    ... await <the submit path> ...
finally:
    self._submit_in_flight.discard(key)   # CANNOT stick — this is the whole point
```

**Why `finally` is the design, not a detail:** it is the one property `close_in_flight` lacks. The
claim cannot outlive the await, so it cannot mute a backstop. Scoped exactly to the race: after the
await the row is committed and the existing DB guards (`get_open_exit_reserved_quantity` /
`dedup_active`) work correctly — they were never wrong, only blind for 1.4–4.5s.

**Precedent in-tree:** the entry side already has this shape — `schwab_1m_v2.py::cw_v2_emit_claimed`
(claimed before the emit). The exit side never got it. ⇒ [[feedback_has_the_other_bot_solved_this]].

**Ordering discipline:** claim BEFORE any state mutation that a skip would strand. Specifically, in
`_window_flatten_armed_stops` the claim/skip must come **before** `self._window_flattened.add(key)` —
claim-then-skip would silently give up for the day (the 4d disease). Skip without claiming ⇒ retries
next 5s pass, hours before the session ends.

**Flag:** `oms_submit_claim_enabled` (default **False** ⇒ byte-identical). Rollback = flag false.

## 6. What this does NOT fix (state it, do not imply it)

- **It is not a lock.** It is correct only because all four callers run on ONE event loop thread; the
  check-and-add is atomic w.r.t. that thread. **If the OMS ever becomes multi-threaded or
  multi-process, this is wrong.** Document it at the definition.
- **It does not fix the E5 matched pair** (v2 re-implementing `include_native_stop_guard=False`
  without the `_cancel_native_stop_guard_before_sell` precondition). Separate design.
- **It does not fix #388-for-v2** (`cw_entries_this_flip` counting emits). Separate design.
- **It does not address the sticky `close_in_flight` itself.** ⚠️ **OPEN QUESTION for review:** an
  accepted-never-filled protective close appears to mute the trail at 2851 indefinitely on the
  EXISTING path, independent of this change. The drift-cancel skips close intents (§4b). Whether the
  working-order refresh (`_stop_guard_refresh_stage`) rescues it is **NOT verified** — flagged, not
  claimed. If it does not, that is its own P0.

## 7. Test plan

Per [[feedback_mutate_the_code_pin_the_threshold]] — a green suite is not evidence until a deliberate
break turns it red.

1. **The race test (the anchor):** drive caller A into its submit await (a broker stub that blocks on
   an `asyncio.Event`), fire caller B from the other task, assert exactly ONE `submit_order`.
   **Assert it FAILS with the flag off** — that is the proof the test binds the bug, not the code.
2. **`finally` releases on EVERY path:** return, broker raise, `asyncio.CancelledError`. The stick is
   the catastrophic failure; test it directly.
3. **All four callers**, both directions each (A→B and B→A) = 8 cases. The race is symmetric.
4. **Flag off ⇒ byte-identical**, asserted, not claimed (#467's lesson: "byte-identical on a prompt
   break" was FALSE — 24 of 50 entries changed).
5. **Mutation:** delete the `finally` ⇒ the stick test must go red. Delete the claim ⇒ the race test
   must go red.
6. **No threshold introduced** (deliberately — nothing to pin, nothing to tune, nothing to turn off
   by accident).

## 8. Rollout

Attended, fleet-flat, off-hours. Flag on for **ORB first** (higher exposure: fires its flatten ~10%
of days — 6/63 — vs v2 needing a rare overnight-bound position; and ORB is on **Webull**, the broker
ERNA proved omits a real fill from its positions endpoint for **≥61s**, so the "the broker will reject
the duplicate" fallback is weakest exactly there). Then v2.

⚠️ **"Bounded, not naked" is an ASSUMPTION, not a floor.** It rests on the broker's oversold check
being synchronous with its own fills. Schwab rejected NXTC ✓ — but NXTC is *also* the day Schwab's
position view disagreed with our fills. Same class of lag. Probably right; not something to rest on.

## 9. Provenance

Traced 2026-07-17 from the v2 19:55-flatten question (dedup is the only double-submit guard since
#478 dropped the per-day claim). The ORB seam was found by asking
[[feedback_has_the_other_bot_solved_this]] — *"does the other bot have this too?"* It does, ~10× more
often, and nobody had looked.

⭐ **The class this belongs to is NEW.** The other five instances were *a path that never reached an
existing mechanism*. This one is **two correct guards whose INTERACTION is the bug**: the flatten's
`_cancel_native_stop_guard_before_sell` (correct — it prevents reverse-rejection) removes the very
native guard that `_trigger_hard_stop`'s RTH defer (3141–3165, correct — it prevents double-close)
relies on to stand down. Each guard is right. Together they open the window.

**Both traps in §4 were proposed and rejected during this design — one by the operator, one by me.**
Neither is safe. Record them so they are not re-proposed.
